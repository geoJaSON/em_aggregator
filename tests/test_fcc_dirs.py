"""FCC DIRS communications status report adapter (emagg/sources/fcc_dirs.py) and its pending catalog.

No real report file could be downloaded (docs.fcc.gov / www.fcc.gov are blocked from the build environment), so the
fixtures are built from what could be confirmed about the published reports (web-search index of the real .txt/.docx
attachments, and repos quoting them). None of them is a recorded FCC file.

* ``dirs_helene_20241001.txt`` is shaped after DOC-406055A1.txt (Hurricane Helene, Oct 1 2024). Traceable to a saved
  source quoting that report (AnanN-36/Healthcare-IT-Project Chris_source_08, 2026): the report time (1 October 2024,
  9:00 a.m. EDT), 21.7% of cell sites out (up from 9.1%), North Carolina 707 of 1,452 out (48.7%; the fixture's 48.69%
  is computed) and 796,999 cable/wireline subscribers out. Seen only in web-search snippets that were not saved: the
  BOM-prefixed title, the "as of October 1, 2024 at 9:00 a.m. EDT" wording and the county table columns (State |
  Affected County | Cell Sites Served | Cell Sites Out | Percent Out | ... Due to Damage | ... Transport | ... Power |
  Cell Sites Up but On Back-up Power). The Buncombe row (348 / 215 / 61.78% / 0 / 89 / 117 / 26) is UNVERIFIED: it was
  noted from a search snippet that could not be reproduced, and FCC-based press figures for Sept 29 (359 sites, 79%
  down) suggest a larger served count; treat it, like every other row, as illustrative. The layout (one table cell per
  line) is one of the two plausible .txt renditions; the tab-delimited one is tested below from the same rows.
* ``dirs_francine_report.docx`` is a minimal WordprocessingML package shaped like the .docx attachment
  ("Percent Cell Sites Out-Of-Service By County/Parish", DOC-405443A1.docx); its numbers are illustrative.
* ``dirs_event_page.html`` / ``dirs_document_page.html``: fcc.gov event and document pages trimmed to their links
  (slugs and DOC numbers are real).
"""

import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
import yaml

import emagg.sources.fcc_dirs  # noqa: F401  (registers fcc_dirs)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.demo import load_text_fixture
from emagg.models import Category, Severity
from emagg.scheduler import attribute
from emagg.sources import REGISTRY, SourceContext, SourceError
from emagg.sources.fcc_dirs import (
    cell_severity,
    column_kind,
    county_label,
    doc_links,
    docx_blocks,
    document_blocks,
    lookup_county,
    parse_dirs_report,
    report_as_of,
    report_event_name,
    report_pages,
    state_code,
    text_blocks,
)
from emagg.store import Store

DATA = Path(catalog.__file__).parent.parent / "demo_data"
PENDING = Path(catalog.__file__).parent / "comms_fcc_dirs.yaml"
TXT = DATA / "dirs_helene_20241001.txt"
DOCX = DATA / "dirs_francine_report.docx"
ATT = "https://docs.fcc.gov/public/attachments/"


def by_id(events):
    return {e.id: e for e in events}


def txt_bytes():
    return TXT.read_bytes()


def docx_bytes():
    return DOCX.read_bytes()


# --- building .docx packages for edge cases --------------------------------------------------------------

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text):
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _tc(text="", span=1, vmerge=None):
    pr = (f'<w:gridSpan w:val="{span}"/>' if span > 1 else "") + (
        '<w:vMerge w:val="restart"/>' if vmerge == "restart" else "<w:vMerge/>" if vmerge == "continue" else "")
    return f"<w:tc>{'<w:tcPr>' + pr + '</w:tcPr>' if pr else ''}{_p(text) if text else '<w:p/>'}</w:tc>"


def _tr(*cells, before=0):
    pr = f'<w:trPr><w:gridBefore w:val="{before}"/></w:trPr>' if before else ""
    return "<w:tr>" + pr + "".join(c if c.startswith("<w:tc>") else _tc(c) for c in cells) + "</w:tr>"


def _tbl(*rows):
    return "<w:tbl>" + "".join(rows) + "</w:tbl>"


def make_docx(*parts: str) -> bytes:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS}><w:body>{"".join(parts)}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


HEADER = ["State", "Affected County", "Cell Sites Served", "Cell Sites Out", "Percent Out",
          "Cell Sites Out Due to Damage", "Cell Sites Out Due to Transport", "Cell Sites Out Due to Power",
          "Cell Sites Up but On Back-up Power"]


# --- .txt (shaped like DOC-406055A1; see the module docstring for which values are verified) -------------------------------------------------------------------


def test_txt_fixture_cell_sites():
    events = by_id(parse_dirs_report(txt_bytes(), doc_id="DOC-406055A1", url=ATT + "DOC-406055A1.pdf"))
    cells = {k: v for k, v in events.items() if v.metrics["kind"] == "dirs_cell_sites"}
    # Sullivan (0 out) and the Total row are left out; McDowell inherits North Carolina; Tennessee rows follow a
    # header repeated after a page break.
    assert set(cells) == {"cell-37021", "cell-37199", "cell-37111", "cell-47179", "cell-47171"}
    b = cells["cell-37021"]
    assert b.title == "Cell sites out: 62% (215 of 348) — Buncombe County, NC"
    assert b.category == Category.comms and b.severity == Severity.extreme
    assert b.states == ["NC"] and b.fips == "37021" and b.area == "Buncombe County, NC"
    assert b.geometry["type"] in ("Polygon", "MultiPolygon")
    assert b.updated_at == datetime(2024, 10, 1, 13, 0, tzinfo=timezone.utc)  # 9:00 a.m. EDT
    assert b.url == ATT + "DOC-406055A1.pdf"
    assert b.metrics == {
        "kind": "dirs_cell_sites", "served": 348, "out": 215, "percent": 61.78, "damage": 0, "transport": 89,
        "power": 117, "backup_power": 26, "as_of": "2024-10-01T13:00:00+00:00", "report": "DOC-406055A1",
        "event_name": "Hurricane Helene",
    }
    assert "89 transport/backhaul" in b.description and "26 other sites are up" in b.description
    assert cells["cell-37111"].states == ["NC"] and cells["cell-37111"].severity == Severity.severe  # 38.96%
    assert cells["cell-47179"].area == "Washington County, TN"  # a county named like a state
    assert cells["cell-47179"].severity == Severity.moderate  # 11.9%
    assert cells["cell-47171"].severity == Severity.severe  # 41.4%


def test_txt_fixture_wireline_and_psaps():
    events = by_id(parse_dirs_report(txt_bytes(), doc_id="DOC-406055A1"))
    wl = events["wireline-hurricane-helene"]  # only an area-wide figure, and the report spans several states
    assert wl.metrics == {"kind": "dirs_wireline", "subscribers_out": 796999, "as_of": "2024-10-01T13:00:00+00:00",
                          "report": "DOC-406055A1", "event_name": "Hurricane Helene"}
    assert wl.states == ["GA", "NC", "TN"] and wl.geometry is None and wl.severity == Severity.extreme
    assert wl.title == "Cable/wireline subscribers out: 796,999 — Hurricane Helene (GA, NC, TN)"
    yancey = events["psap-NC-yancey-county"]
    assert yancey.metrics["status"] == "Rerouted without ALI" and yancey.severity == Severity.moderate
    assert yancey.fips == "37199" and yancey.geometry["type"] in ("Polygon", "MultiPolygon")
    assert "psap-GA-berrien-county" in events
    only_cells = parse_dirs_report(txt_bytes(), include_wireline=False, include_psaps=False)
    assert {e.metrics["kind"] for e in only_cells} == {"dirs_cell_sites"}


def test_txt_include_zero():
    events = by_id(parse_dirs_report(txt_bytes(), include_zero=True))
    sullivan = events["cell-47163"]
    assert sullivan.severity == Severity.info and sullivan.metrics["out"] == 0
    assert sullivan.title == "Cell sites out: 0% (0 of 160) — Sullivan County, TN"


def _delimited(sep: str) -> str:
    rows = [
        HEADER,
        ["North Carolina", "Buncombe", "348", "215", "61.78%", "0", "89", "117", "26"],
        ["", "Yancey", "41", "33", "80.49%", "0", "14", "19", "2"],
        HEADER,  # repeated at a page break
        ["Tennessee", "Unicoi", "29", "12", "41.38%", "2", "4", "6", "0"],
        ["Total", "", "418", "260", "62.20%", "2", "107", "142", "28"],
    ]
    body = "\n".join(sep.join(r) for r in rows)
    return ("\ufeff Communications Status Report for Areas Impacted by Hurricane Helene\n"
            "The following is a summary ... as of October 1, 2024 at 9:00 a.m. EDT.  This report incorporates data.\n\n"
            "Percent Cell Sites Out-Of-Service By County\n" + body + "\n\nCable and wireline companies reported "
            "796,999 subscribers out of service in the disaster area.\n")


@pytest.mark.parametrize("sep", ["\t", " | "])
def test_txt_delimited_rows(sep):
    events = by_id(parse_dirs_report(_delimited(sep)))
    assert {k for k in events if k.startswith("cell-")} == {"cell-37021", "cell-37199", "cell-47171"}
    assert events["cell-37199"].states == ["NC"]  # blank state carried from the row above
    assert events["cell-37021"].metrics["backup_power"] == 26
    assert events["wireline-hurricane-helene"].states == ["NC", "TN"]


def test_txt_aligned_columns_and_prose_with_double_spaces():
    text = (
        "Communications Status Report for Areas Impacted by Hurricane Milton\n"
        "Status as of October 10, 2024, at 10:00 a.m. EDT.  Two spaces after a stop.  Still prose here.\n"
        "State    County      Cell Sites Served   Cell Sites Out   Percent Out\n"
        "FL       Hillsborough      1,200             300          25.0%\n"
        "FL       St. Lucie           400              40          10.0%\n"
    )
    blocks = text_blocks(text)
    assert blocks[1] == ("p", "Status as of October 10, 2024, at 10:00 a.m. EDT. Two spaces after a stop. Still prose here.")
    events = by_id(parse_dirs_report(text))
    assert events["cell-12057"].title == "Cell sites out: 25% (300 of 1,200) — Hillsborough County, FL"
    assert events["cell-12057"].severity == Severity.severe
    assert events["cell-12111"].severity == Severity.moderate
    assert events["cell-12057"].updated_at == datetime(2024, 10, 10, 14, 0, tzinfo=timezone.utc)


# --- .docx ---------------------------------------------------------------------------------------------------


def test_docx_fixture():
    events = by_id(parse_dirs_report(docx_bytes(), doc_id="DOC-405443A1"))
    cells = {k: v for k, v in events.items() if v.metrics["kind"] == "dirs_cell_sites"}
    # vertically merged "Louisiana" cells, zero-out Orleans/Harrison and the gridSpan "Total" row
    assert set(cells) == {"cell-22109", "cell-22057", "cell-22101", "cell-22095", "cell-22007", "cell-28045"}
    t = cells["cell-22109"]
    assert t.title == "Cell sites out: 56% (118 of 212) — Terrebonne Parish, LA" and t.severity == Severity.extreme
    assert t.metrics["backup_power"] is None and t.metrics["power"] == 85
    assert t.updated_at == datetime(2024, 9, 12, 14, 0, tzinfo=timezone.utc)
    assert cells["cell-22095"].area == "St. John the Baptist Parish, LA"
    assert cells["cell-22095"].title.startswith("Cell sites out: 5.6% (4 of 71)")
    assert cells["cell-22095"].severity == Severity.minor
    assert cells["cell-28045"].area == "Hancock County, MS" and cells["cell-28045"].states == ["MS"]
    # per-state wireline table wins over the area-wide sentence
    assert events["wireline-LA"].metrics["subscribers_out"] == 139860 and "wireline-hurricane-francine" not in events
    assert events["wireline-LA"].severity == Severity.extreme and events["wireline-MS"].severity == Severity.moderate
    wl = events["wireline-LA"]
    assert wl.geometry["type"] == "MultiPolygon" and wl.title.endswith("— Louisiana")  # the state's outline
    attribute(wl, [])  # a state-wide figure is not pinned to the county under a point
    assert wl.fips is None and wl.states == ["LA"]
    down = events["psap-LA-st-mary-parish-sheriff-s-office"]
    assert down.severity == Severity.severe and down.fips == "22101" and "(Down)" in down.title
    assert events["psap-LA-terrebonne-parish-911-communications-district"].severity == Severity.moderate


def test_docx_blocks_merges_and_spans():
    data = make_docx(
        _p("Communications Status Report for Areas Impacted by Hurricane Test"),
        _tbl(_tr("State", "County", "Status"),
             _tr(_tc("A", vmerge="restart"), "x", _tc("wide", span=2)),
             _tr(_tc(vmerge="continue"), "y", "z", before=0),
             _tr("w", before=2)),
    )
    blocks = docx_blocks(data)
    assert blocks[0] == ("p", "Communications Status Report for Areas Impacted by Hurricane Test")
    assert blocks[1][1] == [["State", "County", "Status"], ["A", "x", "wide", ""], ["A", "y", "z"], ["", "", "w"]]


def test_docx_two_row_header_split_table_and_names():
    top = ["State", "County", "Cell Sites Served", "Cell Sites Out", "Percent Out", _tc("Cell Sites Out Due to", span=3)]
    sub = ["", "", "", "", "", "Damage", "Transport", "Power"]
    data = make_docx(
        _p("Communications Status Report for Areas Impacted by Hurricane Test"),
        _p("as of 9:00 a.m. CDT, September 12, 2024"),
        _tbl(_tr(*top), _tr(*sub),
             _tr("PR", "Mayagüez Municipio", "50", "30", "60%", "1", "9", "20"),
             _tr("Puerto Rico", "Mayaguez", "50", "25", "50%", "", "", ""),  # duplicate county: first row wins
             _tr("VA", "Richmond city", "100", "10", "10%", "0", "5", "5"),
             _tr("VA", "Richmond County", "20", "1", "5%", "0", "1", "0"),
             _tr("U.S. Virgin Islands", "St. Croix Island", "40", "8", "20%", "0", "0", "8")),
        _p("page break"),
        _tbl(_tr("FL", "Saint Johns", "90", "9", "10.0%", "0", "4", "5"),  # continuation without a header
             _tr("FL", "Miami-Dade", "N/A", "N/A", "N/A", "", "", ""),  # no numbers: skipped
             _tr("FL", "Nowhere", "10", "5", "50%", "0", "0", "5"),  # unknown county: skipped
             _tr("Florida", "DeSoto", "12", "3", "", "0", "0", "3")),  # percent computed
    )
    events = by_id(parse_dirs_report(data))
    assert set(events) == {"cell-72097", "cell-51760", "cell-51159", "cell-78010", "cell-12109", "cell-12027"}
    assert events["cell-72097"].area == "Mayagüez Municipio, PR" and events["cell-72097"].metrics["out"] == 30
    assert events["cell-72097"].metrics["damage"] == 1 and events["cell-72097"].metrics["power"] == 20
    assert events["cell-51760"].area == "Richmond city, VA" and events["cell-51159"].area == "Richmond County, VA"
    assert events["cell-78010"].states == ["VI"] and events["cell-78010"].area == "St. Croix, VI"
    assert events["cell-12027"].metrics["percent"] == 25.0
    assert events["cell-12109"].updated_at == datetime(2024, 9, 12, 14, 0, tzinfo=timezone.utc)  # CDT


def test_table_without_state_column_uses_configured_states():
    text = "County\tCell Sites Served\tCell Sites Out\tPercent Out\nLee\t60\t27\t45%\nCharlotte\t30\t3\t10%\n"
    assert parse_dirs_report(text) == []  # "Lee" exists in many states
    events = by_id(parse_dirs_report(text, states=["FL"]))
    assert events["cell-12071"].title == "Cell sites out: 45% (27 of 60) — Lee County, FL"


def test_bad_input_never_raises_on_rows():
    text = "\t".join(HEADER) + "\n" + "\n".join([
        "North Carolina\tBuncombe\t348\t215\t61.78%\t0\t89\t117\t26",
        "Nonsense\tBuncombe\t1\t1\t1\t1\t1\t1\t1",
        "North Carolina\t\t1,452\t707\t48.69%\t3\t303\t338\t",
        "\t\t\t\t\t\t\t\t",
        "North Carolina\tYancey\tabc\t-\t-\t\t\t\t",
        "North Carolina\tAvery\t0\t0\t0%\t0\t0\t0\t0",
        "x\ty\tz",
    ])
    events = by_id(parse_dirs_report(text))
    assert set(events) == {"cell-37021"}
    assert parse_dirs_report("") == [] and parse_dirs_report(b"random bytes \x00\xff") == []
    with pytest.raises(ValueError):
        document_blocks(b"%PDF-1.7 ...")
    with pytest.raises(ValueError):
        document_blocks(b"PK\x03\x04 not really a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/other.xml", "<x/>")
    with pytest.raises(ValueError):
        docx_blocks(buf.getvalue())


# --- helpers -------------------------------------------------------------------------------------------------


def test_column_kind():
    assert column_kind("State") == "state"
    assert column_kind("Affected County") == "county"
    assert column_kind("County/Parish") == "county" and column_kind("Parish") == "county"
    assert column_kind("Municipio") == "county"
    assert column_kind("Cell Sites Served") == "served" and column_kind("Total Cell Sites") == "served"
    assert column_kind("Cell Sites Out") == "out"
    assert column_kind("Percent Cell Sites Out") == "percent" and column_kind("% Out") == "percent"
    assert column_kind("Cell Sites Out Due to Damage") == "damage"
    assert column_kind("Cell Sites Out Due to Transport") == "transport"
    assert column_kind("Cell Sites Out Due to Power") == "power"
    assert column_kind("Cell Sites Up but On Back-up Power") == "backup"
    assert column_kind("Total Subscribers Out of Service") == "subscribers"
    assert column_kind("PSAP Name") == "psap" and column_kind("Status") == "status"
    assert column_kind("Percent Cell Sites Out-Of-Service By County") is None  # a table title
    assert column_kind("Buncombe") is None and column_kind("348") is None and column_kind("") is None


def test_report_as_of_variants():
    utc = timezone.utc
    assert report_as_of("as of October 1, 2024 at 9:00 a.m. EDT") == datetime(2024, 10, 1, 13, 0, tzinfo=utc)
    assert report_as_of("impacted by Hurricane Beryl as of July 10, 2024, at 10:00 a.m. EDT.") == \
        datetime(2024, 7, 10, 14, 0, tzinfo=utc)
    assert report_as_of("as of 3:30 p.m. CST, January 24, 2026") == datetime(2026, 1, 24, 21, 30, tzinfo=utc)
    assert report_as_of("as of Sept. 30, 2024 at noon") == datetime(2024, 9, 30, 16, 0, tzinfo=utc)  # ET, daylight
    assert report_as_of("as of January 24, 2026 at 9 am") == datetime(2026, 1, 24, 14, 0, tzinfo=utc)  # ET, standard
    assert report_as_of("as of March 16, 2018 at 10:00 a.m. AST") == datetime(2018, 3, 16, 14, 0, tzinfo=utc)
    assert report_as_of("Some title\nOctober 1, 2024\nbody") == datetime(2024, 10, 1, 4, 0, tzinfo=utc)
    assert report_as_of("(down from 9.1% as of yesterday)") is None
    assert report_as_of("as of 2026-09-27T15:00:00Z") == datetime(2026, 9, 27, 15, 0, tzinfo=utc)  # demo placeholders
    assert report_event_name("\ufeff Communications Status Report for Areas Impacted by Hurricane Helene\nx") == \
        "Hurricane Helene"
    assert report_event_name("1 Communications Status Report for Areas Impacted by Tropical Storm Marco and "
                             "Hurricane Laura1") == "Tropical Storm Marco and Hurricane Laura"


def test_states_counties_labels():
    assert state_code("North Carolina") == "NC" and state_code("nc") == "NC"
    assert state_code("U.S. Virgin Islands") == "VI" and state_code("Puerto Rico") == "PR"
    assert state_code("District of Columbia") == "DC" and state_code("Total") is None and state_code("XX") is None
    assert lookup_county("LA", "St. John the Baptist Parish")["fips"] == "22095"
    assert lookup_county("LA", "Saint Tammany")["fips"] == "22103"
    assert lookup_county("MO", "St. Louis City")["fips"] == "29510" and lookup_county("MO", "St. Louis")["fips"] == "29189"
    assert lookup_county("MD", "Baltimore city")["fips"] == "24510"
    assert lookup_county("VA", "James City")["fips"] == "51095"
    assert lookup_county("NV", "Carson City")["fips"] == "32510"
    assert lookup_county("NM", "Dona Ana County")["fips"] == "35013"
    assert lookup_county("FL", "") is None and lookup_county("", "Lee") is None
    assert county_label(regions.county_by_fips("32510")) == "Carson City, NV"
    assert county_label(regions.county_by_fips("02020")) == "Anchorage, AK"
    assert county_label(regions.county_by_fips("11001")).endswith(", DC")


def test_cell_severity_thresholds():
    assert cell_severity(50, 5) == Severity.extreme and cell_severity(49.9, 5) == Severity.severe
    assert cell_severity(25, 5) == Severity.severe and cell_severity(10, 5) == Severity.moderate
    assert cell_severity(9.99, 5) == Severity.minor and cell_severity(0.1, 1) == Severity.minor
    assert cell_severity(0, 0) == Severity.info and cell_severity(None, 3) == Severity.minor


# --- discovery -----------------------------------------------------------------------------------------------


def test_discovery_links():
    event_page = (DATA / "dirs_event_page.html").read_text()
    assert doc_links(event_page) == [(406255, False, 1, False)]  # a news release under "News Releases"
    pages = report_pages(event_page)
    assert [p["url"] for p in pages] == ["https://www.fcc.gov/document/hurricane-helene-communications-status-report-oct-1-2024"]
    doc_page = (DATA / "dirs_document_page.html").read_text()
    assert sorted(doc_links(doc_page), key=lambda d: -d[2])[0] == (406055, True, 3, False)  # under the report title
    listing = """<ul>
      <li><a href="/document/hurricane-milton-communications-status-report-october-12-2024">Milton</a></li>
      <li><a href="/document/hurricane-milton-communications-status-report-october-11-2024">Milton</a></li>
      <li><a href="https://www.fcc.gov/document/hurricane-helene-communications-status-report-oct-19-2024">Helene</a></li>
      <li><a href="/document/fcc-activates-disaster-information-reporting-system-hurricane-milton">DIRS activated</a></li>
      <li>Communications Status Report, Oct 5: <a href="https://docs.fcc.gov/public/attachments/DOC-406400A1.pdf">PDF</a>
          <a href="https://docs.fcc.gov/public/attachments/DOC-406400A1.docx">Word</a></li>
    </ul>"""
    assert [p["group"] for p in report_pages(listing)] == ["hurricane-helene", "hurricane-milton"]
    assert report_pages(listing)[1]["url"].endswith("october-12-2024")
    assert doc_links(listing) == [(406400, True, 2, False)]
    # links labelled only by a date, under a "Communications Status Reports" heading
    dated = """<h2>Communications Status Reports</h2>
      <ul><li><a href="https://docs.fcc.gov/public/attachments/DOC-405443A1.pdf">September 12, 2024</a></li></ul>
      <h2>News Releases</h2>
      <ul><li><a href="https://docs.fcc.gov/public/attachments/DOC-406999A1.pdf">FCC Chairwoman statement</a></li></ul>"""
    assert doc_links(dated) == [(406999, False, 1, False), (405443, True, 1, True)]


def run(cfg: SourceConfig, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
            src = REGISTRY[cfg.type](cfg, SourceContext(http, AreaConfig(), Store()))
            assert src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def site(seen=None, overrides=None):
    files = {
        "https://www.fcc.gov/helene": (DATA / "dirs_event_page.html").read_bytes(),
        "https://www.fcc.gov/document/hurricane-helene-communications-status-report-oct-1-2024":
            (DATA / "dirs_document_page.html").read_bytes(),
        ATT + "DOC-406055A1.txt": txt_bytes(),
        ATT + "DOC-406120A1.pdf": b"%PDF-1.7",
        ATT + "DOC-405443A1.docx": docx_bytes(),
    }
    files.update(overrides or {})

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        body = files.get(url)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404, text="not found")

    return handler


def cfg(**options):
    return SourceConfig.model_validate({"id": "fcc_dirs", "type": "fcc_dirs", **options})


def test_fetch_from_event_page():
    seen = []
    events = by_id(run(cfg(event_pages=["https://www.fcc.gov/helene"], max_age_hours=0), site(seen)))
    assert "cell-37021" in events and events["cell-37021"].metrics["report"] == "DOC-406055A1"
    assert events["cell-37021"].url == ATT + "DOC-406055A1.pdf"
    # event page -> newest report page -> .docx (404) -> .txt; older report pages and PDFs are never requested
    assert seen == [
        "https://www.fcc.gov/helene",
        "https://www.fcc.gov/document/hurricane-helene-communications-status-report-oct-1-2024",
        ATT + "DOC-406055A1.docx",
        ATT + "DOC-406055A1.txt",
    ]


def test_fetch_inactive_or_stale_is_empty():
    seen = []
    # the fixture report is from 2024: with the default max_age_hours the dated report page is not even fetched
    assert run(cfg(event_pages=["https://www.fcc.gov/helene"]), site(seen)) == []
    assert seen == ["https://www.fcc.gov/helene"]
    # an explicit report older than max_age_hours is read but yields nothing
    assert run(cfg(reports=["DOC-405443A1"]), site()) == []
    # no report links at all (DIRS not active)
    quiet = {"https://www.fcc.gov/tags/disaster-information-reporting-system": b"<html><a href='/news'>x</a></html>"}
    assert run(cfg(event_pages=["https://www.fcc.gov/tags/disaster-information-reporting-system"]), site(overrides=quiet)) == []


def test_fetch_explicit_reports_and_merge(tmp_path):
    local = tmp_path / "report.txt"
    local.write_bytes(txt_bytes())
    seen = []
    events = by_id(run(cfg(reports=["DOC-405443A1", str(local), ATT + "DOC-406055A1.txt"], max_age_hours=0), site(seen)))
    assert events["cell-22109"].metrics["report"] == "DOC-405443A1"
    # the local copy and the URL are the same report (same as-of time): later entries win ties
    assert events["cell-37021"].metrics["report"] == "DOC-406055A1"
    assert ATT + "DOC-406055A1.txt" in seen and ATT + "DOC-406055A1.docx" not in seen  # .txt URL asked for first
    assert "wireline-LA" in events and "wireline-hurricane-helene" in events


def test_fetch_newer_report_wins():
    older = _delimited("\t").replace("October 1, 2024", "September 30, 2024").replace("215", "250")
    files = {ATT + "DOC-406000A1.txt": older.encode()}
    events = by_id(run(cfg(reports=["DOC-406055A1", "DOC-406000A1"], formats=["txt"], max_age_hours=0), site(overrides=files)))
    assert events["cell-37021"].metrics["out"] == 215 and events["cell-37021"].metrics["report"] == "DOC-406055A1"


def test_fetch_direct_doc_links_skip_non_reports():
    page = b"""<ul>
      <li>Communications Status Report - Oct 2: <a href="https://docs.fcc.gov/public/attachments/DOC-405443A1.pdf">PDF</a></li>
      <li><a href="https://docs.fcc.gov/public/attachments/DOC-406999A1.pdf">FCC Chairwoman statement</a></li>
    </ul>"""
    seen = []
    events = run(cfg(event_pages=["https://www.fcc.gov/francine"], max_age_hours=0),
                 site(seen, {"https://www.fcc.gov/francine": page}))
    assert any(e.id == "cell-22109" for e in events)
    assert not any("406999" in u for u in seen)
    # unflagged links are tried newest first, and a document without DIRS tables is passed over
    page2 = b'<a href="https://docs.fcc.gov/public/attachments/DOC-406999A1.pdf">x</a> <a href="/y/DOC-405443A1.pdf">y</a>'
    other = make_docx(_p("FCC Chairwoman visits"), _p("as of October 1, 2024"))
    seen = []
    events = run(cfg(event_pages=["https://www.fcc.gov/x"], max_age_hours=0),
                 site(seen, {"https://www.fcc.gov/x": page2, ATT + "DOC-406999A1.docx": other}))
    assert any(e.id == "cell-22109" for e in events)
    assert not any("A2." in u for u in seen)  # not a status report: no second attachment is looked for


def test_fetch_county_table_in_second_attachment():
    narrative = make_docx(_p("Communications Status Report for Areas Impacted by Hurricane Test"),
                          _p("as of October 1, 2024 at 9:00 a.m. EDT."),
                          _p("Cable and wireline companies reported 1,234 subscribers out of service in the disaster area."))
    table = make_docx(_p("Percent Cell Sites Out-Of-Service By County"),
                      _tbl(_tr(*HEADER), _tr("Florida", "Lee", "60", "27", "45%", "0", "7", "20", "3")))
    seen = []
    events = by_id(run(cfg(reports=["DOC-407000A1"], max_age_hours=0),
                       site(seen, {ATT + "DOC-407000A1.docx": narrative, ATT + "DOC-407000A2.docx": table})))
    lee = events["cell-12071"]
    assert lee.title == "Cell sites out: 45% (27 of 60) — Lee County, FL"
    assert lee.updated_at == datetime(2024, 10, 1, 13, 0, tzinfo=timezone.utc) and lee.metrics["report"] == "DOC-407000A1"
    assert events["wireline-FL"].metrics["subscribers_out"] == 1234  # one state in the report
    assert seen == [ATT + "DOC-407000A1.docx", ATT + "DOC-407000A2.docx"]


def test_fetch_bounded_and_concurrency_limited():
    in_flight, peak, seen = 0, 0, []
    pages = "".join(f'<li><a href="/document/storm{i}-communications-status-report-oct-1-2024">r</a></li>' for i in range(9))

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        seen.append(str(request.url))
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if request.url.path == "/tags/dirs":
            return httpx.Response(200, text=pages)
        return httpx.Response(404)

    with pytest.raises(SourceError):  # every report page 404s
        run(cfg(event_pages=["https://www.fcc.gov/tags/dirs"], max_age_hours=0, max_reports=3), handler)
    assert sum("/document/" in u for u in seen) == 3 and peak <= 4


def test_fetch_errors():
    with pytest.raises(SourceError, match="HTTP 500"):
        run(cfg(event_pages=["https://www.fcc.gov/helene"]),
            site(overrides={"https://www.fcc.gov/helene": httpx.Response(500)}))
    with pytest.raises(SourceError, match="HTTP 404"):
        run(cfg(reports=["DOC-400000A1"]), site())
    with pytest.raises(SourceError, match="PDF"):
        run(cfg(reports=["https://example.org/report.pdf"]), site(overrides={"https://example.org/report.pdf": b"%PDF-1.4"}))
    with pytest.raises(SourceError, match="not found"):
        run(cfg(reports=["/nonexistent/dirs.docx"]), site())
    with pytest.raises(SourceError, match="no DIRS report"):
        run(cfg(reports=["https://example.org/x.txt"]), site(overrides={"https://example.org/x.txt": b"hello"}))


def test_fetch_tag_page_fresh_reports_only():
    today = datetime.now(timezone.utc)
    slug = f"{today:%B}-{today.day}-{today.year}".lower()
    listing = f"""<div class="view-content">
      <a href="/document/hurricane-demo-communications-status-report-{slug}">Hurricane Demo report</a>
      <a href="/document/hurricane-helene-communications-status-report-oct-1-2024">Helene</a>
      <a href="/document/fcc-activates-disaster-information-reporting-system-hurricane-demo">DIRS activated</a>
    </div>""".encode()
    doc_page = b'<a href="https://docs.fcc.gov/public/attachments/DOC-900001A1.txt">Text</a>'
    seen = []
    tag = "https://www.fcc.gov/tags/disaster-information-reporting-system"
    events = by_id(run(cfg(event_pages=[tag]), site(seen, {
        tag: listing,
        f"https://www.fcc.gov/document/hurricane-demo-communications-status-report-{slug}": doc_page,
        ATT + "DOC-900001A1.txt": load_text_fixture("dirs_demo_houston.txt").encode(),
    })))
    assert events["cell-48167"].title == "Cell sites out: 56% (131 of 236) — Galveston County, TX"
    assert events["cell-48201"].severity == Severity.moderate and "cell-48291" not in events  # Liberty: 0 out
    assert events["wireline-TX"].metrics["subscribers_out"] == 412580  # one state in the report
    assert not any("helene" in u or "activates" in u for u in seen)  # 2024 report page and notices are skipped


def test_wireline_total_without_event_name():
    text = "\t".join(HEADER[:5]) + "\nTexas\tHarris\t10\t5\t50%\nLouisiana\tOrleans\t10\t5\t50%\n" \
        "Cable and wireline companies reported 1,000 subscribers out of service in the disaster area.\n"
    events = by_id(parse_dirs_report(text))
    assert events["wireline-total"].states == ["LA", "TX"] and events["wireline-total"].severity == Severity.moderate


def test_registration_and_config():
    src_cls = REGISTRY["fcc_dirs"]
    assert src_cls.category == Category.comms and src_cls.default_interval >= 300
    src = src_cls(cfg(), SourceContext(None, AreaConfig(), None))
    assert src.config_error() == "not configured: set reports or event_pages"
    assert src_cls(cfg(reports="DOC-406055A1"), SourceContext(None, AreaConfig(), None)).config_error() is None


# --- review fixes: layouts, merging, discovery -------------------------------------------------------------


def test_txt_flattened_psaps_with_blank_cells():
    text = "\n".join([
        "Communications Status Report for Areas Impacted by Hurricane Helene",
        "as of October 1, 2024 at 9:00 a.m. EDT.",
        "The following PSAPs were reported as being affected:",
        "State", "County", "PSAP Name", "Status",
        "Georgia", "", "Tri County (Lakeland)", "Rerouted with ALI",  # blank county cell
        "North Carolina", "Yancey", "Yancey County", "Rerouted without ALI",
        "", "Watauga", "Watauga County 911", "Down",  # blank state cell: carried from the row above
        "Georgia", "Berrien", "Berrien County", "Down",
        "",
        "Wireless Services",
        "The following section describes the status of wireless communications services and restoration in the area.",
    ])
    events = by_id(parse_dirs_report(text))
    got = {k: (v.metrics["psap"], v.metrics["status"], v.states, v.fips) for k, v in events.items()}
    assert got == {
        "psap-GA-tri-county-lakeland": ("Tri County (Lakeland)", "Rerouted with ALI", ["GA"], None),
        "psap-NC-yancey-county": ("Yancey County", "Rerouted without ALI", ["NC"], "37199"),
        "psap-NC-watauga-county-911": ("Watauga County 911", "Down", ["NC"], "37189"),
        "psap-GA-berrien-county": ("Berrien County", "Down", ["GA"], "13019"),
    }
    assert events["psap-GA-tri-county-lakeland"].geometry is None  # no county: not pinned to a random one
    assert events["psap-NC-watauga-county-911"].severity == Severity.severe
    assert ("p", "Wireless Services") in text_blocks(text)  # the text after the table stays text
    # without a status column the cells are taken n at a time; a row that lost its state is dropped, not shifted
    flat = "\n".join(["State", "County", "PSAP Name", "Georgia", "Berrien", "Berrien County",
                      "Yancey", "Yancey County", "North Carolina", "Watauga", "Watauga County 911"])
    assert set(by_id(parse_dirs_report(flat))) == {"psap-GA-berrien-county", "psap-NC-watauga-county-911"}
    # a status wording we do not know still reads as fixed groups when every cell is there
    odd = "\n".join(["State", "PSAP Name", "Status", "Georgia", "Berrien County", "Answering on paper",
                     "North Carolina", "Yancey County", "Answering on paper"])
    assert {e.metrics["status"] for e in parse_dirs_report(odd)} == {"Answering on paper"}
    assert set(by_id(parse_dirs_report(odd))) == {"psap-GA-berrien-county", "psap-NC-yancey-county"}


def test_txt_split_header_footnotes_and_two_column_table():
    text = "\n".join([
        "Communications Status Report for Areas Impacted by Hurricane Francine",
        "as of September 12, 2024 at 9:00 a.m. CDT.",
        "State", "County1", "Cell Sites Served", "Cell Sites Out", "Percent Out2",
        "Cell Sites Out Due to", "Damage", "Transport", "Power3",  # a two-row header, flattened
        "Louisiana", "Terrebonne", "212", "118", "55.66%", "2", "31", "85",
        "Lafourche", "150", "60", "40.00%", "0", "20", "40",
        "",
        "State\tSubscribers Out",  # a two-column tab-delimited table
        "Louisiana\t139,860",
        "Mississippi\t4,120",
        "Cable and wireline companies reported 143,980 subscribers out of service in the disaster area.",
    ])
    events = by_id(parse_dirs_report(text))
    t = events["cell-22109"].metrics
    assert (t["served"], t["out"], t["damage"], t["transport"], t["power"]) == (212, 118, 2, 31, 85)
    assert events["cell-22057"].states == ["LA"] and events["cell-22057"].metrics["power"] == 40
    assert events["wireline-LA"].metrics["subscribers_out"] == 139860
    assert events["wireline-MS"].metrics["subscribers_out"] == 4120 and "wireline-hurricane-francine" not in events
    assert column_kind("County1") == "county" and column_kind("Power2") == "power" and column_kind("E911 Center") == "psap"
    tabbed = "State1\tCounty1\tCell Sites Served\tCell Sites Out\tPercent Out\nFlorida\tLee*\t60\t27\t45%\n"
    assert set(by_id(parse_dirs_report(tabbed))) == {"cell-12071"}


def _raw_tr(*cells):
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def test_docx_cell_content_controls_and_nested_table():
    sdt_cell = "<w:sdt><w:sdtPr/><w:sdtContent>" + _tc("Lee") + "</w:sdtContent></w:sdt>"
    nested = _tbl(_tr("note", "x", "y"))
    cell_with_table = "<w:tc>" + _p("Collier") + nested + "<w:p/></w:tc>"
    data = make_docx(
        _p("Communications Status Report for Areas Impacted by Hurricane Test"),
        _tbl(_tr(*HEADER[:5]),
             _raw_tr(_tc("Florida"), sdt_cell, _tc("60"), _tc("27"), _tc("45%")),
             "<w:sdt><w:sdtContent>" + _tr("Florida", "Charlotte", "30", "3", "10%") + "</w:sdtContent></w:sdt>",
             _raw_tr(_tc("Florida"), cell_with_table, _tc("40"), _tc("20"), _tc("50%"))),
    )
    blocks = docx_blocks(data)
    assert blocks[1][1][1:] == [["Florida", "Lee", "60", "27", "45%"], ["Florida", "Charlotte", "30", "3", "10%"],
                                ["Florida", "Collier", "40", "20", "50%"]]
    assert blocks[2] == ("t", [["note", "x", "y"]])  # the nested table is its own block, not rows of the outer one
    assert set(by_id(parse_dirs_report(data))) == {"cell-12071", "cell-12015", "cell-12021"}


def test_report_as_of_second_time_zone_and_lookups():
    utc = timezone.utc
    assert report_as_of("as of 6:00 a.m. EDT (8:00 p.m. ChST), July 7, 2026") == datetime(2026, 7, 7, 10, 0, tzinfo=utc)
    assert report_as_of("as of 6:00 a.m. EDT / 8:00 p.m. ChST on July 8, 2026") == datetime(2026, 7, 8, 10, 0, tzinfo=utc)
    assert report_as_of("as of July 7, 2026 at 6:00 a.m. EDT / 8:00 p.m. ChST") == datetime(2026, 7, 7, 10, 0, tzinfo=utc)
    assert lookup_county("WA", "Island")["fips"] == "53029" and lookup_county("WA", "Island County")["fips"] == "53029"
    assert lookup_county("AS", "Eastern District")["fips"] == "60010"
    assert lookup_county("AS", "Manu'a District")["fips"] == "60020"
    assert lookup_county("IL", "Rock Island")["fips"] == "17161"
    assert lookup_county("MP", "Northern Islands Municipality")["fips"] == "69085"
    assert state_code("Florida*") == "FL"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _report(when, rows, name="Hurricane Test"):
    head = "State\tAffected County\tCell Sites Served\tCell Sites Out\tPercent Out\n"
    as_of = f"The following is a summary as of {_iso(when)}.\n" if when else "The following is a summary.\n"
    return (f"Communications Status Report for Areas Impacted by {name}\n" + as_of + head + "\n".join(rows)).encode()


def test_fetch_only_newest_report_of_an_event_counts():
    now = datetime.now(timezone.utc)
    files = {
        ATT + "DOC-500002A1.txt": _report(now - timedelta(hours=2), ["Florida\tLee\t60\t3\t5%"]),
        ATT + "DOC-500001A1.txt": _report(now - timedelta(hours=26), ["Florida\tLee\t60\t30\t50%", "Florida\tCollier\t40\t20\t50%"]),
        ATT + "DOC-500003A1.txt": _report(now - timedelta(hours=30), ["Florida\tSarasota\t50\t5\t10%"], "Hurricane Other"),
    }
    events = by_id(run(cfg(reports=["DOC-500002A1", "DOC-500001A1", "DOC-500003A1"], formats=["txt"]), site(None, files)))
    # Collier is back in service in today's report of the same event; another event's report still counts
    assert set(events) == {"cell-12071", "cell-12115"}
    assert events["cell-12071"].metrics["out"] == 3 and events["cell-12071"].metrics["report"] == "DOC-500002A1"


def test_fetch_reports_layout_problems_instead_of_empty(caplog):
    now = datetime.now(timezone.utc)
    bad = (f"Communications Status Report for Areas Impacted by Hurricane Test\nas of {_iso(now)}.\n"
           "Area\tTowers\tDown towers\tShare\nFlorida - Lee\t60\t30\t50%\n").encode()
    seen = []
    with pytest.raises(SourceError, match="status report found but no county/wireline/PSAP table recognised"):
        run(cfg(reports=["DOC-500009A1"], formats=["txt"]), site(seen, {ATT + "DOC-500009A1.txt": bad}))
    assert seen == [ATT + "DOC-500009A1.txt", ATT + "DOC-500009A2.txt"]
    # an explicit DOC that is not a status report at all
    with pytest.raises(SourceError, match="no DIRS report tables found in DOC-500008A1"):
        run(cfg(reports=["DOC-500008A1"], formats=["txt"]), site(None, {ATT + "DOC-500008A1.txt": b"Public Notice"}))
    # soft 404: an HTML page served with 200 at the .docx address is passed over for the .txt
    seen = []
    html = b"<!DOCTYPE html><html><body>Document not found</body></html>"
    events = by_id(run(cfg(reports=["DOC-406055A1"], max_age_hours=0), site(seen, {ATT + "DOC-406055A1.docx": html})))
    assert "cell-37021" in events and seen == [ATT + "DOC-406055A1.docx", ATT + "DOC-406055A1.txt"]
    with pytest.raises(SourceError, match=r"DOC-406055A1\.docx: not a docx file \(an HTML page\)"):
        run(cfg(reports=["DOC-406055A1"], max_age_hours=0),
            site(None, {ATT + "DOC-406055A1.docx": html, ATT + "DOC-406055A1.txt": b"\xef\xbb\xbf <html>blocked</html>"}))
    # a report page (found on the tag page) without any attachment link
    slug = f"{now:%B}-{now.day}-{now.year}".lower()
    tag = "https://www.fcc.gov/tags/disaster-information-reporting-system"
    page = f"https://www.fcc.gov/document/hurricane-test-communications-status-report-{slug}"
    with pytest.raises(SourceError, match="no DOC attachment links"):
        run(cfg(event_pages=[tag]), site(None, {tag: f'<a href="{page}">r</a>'.encode(), page: b"<html>moved</html>"}))
    # a status report whose county table is not recognised still yields its other events, with a warning
    narrative = (f"Communications Status Report for Areas Impacted by Hurricane Test\nas of {_iso(now)}.\n"
                 "Cable and wireline companies reported 1,234 subscribers out of service in the disaster area.\n").encode()
    with caplog.at_level("WARNING", logger="emagg.sources.fcc_dirs"):
        events = run(cfg(reports=["DOC-500007A1"], formats=["txt"]), site(None, {ATT + "DOC-500007A1.txt": narrative}))
    assert [e.metrics["kind"] for e in events] == ["dirs_wireline"]
    assert "no county cell-site table was recognised" in caplog.text


def test_fetch_undated_discovered_report_is_not_kept_forever(caplog):
    event = "https://www.fcc.gov/test"
    page = (b'<li>Communications Status Report: <a href="https://docs.fcc.gov/public/attachments/DOC-500010A1.txt">'
            b'Text</a></li>')
    files = {event: page, ATT + "DOC-500010A1.txt": _report(None, ["Florida\tLee\t60\t30\t50%"])}
    with pytest.raises(SourceError, match="no 'as of' time"):
        run(cfg(event_pages=[event]), site(None, files))
    assert {e.id for e in run(cfg(event_pages=[event], max_age_hours=0), site(None, files))} == {"cell-12071"}
    # named explicitly in reports: kept (the operator chose it), with a warning
    with caplog.at_level("WARNING", logger="emagg.sources.fcc_dirs"):
        assert {e.id for e in run(cfg(reports=["DOC-500010A1"], formats=["txt"]), site(None, files))} == {"cell-12071"}
    assert "DOC-500010A1: no 'as of' time found" in caplog.text
    # reached through a dated report page: the page's date stands in for the missing as-of time
    now = datetime.now(timezone.utc)
    slug = f"{now:%B}-{now.day}-{now.year}".lower()
    doc_page = f"https://www.fcc.gov/document/hurricane-test-communications-status-report-{slug}"
    files.update({event: f'<a href="{doc_page}">today</a>'.encode(),
                  doc_page: b'<a href="https://docs.fcc.gov/public/attachments/DOC-500010A1.txt">Text</a>'})
    assert {e.id for e in run(cfg(event_pages=[event]), site(None, files))} == {"cell-12071"}


def test_fetch_prefers_date_labelled_links_when_none_is_flagged():
    news = "".join(f'<a href="https://docs.fcc.gov/public/attachments/DOC-40699{i}A1.pdf">FCC news {i}</a> '
                   for i in range(3))
    page = (news + '<a href="https://docs.fcc.gov/public/attachments/DOC-405443A1.pdf">Sept. 12, 2024</a>').encode()
    seen = []
    events = run(cfg(event_pages=["https://www.fcc.gov/x"], max_age_hours=0), site(seen, {"https://www.fcc.gov/x": page}))
    assert any(e.id == "cell-22109" for e in events)
    assert seen[1] == ATT + "DOC-405443A1.docx"  # tried before the three newer, unlabelled documents


# --- catalog -------------------------------------------------------------------------------------------------


def test_pending_catalog_entries_are_valid():
    entries = yaml.safe_load(PENDING.read_text())
    assert isinstance(entries, list) and len(entries) == 1
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "comms_fcc_dirs.yaml"}
    valid_states = regions.state_codes()
    seen = set()
    for e in entries:
        sid = e["id"]
        assert sid not in seen and sid not in existing and sid.startswith("fcc_")
        seen.add(sid)
        assert e["type"] in REGISTRY and e["type"] == "fcc_dirs"
        sc = SourceConfig.model_validate(_interpolate(e))
        assert all(len(st) == 2 and st.isupper() and st in valid_states for st in sc.states)
        assert not sc.states  # national
        assert sc.meta["confidence"] in ("high", "medium", "low") and sc.meta["evidence"] and sc.meta["notes"]
        if sc.meta["confidence"] == "high":
            assert ";" in sc.meta["evidence"]
        assert sc.enabled is False and sc.meta["confidence"] == "low"  # discovery page not verified live
        src = REGISTRY[sc.type](sc, SourceContext(None, AreaConfig(), None))
        assert src.config_error() is None
        assert all(u.startswith("https://www.fcc.gov/") for u in sc.options["event_pages"])
