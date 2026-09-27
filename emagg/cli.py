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
    elif args.markdown:
        failed = sum(1 for r in report if not r["ok"])
        print(f"# Feed check\n\n{len(report) - failed} of {len(report)} feeds OK.\n")
        print("| Feed | Result | Events | Sample |\n|---|---|---|---|")
        for r in sorted(report, key=lambda r: (r["ok"], r["source"])):
            sample = "; ".join(e["title"] for e in r.get("top", [])[:2]).replace("|", "/")
            result = "OK" if r["ok"] else str(r.get("error", "")).replace("|", "/")[:120]
            print(f"| `{r['source']}` | {result} | {r.get('events', '')} | {sample[:160]} |")
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


def catalog_rows(state: str | None = None) -> list[dict[str, str]]:
    import re

    from emagg import catalog

    rows = []
    for e in catalog.load_entries():
        states = e.get("states") or []
        if state and states and state.upper() not in states:
            continue
        cls = REGISTRY.get(e["type"])
        meta = e.get("meta") or {}
        keys = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)", str({k: v for k, v in e.items() if k != "meta"}))))
        rows.append({
            "id": e["id"],
            "name": e.get("name") or (cls.default_name if cls else e["type"]),
            "category": e.get("category") or (cls.category.value if cls else "other"),
            "states": ", ".join(states) or "national",
            "type": e["type"],
            "confidence": meta.get("confidence", ""),
            "access": ("env " + ", ".join(keys)) if keys else ("off by default" if e.get("enabled") is False else "open"),
        })
    return rows


def cmd_catalog(args: argparse.Namespace) -> None:
    rows = catalog_rows(args.state)
    if args.markdown:
        print("| Feed | Category | States | Adapter | Confidence | Access |")
        print("|---|---|---|---|---|---|")
        for r in sorted(rows, key=lambda r: (r["category"], r["states"], r["name"])):
            print(f"| {r['name']} (`{r['id']}`) | {r['category']} | {r['states']} | {r['type']} | {r['confidence']} | {r['access']} |")
        return
    for r in rows:
        print(f"{r['id']:<28} {r['category']:<9} {r['states'][:18]:<18} {r['type']:<18} {r['confidence']:<7} {r['access']}")
    print(f"\n{len(rows)} catalog feeds")


def source_hosts(configs) -> dict[str, set[str]]:
    """Hostnames each source contacts: URLs in its options plus the fixed endpoints in its adapter module."""
    import inspect
    import re
    from urllib.parse import urlsplit

    module_hosts: dict[str, set[str]] = {}
    out: dict[str, set[str]] = {}
    for cfg in configs:
        cls = REGISTRY.get(cfg.type)
        if cls is None:
            continue
        if cfg.type not in module_hosts:
            src = inspect.getsource(inspect.getmodule(cls))
            module_hosts[cfg.type] = {
                h for u in re.findall(r'"(https?://[^"{ ]+)', src)
                if (h := urlsplit(u).hostname) and not h.endswith((".invalid", "example.com", "example.coop", "example.gov"))
            }
        hosts = set(module_hosts[cfg.type])
        for value in cfg.options.values():
            if isinstance(value, str) and value.startswith("http"):
                hosts.add(urlsplit(value.replace("{api_key}", "")).hostname)
        out[cfg.id] = {h for h in hosts if h}
    return out


def cmd_hosts(args: argparse.Namespace) -> None:
    if args.catalog:
        from emagg.config import CatalogConfig, Config

        config = Config(catalog=CatalogConfig(enabled=True))
        configs = config.sources
    else:
        configs = [c for c in load_config(args.config).sources if c.enabled]
    hosts = sorted(set().union(*source_hosts(configs).values())) if configs else []
    print("\n".join(hosts))
    if not args.quiet:
        print(f"\n# {len(hosts)} hosts for {len(configs)} sources (HTTPS, port 443). The dashboard's map tiles are fetched by"
              " each viewer's browser from *.basemaps.cartocdn.com, tile.openstreetmap.org and server.arcgisonline.com.",
              file=sys.stderr)


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
    poll.add_argument("--markdown", action="store_true", help="markdown table (e.g. for a verification report)")
    poll.set_defaults(func=lambda a: sys.exit(asyncio.run(_poll(a))))

    srcs = sub.add_parser("sources", help="list available source types")
    srcs.set_defaults(func=cmd_sources)

    hosts = sub.add_parser("hosts", help="list hostnames the configured feeds contact (for network allowlists)")
    hosts.add_argument("-c", "--config")
    hosts.add_argument("--catalog", action="store_true", help="every catalog feed, regardless of area and enabled state")
    hosts.add_argument("-q", "--quiet", action="store_true")
    hosts.set_defaults(func=cmd_hosts)

    cat = sub.add_parser("catalog", help="list the built-in catalog of known feeds")
    cat.add_argument("--state", help="only feeds covering this state (plus national feeds)")
    cat.add_argument("--markdown", action="store_true")
    cat.set_defaults(func=cmd_catalog)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
