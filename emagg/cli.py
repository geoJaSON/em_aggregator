"""Command line entry point: ``emagg serve``, ``emagg poll``, ``emagg sources``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import httpx

from emagg.config import Config, load_config
from emagg.scheduler import Notifier, Scheduler, build_sources
from emagg.sources import REGISTRY, SourceContext
from emagg.store import Store


def _load(args: argparse.Namespace) -> tuple[Config, httpx.AsyncBaseTransport | None]:
    if getattr(args, "demo", False):
        from emagg.demo import DemoTransport, demo_config

        return demo_config(), DemoTransport()
    return load_config(args.config), None


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from emagg.api import create_app

    config, transport = _load(args)
    store = None
    if args.demo:
        from emagg.demo import seed_field_reports

        store = Store(":memory:")
        seed_field_reports(store)
    app = create_app(config, demo=args.demo, transport=transport, store=store)
    print(f"EM Aggregator on http://{args.host}:{args.port}  ({len(config.sources)} sources configured)")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower(), timeout_graceful_shutdown=3)


async def _poll(args: argparse.Namespace) -> int:
    config, transport = _load(args)
    store = Store(":memory:")
    async with httpx.AsyncClient(
        headers={"User-Agent": config.app.user_agent},
        timeout=config.app.request_timeout,
        follow_redirects=True,
        transport=transport,
    ) as http:
        ctx = SourceContext(http=http, area=config.area, store=store)
        configs = [c for c in config.sources if not args.source or c.id in args.source]
        sources, problems = build_sources(configs, ctx)
        scheduler = Scheduler(sources, store, Notifier(), config.area)
        results = await scheduler.poll_all()

    failed = 0
    report = []
    for r in results:
        events = store.query_events(sources=[r["source"]])
        report.append({**r, "top": [{k: e[k] for k in ("severity", "title", "area")} for e in events[: args.show]]})
    for sid, why in problems.items():
        report.append({"source": sid, "ok": False, "error": why})
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for r in report:
            if r["ok"]:
                print(f"OK    {r['source']:<20} {r['events']:>5} events  {r['ms']:>6} ms")
                for e in r["top"]:
                    print(f"        [{e['severity']:<8}] {e['title']}" + (f" — {e['area']}" if e["area"] else ""))
            else:
                failed += 1
                print(f"FAIL  {r['source']:<20} {r['error']}")
    return 1 if failed else 0


def cmd_sources(_: argparse.Namespace) -> None:
    for name, cls in sorted(REGISTRY.items()):
        req = f"  requires: {', '.join(cls.required_options)}" if cls.required_options else ""
        note = f"  ({cls.note})" if cls.note else ""
        print(f"{name:<18} {cls.default_name:<26} [{cls.category.value}] every {cls.default_interval}s{req}{note}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="emagg", description="Emergency-management data aggregator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the poller and web dashboard")
    serve.add_argument("-c", "--config", help="config YAML (default: ./config.yaml, else built-in defaults)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--demo", action="store_true", help="run offline with simulated sample data")
    serve.add_argument("--log-level", default="INFO")
    serve.set_defaults(func=cmd_serve)

    poll = sub.add_parser("poll", help="poll each source once and print what came back (for testing feeds)")
    poll.add_argument("-c", "--config")
    poll.add_argument("-s", "--source", action="append", help="only this source id (repeatable)")
    poll.add_argument("--demo", action="store_true")
    poll.add_argument("--show", type=int, default=5, help="events to print per source")
    poll.add_argument("--json", action="store_true")
    poll.set_defaults(func=lambda a: sys.exit(asyncio.run(_poll(a))))

    srcs = sub.add_parser("sources", help="list available source types")
    srcs.set_defaults(func=cmd_sources)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
