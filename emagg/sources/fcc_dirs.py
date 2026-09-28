"""FCC DIRS (Disaster Information Reporting System) communications status reports.

When the FCC activates DIRS for a disaster it publishes a daily "Communications Status Report for Areas Impacted
by <event>" as EDOCS attachments: ``https://docs.fcc.gov/public/attachments/DOC-<n>A1.pdf`` (also ``.docx`` and
``.txt``). The report has a county table, "Percent Cell Sites Out-Of-Service By County" (columns: State |
Affected County | Cell Sites Served | Cell Sites Out | Percent Out | Cell Sites Out Due to Damage | ... Transport
| ... Power | Cell Sites Up but On Back-up Power), cable/wireline subscribers out, broadcast stations off air and
affected 911 centers (PSAPs). This adapter reads the ``.docx`` (preferred) or ``.txt`` attachment -- PDF needs a
dependency we do not carry -- and emits:

* one ``comms`` event per county with cell sites out (``metrics.kind = dirs_cell_sites``), drawn as the county,
* one per state for cable/wireline subscribers out (``dirs_wireline``) when the report gives them,
* one per affected PSAP (``dirs_psap``) when the report has a PSAP table.

Discovery::

    - id: fcc_dirs
      type: fcc_dirs
      reports: [DOC-406055A1]                     # DOC ids, attachment URLs or local .docx/.txt files, and/or
      event_pages: [https://www.fcc.gov/helene]   # pages scanned for DOC-nnnnnnA1 links or /document/ report pages

Only the newest report of each event counts (so a county restored in today's report does not keep yesterday's
outage), and reports whose "as of" time is older than ``max_age_hours`` (default 48; 0 = no limit) are ignored, so the
source is simply empty when DIRS is not active. A report found through an event page with no readable "as of" time
is dated from its fcc.gov/document/ page address, or else dropped. A status report whose tables are not recognised,
or an attachment address that answers with an HTML page, is reported as an error rather than read as "nothing to
report" (a warning is logged when other reports did load). Other options: ``formats`` (default [docx, txt]), ``include_zero``
(counties with no sites out, default false), ``include_wireline`` / ``include_psaps`` (default true), ``link``,
``headers``, and per-poll bounds ``max_reports`` (4), ``max_event_pages`` (10), ``max_document_pages`` (4) and
``max_candidates`` (3); at most 4 requests run at once. The EDOCS search API is not used: no open-source client
confirmed its request/response shape.

The cell-site percentage is not the share of customers without service: networks overlap (the FCC says so in
every report).
"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import logging
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from emagg import regions
from emagg.models import Category, Event, Severity, utcnow
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, num, parse_time, to_int

ATTACHMENTS = "https://docs.fcc.gov/public/attachments/"
DOC_RE = re.compile(r"DOC-(\d{5,7})A(\d{1,2})", re.I)
FORMATS = ("docx", "txt")
MAX_XML_BYTES = 40_000_000
log = logging.getLogger(__name__)
Block = tuple[str, Any]  # ("p", text) or ("t", rows)

# --- document text -------------------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MC_FALLBACK = f"{_MC}Fallback"
# elements that only wrap rows, cells or paragraphs (content controls, custom XML, alternate content)
_WRAPPERS = {f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml", f"{_W}smartTag", f"{_MC}AlternateContent", f"{_MC}Choice"}


def _children(el: ET.Element, tag: str) -> list[ET.Element]:
    """Direct ``tag`` children of ``el``, looking through wrapper elements but never into a nested table."""
    out: list[ET.Element] = []
    for child in el:
        if child.tag == tag:
            out.append(child)
        elif child.tag in _WRAPPERS:
            out.extend(_children(child, tag))
    return out


def _norm_ws(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split())


def _run_text(el: ET.Element, out: list[str]) -> None:
    for child in el:
        tag = child.tag
        if tag == _MC_FALLBACK or tag == f"{_W}tbl":  # duplicate of mc:Choice; nested tables read separately
            continue
        if tag == f"{_W}t":
            out.append(child.text or "")
        elif tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
            out.append(" ")
        elif tag == f"{_W}noBreakHyphen":
            out.append("-")
        else:
            _run_text(child, out)


def _para_text(p: ET.Element) -> str:
    out: list[str] = []
    _run_text(p, out)
    return _norm_ws("".join(out))


def _table_rows(tbl: ET.Element, nested: list[Block] | None = None) -> list[list[str]]:
    """Rows of cell text. Horizontally merged cells (gridSpan) are padded so columns line up; vertically merged
    continuation cells (vMerge) repeat the text above, as a reader sees it. Rows and cells inside content controls
    count; a table nested in a cell is not mixed into this one (it is added to ``nested`` as its own block)."""
    rows: list[list[str]] = []
    prev: list[str] = []
    for tr in _children(tbl, f"{_W}tr"):
        cells: list[str] = []
        trpr = tr.find(f"{_W}trPr")
        before = trpr.find(f"{_W}gridBefore") if trpr is not None else None
        if before is not None:
            cells.extend([""] * max(0, min(20, to_int(before.get(f"{_W}val")) or 0)))
        for tc in _children(tr, f"{_W}tc"):
            if nested is not None:
                nested.extend(("t", _table_rows(inner, nested)) for inner in _children(tc, f"{_W}tbl"))
            tcpr = tc.find(f"{_W}tcPr")
            span, cont = 1, False
            if tcpr is not None:
                gs = tcpr.find(f"{_W}gridSpan")
                if gs is not None:
                    span = max(1, min(20, to_int(gs.get(f"{_W}val")) or 1))
                vm = tcpr.find(f"{_W}vMerge")
                if vm is not None and (vm.get(f"{_W}val") or "continue") == "continue":
                    cont = True
            col = len(cells)
            if cont:
                text = prev[col] if col < len(prev) else ""
            else:
                text = _norm_ws(" ".join(t for p in _children(tc, f"{_W}p") if (t := _para_text(p))))
            cells.append(text)
            cells.extend([""] * (span - 1))
        if any(cells):
            rows.append(cells)
        prev = cells
    return rows


def _walk_body(el: ET.Element, blocks: list[Block]) -> None:
    for child in el:
        if child.tag == f"{_W}p":
            text = _para_text(child)
            if text:
                blocks.append(("p", text))
        elif child.tag == f"{_W}tbl":
            nested: list[Block] = []
            blocks.append(("t", _table_rows(child, nested)))
            blocks.extend(nested)
        elif child.tag not in (f"{_W}sectPr", _MC_FALLBACK):
            _walk_body(child, blocks)  # w:sdt, w:customXml, mc:AlternateContent wrappers


def docx_blocks(data: bytes) -> list[Block]:
    """Paragraphs and tables of a .docx in document order (zipfile + ElementTree, no other dependency)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                raise ValueError("word/document.xml is too large")
            xml = zf.read(info)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError(f"not a .docx file ({exc})") from exc
    if b"<!DOCTYPE" in xml[:4096] or b"<!ENTITY" in xml[:4096]:
        raise ValueError("unexpected DTD in word/document.xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"invalid word/document.xml ({exc})") from exc
    blocks: list[Block] = []
    body = root.find(f"{_W}body")
    _walk_body(body if body is not None else root, blocks)
    return blocks


# --- column labels --------------------------------------------------------------------------------------

NUMERIC_KINDS = {"served", "out", "percent", "damage", "transport", "power", "backup", "subscribers"}
_COUNTY_WORDS = re.compile(r"\b(county|counties|parish|parishes|municipio|municipios|municipality|borough|county equivalent)\b")


def column_kind(label: str) -> str | None:
    """What a table header cell holds (state, county, served, out, percent, damage, transport, power, backup,
    subscribers, psap, status), or None for anything else."""
    s = re.sub(r"\be911\b", "911", str(label or "").lower().replace("-", " "))
    s = re.sub(r"(?<=[a-z%)])\d+\b", "", s)  # footnote markers: "County1", "Power2"
    s = re.sub(r"[^a-z0-9%/ ]+", " ", s)
    s = " ".join(s.split())
    if not s or len(s) > 70 or " by " in f" {s} ":  # "... Out-Of-Service By County" is a table title
        return None
    words = s.split()
    if len(words) > 9:
        return None
    if s in ("state", "states", "state/territory", "state territory", "state or territory", "territory"):
        return "state"
    if words[-1] == "status" or s in ("condition", "psap condition"):
        return "status"
    if "psap" in s or "answering point" in s or "911 center" in s or "call center" in s:
        return "psap"
    if "subscriber" in s:
        return "subscribers"
    if _COUNTY_WORDS.search(s) and "site" not in s:
        return "county"
    if "back up" in s or "backup" in s or "up but" in s or "generator" in s:
        return "backup"
    if "damage" in s:
        return "damage"
    if "transport" in s or "backhaul" in s:
        return "transport"
    if "power" in s:
        return "power"
    if "percent" in s or "%" in s or "pct" in words:
        return "percent"
    if "served" in s or s in ("total cell sites", "cell sites", "total sites", "sites"):
        return "served"
    if "out" in words and ("site" in s or "sites" in s or s in ("out", "total out", "number out")):
        return "out"
    return None


def _header_kinds(row: list[str]) -> list[str | None] | None:
    """Column kinds when ``row`` is a header row of a table we understand, else None."""
    kinds = [column_kind(c) for c in row]
    found = [k for k in kinds if k]
    filled = [c for c in row if c.strip()]
    if len(found) < 2 or len(found) * 2 < len(filled) or len(set(found)) < len(found):
        return None
    if _table_type(kinds):
        return kinds
    return None


def _header_ish(row: list[str]) -> bool:
    filled = [c for c in row if c.strip()]
    return bool(filled) and not any(_is_num_token(c) for c in filled) and any(column_kind(c) for c in filled)


def _table_type(kinds: list[str | None]) -> str | None:
    ks = set(k for k in kinds if k)
    if "county" in ks and ks & {"out", "percent"}:
        return "cells"
    if "subscribers" in ks and "state" in ks and "county" not in ks:
        return "wireline"
    if "psap" in ks and ("status" in ks or "state" in ks or "county" in ks):
        return "psap"
    if "state" in ks and ks & {"out", "percent"}:
        return "state_cells"
    return None


# --- plain text (.txt attachment) ----------------------------------------------------------------------

_NUM_TOKEN = re.compile(r"^(?:[-\u2013\u2014]|n/?a|[\d,]+(?:\.\d+)?\s*%?|\.\d+\s*%?)$", re.I)


def _is_num_token(tok: str) -> bool:
    return bool(_NUM_TOKEN.match(tok.strip()))


def _split_cells(line: str) -> list[str]:
    if "\t" in line:
        cells = line.split("\t")
    elif line.count("|") >= 2:
        cells = line.strip().strip("|").split("|")
    else:
        cells = re.split(r"\s{2,}", line.strip())
    cells = [_norm_ws(c) for c in cells]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _looks_like_prose(tok: str) -> bool:
    words = tok.split()
    return len(words) > 9 or (len(words) > 5 and tok.rstrip().endswith((".", ":")))


_STATUS_START = re.compile(
    r"^(?:re-?routed|down|degraded|isolated|out\s+of\s+service|evacuated|inoperable|off-?line|not\s+operational|"
    r"operational|normal|impaired|diverted|partial(?:ly)?|limited|affected|restored|out|outage|no\s+(?:911|service)|"
    r"congested|overloaded)\b", re.I)
_STATUS_ALI = re.compile(r"\bwith(?:out)?\s+(?:ali|ani)\b|\b(?:no|lost)\s+(?:ali|ani)\b|\bali\s*/\s*ani\b", re.I)
_PSAP_NOUN = re.compile(r"\b(?:county|parish|911|e911|sheriff|police|center|centre|dispatch|communications|district|"
                        r"office|department|psap|city|town|borough|authority|agency)\b", re.I)


def _is_status(tok: str) -> bool:
    """A PSAP status cell ("Down", "Rerouted without ALI", ...), as opposed to a state, county or PSAP name."""
    t = tok.strip()
    if not t or len(t.split()) > 8 or "%" in t:
        return False
    if _STATUS_ALI.search(t) or re.match(r"re-?routed\b", t, re.I):
        return True
    return bool(_STATUS_START.match(t)) and not _PSAP_NOUN.search(t) and not re.search(r"\d", t)


def _align_text_row(cells: list[str], kinds: list[str | None]) -> list[str] | None:
    """Place the text cells of one flattened all-text row (everything before its status) onto the columns before
    the status. Blank cells were dropped with the blank lines, so: the first cell is the state only when it is one
    (else the state is carried from the row above), and the rest are right-aligned (PSAP name last, county before
    it). None when the cells cannot be placed."""
    pre = len(kinds) - 1
    toks = list(cells)
    if len(toks) > pre:  # a stray cell: resync on a state that leaves a row that fits
        starts = [j for j, t in enumerate(toks) if len(toks) - j <= pre and state_code(t)] if kinds[0] == "state" else []
        if not starts:
            return None
        toks = toks[starts[0]:]
    if len(toks) == pre:
        return toks
    if kinds[0] == "state":
        state, rest = (toks[0], toks[1:]) if state_code(toks[0]) else ("", toks)
        return [state] + [""] * (pre - 1 - len(rest)) + rest
    return [""] * (pre - len(toks)) + toks


def _text_rows(tokens: list[str], kinds: list[str | None]) -> tuple[list[list[str]], int]:
    """Rows of an all-text table (PSAPs) flattened one cell per line. When the last column is a status, each status
    cell ends a row, so a blank cell cannot shift later rows. Otherwise (or when no status cell is recognised) the
    cells are taken n at a time, and a group that does not start with a state (when the first column is the state)
    is dropped one cell at a time."""
    if kinds[-1] == "status":
        rows, used = _status_rows(tokens, kinds)
        if rows:
            return rows, used
    n = len(kinds)
    rows = []
    i = start = 0
    while i < len(tokens):
        again, _, used = _header_scan(tokens, i)
        if again:
            if again != kinds:  # another table starts
                break
            i += used  # header repeated after a page break
            start = i
            continue
        group = tokens[i:i + n]
        if len(group) < n or any(_looks_like_prose(t) or _header_scan(tokens, i + x)[0] for x, t in enumerate(group)):
            break
        if kinds[0] == "state" and not state_code(group[0]):
            i += 1  # out of step: resync on the next state
            continue
        rows.append(group)
        i += n
        start = i
    return rows, start


def _status_rows(tokens: list[str], kinds: list[str | None]) -> tuple[list[list[str]], int]:
    n = len(kinds)
    rows: list[list[str]] = []
    pending: list[str] = []
    i = start = 0
    while i < len(tokens):
        again, _, used = _header_scan(tokens, i)
        if again:
            if again != kinds:  # another table starts
                break
            i += used  # header repeated after a page break
            pending, start = [], i
            continue
        tok = tokens[i]
        if _looks_like_prose(tok):
            break
        if _is_status(tok):
            row = _align_text_row(pending, kinds) if pending else None
            if row:
                rows.append(row + [tok])
            i += 1
            pending, start = [], i
            continue
        pending.append(tok)
        if len(pending) > n:  # no status where one should be: the table has ended
            break
        i += 1
    return rows, start


def _stacked_rows(tokens: list[str], kinds: list[str | None]) -> tuple[list[list[str]], int]:
    """Rows of a table flattened to one cell per line. Leading text columns (state, county) followed by numeric
    columns are delimited by the numbers, so a blank state or a missing trailing number cannot shift later rows.
    Returns (rows, tokens consumed)."""
    n = len(kinds)
    lead = 0
    while lead < n and kinds[lead] not in NUMERIC_KINDS:
        lead += 1
    numeric_tail = lead < n and all(k in NUMERIC_KINDS for k in kinds[lead:])
    if not numeric_tail:
        return _text_rows(tokens, kinds)
    rows: list[list[str]] = []
    i = 0
    k = n - lead
    texts: list[str] = []
    start = 0
    while i < len(tokens):
        again, _, used = _header_scan(tokens, i)
        if again:
            if again != kinds:  # another table starts
                return rows, start
            i += used  # header repeated after a page break
            texts, start = [], i
            continue
        tok = tokens[i]
        if _is_num_token(tok):
            nums: list[str] = []
            while i < len(tokens) and len(nums) < k and _is_num_token(tokens[i]):
                nums.append(tokens[i])
                i += 1
            if texts:
                row = ([""] * lead + texts)[-lead:] if lead else []
                rows.append(row + nums + [""] * (k - len(nums)))
            texts, start = [], i
            continue
        if _looks_like_prose(tok) or len(texts) >= max(lead, 1) + 1:
            return rows, start
        texts.append(tok)
        i += 1
    return rows, start if texts else i


_GROUP_LABEL = re.compile(r"\bdue\s+to\s*:?\s*$", re.I)
_CAUSES = ("damage", "transport", "power")


def _header_scan(tokens: list[str], i: int) -> tuple[list[str | None], list[str], int]:
    """(kinds, labels, tokens consumed) of the consecutive header labels starting at tokens[i]; empty when they do
    not form a known table. A group label spanning the cause columns ("Cell Sites Out Due to" over "Damage",
    "Transport", "Power") is consumed but is not a column."""
    j = i
    kinds: list[str | None] = []
    labels: list[str] = []
    while j < len(tokens):
        tok = tokens[j]
        if kinds and _GROUP_LABEL.search(tok) and j + 1 < len(tokens) and column_kind(tokens[j + 1]) in _CAUSES:
            j += 1
            continue
        k = column_kind(tok)
        if not k or k in kinds:
            break
        kinds.append(k)
        labels.append(tok)
        j += 1
    if len(kinds) >= 2 and _table_type(kinds):
        return kinds, labels, j - i
    return [], [], 0


def text_blocks(text: str) -> list[Block]:
    """Paragraphs and tables from a .txt rendition. Tables may be delimited (tab, pipe or aligned columns, one row
    per line) or flattened one cell per line; both are reassembled into rows."""
    text = text.replace("\ufeff", "").replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n")
    items: list[tuple[str, Any]] = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        cells = _split_cells(raw)
        filled = [c for c in cells if c]
        delimited = "\t" in raw or raw.count("|") >= 2
        # Aligned columns (2+ spaces) only count as a row when they hold numbers or a header: prose often has two
        # spaces after a full stop. Tab/pipe-delimited lines are rows from two cells up (State | Subscribers Out).
        if (delimited and len(filled) >= 2) or \
                (len(filled) >= 3 and (sum(map(_is_num_token, filled)) >= 2 or _header_kinds(cells))):
            items.append(("row", cells))
        else:
            items.append(("line", _norm_ws(raw)))
    blocks: list[Block] = []
    i = 0
    while i < len(items):
        if items[i][0] == "row":
            rows = []
            while i < len(items) and items[i][0] == "row":
                rows.append(items[i][1])
                i += 1
            blocks.append(("t", rows))
            continue
        j = i
        while j < len(items) and items[j][0] == "line":
            j += 1
        tokens = [items[x][1] for x in range(i, j)]  # a run of single-cell lines: prose and/or flattened tables
        k = 0
        while k < len(tokens):
            kinds, labels, run = _header_scan(tokens, k)
            if run:
                rows, used = _stacked_rows(tokens[k + run:], kinds)
                if rows:
                    blocks.append(("t", [labels] + rows))
                    k += run + used
                    continue
            blocks.append(("p", tokens[k]))
            k += 1
        i = j
    return blocks


def document_blocks(payload: bytes | str) -> list[Block]:
    """Blocks from a report file: .docx (zip) or text. PDF is rejected with a clear message."""
    if isinstance(payload, str):
        return text_blocks(payload)
    if payload[:4] == b"PK\x03\x04":
        return docx_blocks(payload)
    if payload[:5] == b"%PDF-":
        raise ValueError("PDF reports are not supported; use the report's .docx or .txt attachment")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return text_blocks(payload.decode(enc))
        except UnicodeDecodeError:
            continue
    return text_blocks(payload.decode("latin-1"))


# --- states and counties ------------------------------------------------------------------------------

_STATE_ALIASES = {
    "us virgin islands": "VI", "u s virgin islands": "VI", "usvi": "VI", "virgin islands": "VI",
    "united states virgin islands": "VI", "northern mariana islands": "MP", "cnmi": "MP",
    "commonwealth of the northern mariana islands": "MP", "d c": "DC", "dc": "DC", "washington dc": "DC",
    "district of columbia": "DC", "washington d c": "DC",
}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def state_code(value: Any) -> str | None:
    """Two-letter code for a state name or code (including territories), else None."""
    text = re.sub(r"(?<=[A-Za-z.])\d+$|\s*\*+$", "", clean_text(value) or "")  # footnote markers
    if not text:
        return None
    if len(text) == 2 and text.isalpha():
        return text.upper() if text.upper() in regions.state_codes() else None
    code = regions.state_code_for_name(text)
    if code:
        return code
    key = " ".join(re.sub(r"[^a-z ]+", " ", _fold(text)).split())
    return _STATE_ALIASES.get(key) or regions.state_code_for_name(key)


_INDEPENDENT_CITIES = {"24510", "29510", "32510"}  # Baltimore, St. Louis, Carson City (plus Virginia's 51510+)


def _is_city(county: dict[str, Any]) -> bool:
    fips = county["fips"]
    return fips in _INDEPENDENT_CITIES or (fips.startswith("51") and int(fips[2:]) >= 510)


_SUFFIX = re.compile(
    r"\b(county|parish|municipio|municipality|borough|census area|city and borough|island|islands|district)\b")


def _county_key(name: str) -> str:
    s = _fold(name).replace("saint ", "st ").replace("sainte ", "ste ")
    key = re.sub(r"[^a-z]", "", _SUFFIX.sub(" ", s))
    if not key:  # the name is only suffix words ("Island County", WA): drop trailing ones, keep the first
        words = re.sub(r"[^a-z ]", " ", s).split()
        while len(words) > 1 and _SUFFIX.fullmatch(words[-1]):
            words.pop()
        key = "".join(words)
    return key


_county_index_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}


def _county_index() -> dict[tuple[str, str], dict[str, Any]]:
    if "idx" not in _county_index_cache:
        idx: dict[tuple[str, str], dict[str, Any]] = {}
        counties = regions._data()["counties"]
        for c in sorted(counties, key=_is_city, reverse=True):  # counties overwrite same-named cities
            key = _county_key(c["name"])
            if _is_city(c):
                idx[(c["state"], key + "city")] = c
            idx[(c["state"], key)] = c
        _county_index_cache["idx"] = idx
    return _county_index_cache["idx"]


def lookup_county(state: str, name: str) -> dict[str, Any] | None:
    """County record for a report's county cell: tolerant of County/Parish/Municipio suffixes, accents, "St."/
    "Saint" and Virginia-style independent cities ("Richmond city" vs "Richmond County")."""
    name = re.sub(r"(?<=[A-Za-z.])\d+$|\s*\*+$", "", clean_text(name) or "")  # footnote markers
    if not state or not name:
        return None
    key = _county_key(name)
    if not key:
        return None
    is_city = bool(re.search(r"\bcity\s*$", name.strip(), re.I)) and not re.search(r"\bcounty\b", name, re.I)
    idx = _county_index()
    if is_city:
        city = idx.get((state, key))  # "richmondcity" is stored for independent cities only
        if city:
            return city
        base = key[:-4] if key.endswith("city") else key
        if (state, base + "city") in idx:
            return idx[(state, base + "city")]
    return idx.get((state, key)) or regions.find_county(state, name)


def state_geometry(st: str) -> dict[str, Any] | None:
    """The state's outline. State-wide figures are drawn as the state, not as a point, so the scheduler does not
    attribute them to whichever county lies under the point."""
    rec = regions._data()["state_by_code"].get(st)
    polys = rec.get("polys") if rec else None
    if not polys:
        return None
    return {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else {"type": "MultiPolygon", "coordinates": polys}


def county_label(county: dict[str, Any]) -> str:
    """"Lee County, FL", "Orleans Parish, LA", "Mayagüez Municipio, PR", "Richmond city, VA"."""
    name, st = county["name"], county["state"]
    if st == "LA":
        label = f"{name} Parish"
    elif st == "PR":
        label = f"{name} Municipio"
    elif st in ("AK", "DC", "VI", "GU", "AS", "MP"):
        label = name
    elif _is_city(county):
        label = name if name.lower().endswith("city") else f"{name} city"
    else:
        label = f"{name} County"
    return f"{label}, {st}"


# --- report metadata ------------------------------------------------------------------------------------

_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_DATE = (r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|"
         r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})")
_TIME = r"(?:(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])\.?\s*m\b\.?|(?P<noon>noon|midnight))"
_TZ = r"(?P<tz>E[DS]T|C[DS]T|M[DS]T|P[DS]T|AK[DS]T|H[DS]T|A[DS]T|ChST|SST|ET|CT|MT|PT)\b"
_AS_OF = [
    re.compile(r"as\s+of\s+" + _DATE + r"(?:,?\s*(?:at\s+)?" + _TIME + r")?(?:\s*\(?" + _TZ + r")?", re.I),
    # time first, possibly with a second local time: "as of 6:00 a.m. EDT / 8:00 p.m. ChST on July 8, 2026"
    re.compile(r"as\s+of\s+" + _TIME + r"\s*(?:" + _TZ + r")?(?:\s*(?:\([^()]{0,40}\)|/[^,;()/]{0,40}?))?,?\s*(?:on\s+)?" + _DATE,
               re.I),
]
_AS_OF_ISO = re.compile(r"as\s+of\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)", re.I)
_DATE_ONLY = re.compile(r"^\s*" + _DATE + r"\s*$", re.I)
_TZ_OFFSETS = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8, "AKDT": -8,
               "AKST": -9, "HST": -10, "HDT": -9, "AST": -4, "ADT": -3, "CHST": 10, "SST": -11}
_ZONES = {"ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "PT": "America/Los_Angeles"}


def _zone_offset(zone: str, naive: datetime) -> timedelta:
    try:
        from zoneinfo import ZoneInfo

        off = naive.replace(tzinfo=ZoneInfo(_ZONES[zone])).utcoffset()
        if off is not None:
            return off
    except Exception:  # no tz database on this system: US daylight time is roughly March-October
        pass
    base = {"ET": -5, "CT": -6, "MT": -7, "PT": -8}[zone]
    return timedelta(hours=base + (1 if 3 < naive.month < 11 else 0))


def _build_time(m: re.Match[str]) -> datetime | None:
    g = m.groupdict()
    try:
        month = _MONTHS[g["mon"][:3].lower()]
        hour, minute = 0, 0
        if g.get("noon"):
            hour = 12 if g["noon"].lower() == "noon" else 0
        elif g.get("hour"):
            hour, minute = int(g["hour"]) % 12, int(g.get("minute") or 0)
            if g["ampm"].lower() == "p":
                hour += 12
        naive = datetime(int(g["year"]), month, int(g["day"]), hour, minute)
    except (KeyError, ValueError, TypeError):
        return None
    tz = (g.get("tz") or "ET").upper()
    offset = timedelta(hours=_TZ_OFFSETS[tz]) if tz in _TZ_OFFSETS else _zone_offset(tz if tz in _ZONES else "ET", naive)
    return (naive - offset).replace(tzinfo=timezone.utc)


def report_as_of(text: str) -> datetime | None:
    """The report's "as of <date> at <time> <zone>" as UTC (Eastern time when no zone is given); falls back to a
    date line at the top of the report."""
    for rx in _AS_OF:
        m = rx.search(text)
        if m and (t := _build_time(m)):
            return t
    m = _AS_OF_ISO.search(text)
    if m and (t := parse_time(m.group(1).replace(" ", "T"))):
        return t
    for line in text.split("\n")[:15]:
        m = _DATE_ONLY.match(line)
        if m and (t := _build_time(m)):
            return t
    return None


_TITLE = re.compile(r"Communications\s+Status\s+Report\s+for\s+(?:the\s+)?(?:Areas?\s+Impacted\s+by\s+)?(?P<name>[^\n]+)", re.I)


def report_event_name(text: str) -> str | None:
    m = _TITLE.search(text)
    if not m:
        return None
    name = re.split(r"\s+(?:as of|on)\s+|[.;]\s", m.group("name"))[0]
    name = re.sub(r"[\s\d*]+$", "", name).strip(" ,:-")  # footnote markers
    return name or None


_WIRELINE_TOTAL = re.compile(
    r"(?:cable|wireline)[^.]{0,120}?reported\s+(?:a\s+total\s+of\s+)?(?P<n>\d[\d,]*)\s+subscribers?\s+out[\s-]+of[\s-]+service",
    re.I)


# --- report parsing ---------------------------------------------------------------------------------------


def _pct(value: Any) -> float | None:
    if value is None:
        return None
    return num(str(value).replace("%", "").strip())


def cell_severity(pct: float | None, out: int | None) -> Severity:
    if pct is None:
        return Severity.minor if out else Severity.info
    if pct >= 50:
        return Severity.extreme
    if pct >= 25:
        return Severity.severe
    if pct >= 10:
        return Severity.moderate
    return Severity.minor if pct > 0 or out else Severity.info


def count_severity(n: int) -> Severity:
    if n >= 50000:
        return Severity.extreme
    if n >= 10000:
        return Severity.severe
    if n >= 1000:
        return Severity.moderate
    return Severity.minor if n > 0 else Severity.info


def psap_severity(status: str) -> Severity:
    s = status.lower()
    if re.search(r"\b(down|out of service|not operational|inoperable|offline|evacuated)\b", s):
        return Severity.severe
    if re.search(r"without\s+(ali|ani|location)|no\s+(ali|ani)|degraded", s):
        return Severity.moderate
    return Severity.minor


def _fmt_pct(pct: float) -> str:
    return f"{pct:.0f}" if pct >= 10 or pct == int(pct) else f"{pct:.1f}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(text)).strip("-")[:60]


def _is_total(*cells: str) -> bool:
    return any(re.search(r"\btotals?\b|\ball\s+(counties|parishes|areas)\b", c or "", re.I) for c in cells)


class _Report:
    def __init__(self, blocks: list[Block], doc_id: str | None, url: str | None, states: list[str]):
        self.blocks = blocks
        self.doc_id = doc_id
        self.url = url
        self.default_states = [s for s in states if s]
        self.text = "\n".join(b[1] if b[0] == "p" else "\n".join(" ".join(r) for r in b[1]) for b in blocks)
        self.table_types: set[str] = set()
        self.as_of = report_as_of(self.text)
        self.event_name = report_event_name(self.text)
        self.cells: dict[str, Event] = {}
        self.wireline: dict[str, Event] = {}
        self.psaps: dict[str, Event] = {}
        self.discovered = False  # reached through an event page rather than named in ``reports``
        self.page_date: datetime | None = None  # date in the fcc.gov/document/ page address, when there was one

    @property
    def is_report(self) -> bool:
        return bool(self.cells or self.wireline or self.psaps or self.table_types)

    def _common(self) -> dict[str, Any]:
        return {"as_of": self.as_of.isoformat() if self.as_of else None, "report": self.doc_id,
                "event_name": self.event_name}

    def _source_line(self) -> str:
        when = self.as_of.strftime("%b %d, %Y %H:%M UTC") if self.as_of else "date unknown"
        what = f" ({self.event_name})" if self.event_name else ""
        return f"FCC DIRS communications status report{what}, as of {when}."

    # tables -------------------------------------------------------------------------------------------

    def read_tables(self, include_zero: bool) -> None:
        last: dict[str, Any] | None = None  # header of the previous table, for continuations without a header
        carry: str | None = None
        for kind, payload in self.blocks:
            if kind != "t":
                continue
            header: dict[str, Any] | None = None
            skip = False
            for idx, row in enumerate(payload):
                if skip:
                    skip = False
                    continue
                kinds = _header_kinds(row)
                if not kinds and idx + 1 < len(payload) and _header_ish(row) and _header_ish(payload[idx + 1]):
                    # two-row header, e.g. "Cell Sites Out Due to" spanning "Damage | Transport | Power"
                    below = payload[idx + 1]
                    merged = [(below[i] if i < len(below) else "") or (row[i] if i < len(row) else "")
                              for i in range(max(len(row), len(below)))]
                    kinds = _header_kinds(merged)
                    skip = bool(kinds)
                if kinds:
                    header = {"kinds": kinds, "type": _table_type(kinds)}
                    self.table_types.add(header["type"])
                    if last is None or last["type"] != header["type"]:
                        carry = None
                    last = header
                    continue
                if header is None:
                    if last is None or abs(len(row) - len(last["kinds"])) > 1:
                        continue
                    header = last  # a table continued after a page break without its header
                try:
                    carry = self._row(header, row, carry, include_zero)
                except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
                    continue  # never let one odd row sink the report

    def _row(self, header: dict[str, Any], row: list[str], carry: str | None, include_zero: bool) -> str | None:
        kinds: list[str | None] = header["kinds"]
        if len(row) == len(kinds) - 1 and kinds[0] == "state" and not _is_num_token(row[0]) and len(row) > 1 \
                and kinds[1] == "county" and state_code(row[0]) is None:
            row = [""] + row  # state column left out of a continuation row
        cell = {k: (row[i] if i < len(row) else "") for i, k in enumerate(kinds) if k}
        state_raw, county_raw = cell.get("state", ""), cell.get("county", "")
        if _is_total(state_raw, county_raw):
            return carry
        st = state_code(state_raw) if state_raw else carry
        if state_raw and st is None:
            return carry  # not a state: a section label or stray text
        carry = st
        typ = header["type"]
        if typ == "cells":
            self._cell_row(st, county_raw, cell, include_zero)
        elif typ == "wireline":
            self._wireline_row(st, cell)
        elif typ == "psap":
            self._psap_row(st, county_raw, cell)
        return carry

    def _find_county(self, st: str | None, name: str) -> dict[str, Any] | None:
        if st:
            return lookup_county(st, name)
        found = [c for s in self.default_states if (c := lookup_county(s, name))]
        return found[0] if len(found) == 1 else None

    def _cell_row(self, st: str | None, county_raw: str, cell: dict[str, str], include_zero: bool) -> None:
        if not county_raw:
            return  # state subtotal line
        county = self._find_county(st, county_raw)
        if county is None:
            return
        served, out = to_int(cell.get("served")), to_int(cell.get("out"))
        pct = _pct(cell.get("percent"))
        if served and out is not None:
            pct = out / served * 100
        elif out is None and served and pct is not None:
            out = round(served * pct / 100)
        if out is None and pct is None:
            return
        if not include_zero and not (out or pct):
            return
        ev_id = f"cell-{county['fips']}"
        if ev_id in self.cells:
            return
        area = county_label(county)
        if pct is not None and served:
            head = f"{_fmt_pct(pct)}% ({out:,} of {served:,})"
        elif pct is not None:
            head = f"{_fmt_pct(pct)}%" + (f" ({out:,})" if out is not None else "")
        else:
            head = f"{out:,}"
        causes = {k: to_int(cell.get(k)) for k in ("damage", "transport", "power", "backup")}
        detail = [f"{causes[k]:,} {label}" for k, label in
                  (("damage", "damage"), ("transport", "transport/backhaul"), ("power", "power")) if causes[k] is not None]
        of_served = f" of {served:,}" if served is not None else ""
        lines = [f"{out:,}{of_served} cell sites out of service" if out is not None else f"{head} of cell sites out of service"]
        lines[0] += f": {', '.join(detail)}." if detail else "."
        if causes["backup"]:
            lines.append(f"{causes['backup']:,} other sites are up but running on back-up power.")
        lines.append(self._source_line())
        lines.append("Share of cell sites out is not the share of customers without service: networks overlap.")
        self.cells[ev_id] = Event(
            id=ev_id,
            category=Category.comms,
            title=f"Cell sites out: {head} — {area}",
            severity=cell_severity(pct, out),
            description="\n".join(lines),
            area=area,
            geometry=regions.county_geometry(county),
            updated_at=self.as_of,
            url=self.url,
            states=[county["state"]],
            fips=county["fips"],
            metrics={
                "kind": "dirs_cell_sites",
                "served": served,
                "out": out,
                "percent": round(pct, 2) if pct is not None else None,
                "damage": causes["damage"],
                "transport": causes["transport"],
                "power": causes["power"],
                "backup_power": causes["backup"],
                **self._common(),
            },
        )

    def _wireline_row(self, st: str | None, cell: dict[str, str]) -> None:
        n = to_int(cell.get("subscribers"))
        if not st or n is None or n <= 0 or f"wireline-{st}" in self.wireline:
            return
        self._wireline_event(st, n)

    def _wireline_event(self, st: str, n: int) -> None:
        name = regions.state_name(st) or st
        self.wireline[f"wireline-{st}"] = Event(
            id=f"wireline-{st}",
            category=Category.comms,
            title=f"Cable/wireline subscribers out: {n:,} — {name}",
            severity=count_severity(n),
            description="Cable and wireline subscribers out of service (telephone, TV and/or internet).\n"
                        + self._source_line(),
            area=name,
            geometry=state_geometry(st),
            updated_at=self.as_of,
            url=self.url,
            states=[st],
            metrics={"kind": "dirs_wireline", "subscribers_out": n, **self._common()},
        )

    def _psap_row(self, st: str | None, county_raw: str, cell: dict[str, str]) -> None:
        name = clean_text(cell.get("psap")) or (f"{county_raw} PSAP" if county_raw else None)
        status = clean_text(cell.get("status")) or "Affected"
        if not name or not st:
            return
        county = lookup_county(st, county_raw) if county_raw else None
        if county is None:  # "Yancey County 911", "Lee County Sheriff's Office"
            m = re.match(r"^(.+?\b(?:county|parish))\b", name, re.I)
            county = lookup_county(st, m.group(1)) if m else None
        ev_id = f"psap-{st}-{_slug(name)}"
        if ev_id in self.psaps:
            return
        if county:
            geom, where = regions.county_geometry(county), county_label(county)
        else:  # no geometry rather than a point that would be attributed to an arbitrary county
            geom, where = None, (regions.state_name(st) or st)
        self.psaps[ev_id] = Event(
            id=ev_id,
            category=Category.comms,
            title=f"911 center affected: {name} ({status}) — {where}",
            severity=psap_severity(status),
            description=f"PSAP status: {status}.\n" + self._source_line(),
            area=where,
            geometry=geom,
            updated_at=self.as_of,
            url=self.url,
            states=[st],
            fips=county["fips"] if county else None,
            metrics={"kind": "dirs_psap", "psap": name, "status": status, **self._common()},
        )

    def wireline_from_text(self) -> None:
        """Report-wide cable/wireline total when there is no per-state table."""
        if self.wireline:
            return
        m = _WIRELINE_TOTAL.search(self.text)
        n = to_int(m.group("n")) if m else None
        if not n:
            return
        states = sorted({e.states[0] for e in self.cells.values()} | {e.states[0] for e in self.psaps.values()})
        if len(states) == 1:
            self._wireline_event(states[0], n)
            return
        where = self.event_name or "DIRS area"
        ev_id = f"wireline-{_slug(self.event_name)}" if self.event_name else "wireline-total"
        self.wireline[ev_id] = Event(
            id=ev_id,
            category=Category.comms,
            title=f"Cable/wireline subscribers out: {n:,} — {where}" + (f" ({', '.join(states)})" if states else ""),
            severity=count_severity(n),
            description="Cable and wireline subscribers out of service across the report's area (not broken down by state).\n"
                        + self._source_line(),
            area=where,
            geometry=None,
            updated_at=self.as_of,
            url=self.url,
            states=states,
            metrics={"kind": "dirs_wireline", "subscribers_out": n, **self._common()},
        )


def parse_dirs_report(
    payload: bytes | str | list[Block],
    *,
    doc_id: str | None = None,
    url: str | None = None,
    states: list[str] | None = None,
    include_zero: bool = False,
    include_wireline: bool = True,
    include_psaps: bool = True,
) -> list[Event]:
    """County cell-site events (plus state wireline and PSAP events) from one DIRS report (.docx bytes, .txt
    text, or already extracted blocks). ``states`` places counties when the table has no State column."""
    return _read_report(payload, doc_id=doc_id, url=url, states=states, include_zero=include_zero,
                        include_wireline=include_wireline, include_psaps=include_psaps)[1]


def _read_report(payload: bytes | str | list[Block], **kw: Any) -> tuple[_Report, list[Event]]:
    blocks = payload if isinstance(payload, list) else document_blocks(payload)
    rep = _Report(blocks, kw.get("doc_id"), kw.get("url"), list(kw.get("states") or []))
    rep.read_tables(bool(kw.get("include_zero")))
    events = list(rep.cells.values())
    if kw.get("include_wireline", True):
        rep.wireline_from_text()
        events += list(rep.wireline.values())
    if kw.get("include_psaps", True):
        events += list(rep.psaps.values())
    return rep, events


# --- discovery --------------------------------------------------------------------------------------------

_ANCHOR = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_HEADING = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]\s*>", re.I | re.S)
_DOC_PAGE = re.compile(r"(?:https?://(?:www\.)?fcc\.gov)?/document/([a-z0-9\-]+)", re.I)
_SLUG_DATE = re.compile(r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-(\d{1,2})-(\d{4})(?:-\d+)?$")
_LINK_DATE = re.compile(_DATE + r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
_STATUS_REPORT = re.compile(r"status\s+reports?", re.I)


def _strip_tags(fragment: str) -> str:
    return _norm_ws(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def doc_links(page: str) -> list[tuple[int, bool, int, bool]]:
    """(DOC number, looks like a status report, number of attachment links, link text is a date) for every
    DOC-nnnnnnA1 attachment linked or named on a page, newest first. A link counts as a report when its text, the
    text just before it in the same list item/paragraph, or the nearest heading above it says "status report"."""
    found: dict[int, list[Any]] = {}
    for m in _ANCHOR.finditer(page):
        href, inner = html_lib.unescape(m.group(1)), m.group(2)
        d = DOC_RE.search(href) or DOC_RE.search(inner)
        if not d:
            continue
        before = page[max(0, m.start() - 400):m.start()]
        cut = max(before.rfind(t) for t in ("</a>", "</li>", "</p>", "</h", "<li", "<p", "<tr", "</td>"))
        # the link text plus the text leading up to it within the same list item / paragraph
        context = _strip_tags((before[cut:] if cut >= 0 else before) + " " + inner)
        headings = list(_HEADING.finditer(page, max(0, m.start() - 6000), m.start()))
        heading = _strip_tags(headings[-1].group(1)) if headings else ""
        entry = found.setdefault(int(d.group(1)), [False, 0, False])
        entry[0] = entry[0] or bool(_STATUS_REPORT.search(context) or _STATUS_REPORT.search(heading))
        entry[1] += 1
        entry[2] = entry[2] or bool(_LINK_DATE.search(_strip_tags(inner)))
    for d in DOC_RE.finditer(re.sub(r"<[^>]+>", " ", page)):
        found.setdefault(int(d.group(1)), [False, 0, False])
    return [(n, v[0], v[1], v[2]) for n, v in sorted(found.items(), key=lambda kv: -kv[0])]


def _slug_date(slug: str) -> datetime | None:
    m = _SLUG_DATE.search(slug)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def report_pages(page: str) -> list[dict[str, Any]]:
    """Links to fcc.gov/document/... pages of communications status reports, newest per event first."""
    groups: dict[str, dict[str, Any]] = {}
    for order, m in enumerate(_DOC_PAGE.finditer(html_lib.unescape(page))):
        slug = m.group(1).lower()
        if "status-report" not in slug:
            continue
        date = _slug_date(slug)
        group = _SLUG_DATE.sub("", slug).replace("communications-status-report", "").strip("-") or "report"
        cand = {"url": "https://www.fcc.gov/document/" + slug, "slug": slug, "date": date, "group": group, "order": order}
        cur = groups.get(group)
        if cur is None or (date and (cur["date"] is None or date > cur["date"])):
            groups[group] = cand
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(groups.values(), key=lambda c: (-(c["date"] or far_past).timestamp(), c["order"]))


def attachment_url(doc: int | str, ext: str, attachment: int = 1) -> str:
    return f"{ATTACHMENTS}DOC-{int(doc)}A{attachment}.{ext}"


def _bounded(value: Any, default: int, lo: int, hi: int) -> int:
    n = to_int(value)
    return default if n is None else max(lo, min(hi, n))


@register
class FccDirs(Source):
    type = "fcc_dirs"
    default_name = "FCC DIRS cell sites out"
    category = Category.comms
    default_interval = 1800  # the FCC publishes about once a day during an activation
    note = "FCC daily report; only during DIRS activations"

    def config_error(self) -> str | None:
        if not (self.options.get("reports") or self.options.get("event_pages")):
            return "not configured: set reports or event_pages"
        return super().config_error()

    def _list(self, key: str) -> list[str]:
        value = self.options.get(key) or []
        return [str(v) for v in ([value] if isinstance(value, str) else value) if str(v).strip()]

    def _formats(self) -> list[str]:
        fmts = [str(f).lower().lstrip(".") for f in self._list("formats")] or list(FORMATS)
        return [f for f in fmts if f in FORMATS] or list(FORMATS)

    def _stale(self, when: datetime | None, slack_hours: float = 0) -> bool:
        max_age = self._max_age()
        if not when or max_age <= 0:
            return False
        return when < utcnow() - timedelta(hours=max_age + slack_hours)

    async def _get(self, url: str) -> bytes:
        try:
            resp = await self.ctx.http.get(url, headers=self.options.get("headers") or {})
        except Exception as exc:  # httpx.HTTPError and friends
            raise SourceError(f"{type(exc).__name__} fetching {url}") from exc
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {url}")
        return resp.content

    def _max_age(self) -> float:
        max_age = num(self.options.get("max_age_hours"))
        return 48.0 if max_age is None else max_age

    async def fetch(self) -> list[Event]:
        sem = asyncio.Semaphore(4)
        errors: list[str] = []  # everything that went wrong; raised when no report could be read
        notes: list[str] = []  # the subset that points at a report layout we do not understand
        explicit = [self._explicit(r) for r in self._list("reports")]
        pages = self._list("event_pages")[:_bounded(self.options.get("max_event_pages"), 10, 1, 25)]
        found = await asyncio.gather(*(self._discover(p, sem, errors) for p in pages))
        targets = [(t, False) for t in explicit] + [(t, True) for group in found for t in group]
        targets = targets[:_bounded(self.options.get("max_reports"), 4, 1, 12)]
        results = await asyncio.gather(*(self._load(t, sem, errors, notes, discovered) for t, discovered in targets))
        loaded = [r for r in results if r is not None]
        if not loaded and errors:
            raise SourceError("; ".join(list(dict.fromkeys(notes + errors))[:3]))
        fresh: list[tuple[_Report, list[Event], datetime | None]] = []
        undated: list[str] = []
        for rep, events in loaded:
            when, slack = rep.as_of, 0.0
            if when is None and rep.page_date is not None:
                when, slack = rep.page_date, 24.0  # the page address has the date only
            if when is None and self._max_age() > 0:
                if rep.discovered:
                    undated.append(rep.doc_id or "report")  # cannot tell how old it is: never keep it forever
                    continue
                notes.append(f"{rep.doc_id or rep.url or 'report'}: no 'as of' time found; kept only because it is "
                             "named in reports (remove it when the activation ends)")
            if not self._stale(when, slack):
                fresh.append((rep, events, when))
        # Only the newest report of each event counts: a county restored in today's report (so missing from its
        # table) must not keep yesterday's outage from an older report of the same event.
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        latest: dict[str, tuple[_Report, list[Event], datetime | None]] = {}
        for idx, item in enumerate(fresh):
            rep = item[0]
            key = _slug(rep.event_name) if rep.event_name else (rep.doc_id or f"#{idx}")
            if key not in latest or (item[2] or oldest) >= (latest[key][2] or oldest):
                latest[key] = item  # later entries win ties
        merged: dict[str, Event] = {}
        for _, events, _ in sorted(latest.values(), key=lambda x: x[2] or oldest):
            for ev in events:
                merged[ev.id] = ev  # between different events, the newer report wins a shared county
        if undated and not merged:
            raise SourceError(f"{undated[0]}: no 'as of' time found in the report, so it is ignored while "
                              "max_age_hours is set (layout changed?)")
        for note in dict.fromkeys(notes):
            log.warning("%s: %s", self.id, note)
        return list(merged.values())

    def _explicit(self, ref: str) -> list[tuple[str, Any]]:
        ref = ref.strip()
        m = DOC_RE.search(ref)
        if ref.lower().startswith(("http://", "https://")):
            if m and "fcc.gov" in ref.lower():
                ext = ref.rsplit(".", 1)[-1].lower()
                return [("doc", (int(m.group(1)), int(m.group(2)), ext if ext in FORMATS else None))]
            return [("url", ref)]
        if m and re.fullmatch(r"DOC-\d{5,7}A\d{1,2}", ref, re.I):
            return [("doc", (int(m.group(1)), int(m.group(2)), None))]
        return [("file", ref)]

    async def _discover(self, page_url: str, sem: asyncio.Semaphore, errors: list[str]) -> list[list[tuple[str, Any]]]:
        """Report candidates on an event page: DOC links introduced as status reports (newest first), else the
        newest fcc.gov/document/ report page per event, else any DOC link (date-labelled links first, then newest;
        each is checked once fetched)."""
        try:
            async with sem:
                page = (await self._get(page_url)).decode("utf-8", "replace")
        except SourceError as exc:
            errors.append(str(exc))
            return []
        docs = doc_links(page)
        max_candidates = _bounded(self.options.get("max_candidates"), 3, 1, 6)
        flagged = [d[0] for d in docs if d[1]]
        max_pages = _bounded(self.options.get("max_document_pages"), 4, 1, 10)
        listed = report_pages(page)
        pages = [("page", c["url"]) for c in listed if not self._stale(c["date"], slack_hours=24)][:max_pages]
        if flagged:  # one event: its newest report, falling back to its newest report page
            return [[("doc", (n, 1, None)) for n in flagged[:max_candidates]] + pages[:1]]
        if listed:  # one per event (a tag page lists several); none when they are all old
            return [[pg] for pg in pages]
        if docs:
            ranked = sorted(docs, key=lambda d: (not d[3], -d[0]))
            return [[("guess", (d[0], 1, None)) for d in ranked[:max_candidates]]]
        return []

    async def _load(self, target: list[tuple[str, Any]], sem: asyncio.Semaphore, errors: list[str],
                    notes: list[str], discovered: bool) -> tuple[_Report, list[Event]] | None:
        for kind, ref in target:
            try:
                if kind == "page":
                    got = await self._load_page(ref, sem, errors, notes)
                elif kind in ("doc", "guess"):
                    # a DOC named in reports or introduced as a status report must be one; a guess may not be
                    got = await self._load_doc(*ref, sem=sem, errors=errors, notes=notes, strict=kind == "doc")
                else:
                    if kind == "url":
                        async with sem:
                            data = await self._get(ref)
                    else:
                        path = Path(ref).expanduser()
                        if not path.is_file():
                            raise SourceError(f"report file not found: {ref}")
                        data = path.read_bytes()
                    blocks = self._blocks(data, "report")
                    got = self._build(blocks, None, ref if kind == "url" else None)
                    if got is None:
                        self._no_tables(ref, blocks, errors, notes)
                if got:
                    got[0].discovered = discovered
                    return got
            except SourceError as exc:
                errors.append(str(exc))
        return None

    async def _load_page(self, url: str, sem: asyncio.Semaphore, errors: list[str],
                         notes: list[str]) -> tuple[_Report, list[Event]] | None:
        """A fcc.gov/document/ report page: its own attachments are the DOC linked most often (pdf, docx, txt)."""
        async with sem:
            page = (await self._get(url)).decode("utf-8", "replace")
        ranked = sorted(doc_links(page), key=lambda d: (-d[2], -d[0]))
        docs = [d[0] for d in ranked][:_bounded(self.options.get("max_candidates"), 3, 1, 6)]
        for n in docs:
            got = await self._load_doc(n, 1, None, sem, errors, notes, strict=False)
            if got:
                got[0].page_date = _slug_date(url.rstrip("/").rsplit("/", 1)[-1])
                return got
        msg = (f"{url}: no DIRS report among its attachments ({', '.join(f'DOC-{n}A1' for n in docs)})" if docs
               else f"{url}: no DOC attachment links on the report page (page layout changed?)")
        errors.append(msg)
        notes.append(msg)
        return None

    async def _load_doc(self, n: int, attachment: int, ext: str | None, sem: asyncio.Semaphore,
                        errors: list[str], notes: list[str], strict: bool = True) -> tuple[_Report, list[Event]] | None:
        doc_id = f"DOC-{n}A{attachment}"
        blocks = await self._doc_blocks(n, attachment, ext, sem, errors)
        if blocks is None:
            return None
        link = self.options.get("link") or attachment_url(n, "pdf", attachment)
        got = self._build(blocks, doc_id, link)
        titled = _titled(blocks)
        if attachment == 1 and ((got is None and titled) or (got and "cells" not in got[0].table_types)):
            # a status report without the county table: it may be released as a second attachment
            more = await self._doc_blocks(n, 2, ext, sem, None)
            if more:
                got = self._build(blocks + more, doc_id, link) or got
        if got is None and (titled or strict):
            self._no_tables(doc_id, blocks, errors, notes)
        elif got and titled and "cells" not in got[0].table_types:
            notes.append(f"{doc_id}: status report read, but no county cell-site table was recognised (layout changed?)")
        return got

    async def _doc_blocks(self, n: int, attachment: int, ext: str | None, sem: asyncio.Semaphore,
                          errors: list[str] | None) -> list[Block] | None:
        """Blocks of the first readable format of an attachment (.docx, then .txt, or ``ext`` first). A response
        that is not that format (an HTML error or block page served with 200) is passed over for the next one."""
        formats = self._formats()
        if ext in FORMATS:
            formats = [ext] + [f for f in formats if f != ext]
        for fmt in formats:
            url = attachment_url(n, fmt, attachment)
            what = f"DOC-{n}A{attachment}.{fmt}"
            try:
                async with sem:
                    data = await self._get(url)
                problem = _format_problem(data, fmt)
                if problem:
                    raise SourceError(f"{what}: not a {fmt} file ({problem})")
                return self._blocks(data, what)
            except SourceError as exc:
                if errors is not None:
                    errors.append(str(exc))
        return None

    @staticmethod
    def _no_tables(what: str, blocks: list[Block], errors: list[str], notes: list[str]) -> None:
        if _titled(blocks):
            msg = f"{what}: status report found but no county/wireline/PSAP table recognised (layout changed?)"
            notes.append(msg)
        else:
            msg = f"no DIRS report tables found in {what}"
        errors.append(msg)

    @staticmethod
    def _blocks(data: bytes, what: str) -> list[Block]:
        try:
            return document_blocks(data)
        except ValueError as exc:
            raise SourceError(f"{what}: {exc}") from exc

    def _build(self, blocks: list[Block], doc_id: str | None, link: str | None) -> tuple[_Report, list[Event]] | None:
        rep, events = _read_report(
            blocks, doc_id=doc_id, url=link or self.options.get("link"), states=self.cfg.states,
            include_zero=bool(self.options.get("include_zero")),
            include_wireline=self.options.get("include_wireline", True) is not False,
            include_psaps=self.options.get("include_psaps", True) is not False,
        )
        return (rep, events) if rep.is_report else None


def _titled(blocks: list[Block]) -> bool:
    """Whether the document opens with a "Communications Status Report for ..." title."""
    return any(b[0] == "p" and _TITLE.search(b[1]) for b in blocks[:10])


_HTML_START = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<(?:!doctype\s+html|html|head|body|\?xml)", re.I)


def _format_problem(data: bytes, fmt: str) -> str | None:
    """Why ``data`` is not a ``fmt`` attachment, or None."""
    html = bool(_HTML_START.match(data[:512]))
    if fmt == "docx" and data[:4] != b"PK\x03\x04":
        return "an HTML page" if html else "no zip signature"
    if fmt == "txt" and html:
        return "an HTML page"
    return None
