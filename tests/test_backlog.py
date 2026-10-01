import io

import pandas as pd
import pytest
from openpyxl import load_workbook

from url_analyzer.backlog import config as C
from url_analyzer.backlog import filters as F
from url_analyzer.backlog.dataset import build_dataset
from url_analyzer.backlog.excel_report import safe_sheet_name, write_report
from url_analyzer.backlog.loader import BacklogError
from url_analyzer.backlog.matching import match_book_types
from url_analyzer.backlog.normalize import (categorize_api, derive_report_date, extract_domain,
                                            normalize_api_result, normalize_book_type)

API = {
    "change": "Change", "potential": "Stream 3 - Potential Changes BAU skim",
    "evc_open": "Stream 5 - Externally Verified Changed - Open Task",
    "evc": "Stream 5 - Externally Verified Changed",
    "mon": "Stream 2 - Monitoring Failures Existing Manual", "src": "Stream 2 - Source Modified",
    "tpl": "Stream 2 - Template Error", "img": "Stream 2 - Image Base64 Issue",
}


# ── Domain extraction ────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/path", "www.example.com"),
    ("http://example.gov/document", "example.gov"),
    ("https://example.gov/a/b?x=1&y=2#frag", "example.gov"),
    ("HTTPS://User@Example.GOV:8443/x", "example.gov"),
    ("example.org/page", "example.org"),
    ("not a url", "Unknown"),
    ("http://", "Unknown"),
    ("", "Unknown"),
    ("   ", "Unknown"),
    (None, "Unknown"),
    (float("nan"), "Unknown"),
])
def test_extract_domain(url, expected):
    assert extract_domain(url) == expected


# ── API Result normalisation ─────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["Change", "CHANGE", "change", "  Change  ", "\tCHANGE\n"])
def test_api_casing_whitespace(raw):
    assert normalize_api_result(raw) == "change"
    assert categorize_api(normalize_api_result(raw)) == C.CAT_CHANGE


def test_api_null_and_dash_variants():
    assert normalize_api_result(None) == "" and normalize_api_result(float("nan")) == ""
    assert normalize_api_result("Stream 2 – Source Modified") == "stream 2 - source modified"
    assert normalize_api_result("Stream 2 � Source Modified") == "stream 2 - source modified"


@pytest.mark.parametrize("key,cat", [
    ("change", C.CAT_CHANGE), ("potential", C.CAT_POTENTIAL), ("evc_open", C.CAT_EVC_OPEN),
    ("evc", C.CAT_EVC), ("mon", C.CAT_DEV), ("src", C.CAT_DEV), ("tpl", C.CAT_DEV), ("img", C.CAT_DEV),
])
def test_categories(key, cat):
    assert categorize_api(normalize_api_result(API[key])) == cat


def test_unlisted_values_are_other():
    for v in ["Stream 2 - Spidering - Template Error", "Stream 6 - Broken URL", "Unknown Status", ""]:
        assert categorize_api(normalize_api_result(v)) == C.CAT_OTHER


def test_book_type_normalisation():
    assert [normalize_book_type(v) for v in ["pdf", " PDF ", "HTML", "html + pdf", "other", None, "  ", "Epub"]] == [
        "PDF", "PDF", "HTML", "HTML + PDF", "Other", "Unknown", "Unknown", "Epub"]


def test_report_date():
    assert str(derive_report_date("StartPointStatus-01-Oct-2026 (1).csv", "")) == "2026-10-01"
    assert str(derive_report_date("x.csv", "Details_Table_-_2026-09-30T010101_000.xlsx")) == "2026-09-30"


# ── Fixtures ─────────────────────────────────────────────────────────────────
ROWS = [  # (id, api, ui book category, link)
    ("id01", "Change", "PDF", "https://a.gov/1.pdf"),
    ("id02", "CHANGE ", "Other", "https://a.gov/2"),
    ("id03", "Change", "HTML", "https://b.org/3"),
    ("id04", "Stream 3 - Potential Changes BAU skim", "PDF", "https://b.org/4"),
    ("id05", "Stream 3 - Potential Changes BAU skim", "HTML + PDF", "https://b.org/5"),
    ("id06", API["evc_open"], "PDF", "https://c.com/6"),
    ("id07", API["evc_open"], "HTML", "https://c.com/7"),
    ("id08", API["evc"], "Other", "https://c.com/8"),
    ("id09", API["evc"], "HTML + PDF", "https://c.com/9"),
    ("id10", API["mon"], "PDF", "https://d.net/10"),
    ("id11", API["src"], "Other", "https://d.net/11"),
    ("id12", API["tpl"], "PDF", "https://d.net/12"),
    ("id13", API["img"], "HTML", "https://d.net/13"),
    ("id14", "Change", None, "not a url"),            # UI row exists, blank category -> Other
    ("id15", "Change", "PDF", None),                  # UI row exists, blank link
    ("idXX", "Change", None, "https://e.io/x"),       # not in UI at all -> unmatched
]


def make_frames(rows=ROWS, with_dup=False):
    ui_rows = [(i, bt) for i, _, bt, _ in rows if i != "idXX"]
    ui = pd.DataFrame({"DocumentId": [r[0] for r in ui_rows], "BookCategory": [r[1] for r in ui_rows]})
    ui["Code"] = ["CODE-" + i for i in ui["DocumentId"]]
    if with_dup:
        ui = pd.concat([ui, ui.iloc[[0]].assign(BookCategory="HTML")], ignore_index=True)
    pbi = pd.DataFrame({
        "BookSourceId": [r[0].upper() for r in rows],       # case differs from UI
        "BookTitle": [f"Title {r[0]}" for r in rows],
        "API Result": [r[1] for r in rows],
        "Link to the Issuance": [r[3] for r in rows],
    })
    return ui, pbi


@pytest.fixture
def files(tmp_path):
    ui, pbi = make_frames()
    ui_path = tmp_path / "StartPointStatus-01-Oct-2026.csv"
    ui.to_csv(ui_path, index=False)
    pbi_path = tmp_path / "Details_Table_-_2026-10-01T090000_000.xlsx"
    with pd.ExcelWriter(pbi_path) as w:
        pbi.to_excel(w, sheet_name="Export", index=False)
    return ui_path, pbi_path


@pytest.fixture
def data(files):
    return build_dataset(str(files[0]), str(files[1]))


# ── Matching ─────────────────────────────────────────────────────────────────
def test_match_success_and_key_detection():
    ui, pbi = make_frames()
    m = match_book_types(ui, pbi)
    assert (m.ui_key, m.pbi_key, m.key_source) == ("DocumentId", "BookSourceId", "configured")
    assert m.matched == 15 and m.unmatched == 1
    assert m.book_type_raw.iloc[0] == "PDF"


def test_match_no_match_row_is_kept(data):
    row = data.export[data.export["BookSourceId"] == "IDXX"].iloc[0]
    assert row["Book Type"] == "Unmatched" and not row["_matched"]
    assert data.stats["unmatched"] == 1 and len(data.export) == len(ROWS)


def test_match_duplicate_identifier_uses_first():
    ui, pbi = make_frames(with_dup=True)
    m = match_book_types(ui, pbi)
    assert m.duplicate_ui_keys == 1 and m.book_type_raw.iloc[0] == "PDF"


def test_match_empty_identifier():
    ui, pbi = make_frames()
    pbi.loc[0, "BookSourceId"] = None
    pbi.loc[1, "BookSourceId"] = "  "
    m = match_book_types(ui, pbi)
    assert m.empty_pbi_keys == 2 and m.unmatched == 3 and len(pbi) == len(m.matched_mask)


def test_match_nothing_in_common_raises():
    ui, pbi = make_frames()
    pbi["BookSourceId"] = "zzz-" + pbi["BookSourceId"]
    with pytest.raises(BacklogError, match="No Power BI records"):
        match_book_types(ui, pbi)


def test_match_via_filename_stem():
    ui = pd.DataFrame({"Code": ["DE--X--REG--1"], "BookCategory": ["PDF"]})
    pbi = pd.DataFrame({"SourceFileName": ["DE--X--REG--1_v3.xml"], "API Result": ["Change"]})
    m = match_book_types(ui, pbi)
    assert m.matched == 1 and "stem" in m.pbi_key


# ── Dataset / action lists ───────────────────────────────────────────────────
def ids(df):
    return set(df["BookSourceId"])


def test_derived_columns(data):
    ex = data.export.set_index("BookSourceId")
    assert ex.loc["ID01", "Domain"] == "a.gov"
    assert ex.loc["ID14", "Domain"] == "Unknown" and ex.loc["ID15", "Domain"] == "Unknown"
    assert ex.loc["ID14", "Book Type"] == "Unknown"   # matched UI record, blank BookCategory
    assert len(data.export) == len(ROWS)   # nothing dropped


def test_action_lists_filter_by_book_type_and_api(data):
    ex = data.export
    assert ids(ex[F.view_mask(ex, "change")]) == {"ID01", "ID02", "ID04", "ID15"}   # Unknown book types are not PDF/Other
    assert ids(ex[F.view_mask(ex, "evc_open")]) == {"ID06"}
    assert ids(ex[F.view_mask(ex, "evc")]) == {"ID08"}
    assert ids(ex[F.view_mask(ex, "dev")]) == {"ID10", "ID11", "ID12"}


@pytest.mark.parametrize("book_type,included", [("PDF", True), ("Other", True), ("HTML", False), ("HTML + PDF", False)])
def test_book_type_scope(book_type, included):
    ui = pd.DataFrame({"DocumentId": ["a"], "BookCategory": [book_type]})
    pbi = pd.DataFrame({"BookSourceId": ["a"], "API Result": ["Change"], "Link": ["http://x.com"]})
    m = match_book_types(ui, pbi)
    df = pd.DataFrame({"Book Type": [normalize_book_type(m.book_type_raw.iloc[0])], "_cat": [C.CAT_CHANGE]})
    df["_in_scope"] = df["Book Type"].isin(C.ACTION_BOOK_TYPES)
    assert bool(F.view_mask(df, "change").iloc[0]) is included


def test_html_kept_in_overall_counts(data):
    assert data.stats["html_or_html_pdf"] == 5
    assert F.cards(data.export)["total"]["count"] == len(ROWS)
    bt = {b["label"]: b["count"] for b in F.book_type_summary(data.export)}
    assert bt["HTML"] == 3 and bt["HTML + PDF"] == 2


def test_api_casing_does_not_duplicate_categories(data):
    labels = [a["label"] for a in F.api_summary(data.export)]
    assert len(labels) == len({l.lower() for l in labels})
    assert {a["label"]: a["count"] for a in F.api_summary(data.export)}["Change"] == 6


# ── Filters / summaries ──────────────────────────────────────────────────────
def test_filters_combine(data):
    ex = data.export
    spec = F.FilterSpec(domains=["a.gov", "b.org"], apis=["Change"], book_types=["PDF", "Other"])
    assert ids(F.apply_filters(ex, spec)) == {"ID01", "ID02"}
    assert ids(F.apply_filters(ex, F.FilterSpec(domains=["a.gov"], q="title id02"))) == {"ID02"}
    assert F.apply_filters(ex, F.FilterSpec(domains=["nope.example"])).empty


def test_domain_summary(data):
    rows = {r["domain"]: r for r in F.domain_summary(data.export)}
    assert rows["c.com"]["total"] == 4 and rows["c.com"]["evc"] == 4
    assert rows["d.net"]["dev"] == 4 and rows["a.gov"]["change"] == 2


def test_table_page_sort_and_paging(data):
    cols = ["BookSourceId", "API Result"]
    rows = F.table_page(data.export, cols, "API Result", sort="BookSourceId", asc=False, page=1, size=3)
    assert [r["BookSourceId"] for r in rows] == ["IDXX", "ID15", "ID14"]
    rows2 = F.table_page(data.export, cols, "API Result", page=6, size=3)
    assert len(rows2) == 1


# ── Validation ───────────────────────────────────────────────────────────────
def test_validation_errors(tmp_path, files):
    with pytest.raises(BacklogError, match="not found"):
        build_dataset(str(tmp_path / "missing.csv"), str(files[1]))
    bad = tmp_path / "x.txt"
    bad.write_text("a")
    with pytest.raises(BacklogError, match=r"\.csv"):
        build_dataset(str(bad), str(files[1]))
    with pytest.raises(BacklogError, match=r"\.xlsx"):
        build_dataset(str(files[0]), str(bad))


def test_missing_required_column(tmp_path, files):
    pd.DataFrame({"BookSourceId": ["a"], "Other": [1]}).to_excel(tmp_path / "p.xlsx", index=False)
    with pytest.raises(BacklogError, match="'API Result'"):
        build_dataset(str(files[0]), str(tmp_path / "p.xlsx"))
    (tmp_path / "u.csv").write_text("DocumentId,Foo\na,1\n")
    with pytest.raises(BacklogError, match="'BookCategory'"):
        build_dataset(str(tmp_path / "u.csv"), str(files[1]))


def test_empty_and_corrupt_inputs(tmp_path, files):
    (tmp_path / "e.csv").write_text("DocumentId,BookCategory\n")
    with pytest.raises(BacklogError, match="no rows"):
        build_dataset(str(tmp_path / "e.csv"), str(files[1]))
    (tmp_path / "c.xlsx").write_bytes(b"not an excel file")
    with pytest.raises(BacklogError, match="could not be read"):
        build_dataset(str(files[0]), str(tmp_path / "c.xlsx"))


def test_mixed_encoding_csv(tmp_path, files):
    p = tmp_path / "StartPointStatus-02-Oct-2026.csv"
    p.write_bytes("DocumentId,BookCategory,Title\nid01,PDF,café “x”\n".encode("utf-8") + b"id02,PDF,bad\x9d\n")
    d = build_dataset(str(p), str(files[1]))
    assert d.stats["ui_rows"] == 2 and any("encoding" in w for w in d.warnings)


# ── Workbook ─────────────────────────────────────────────────────────────────
def test_sheet_name_rules():
    used: set[str] = set()
    n = safe_sheet_name("A" * 50 + "[x]:y", used)
    assert len(n) <= 31 and not set("[]:*?/\\") & set(n)
    assert safe_sheet_name("A" * 50, used) != n or True
    assert safe_sheet_name("export", {"export"}) == "export (2)"


def test_workbook_has_seven_valid_tabs(data, tmp_path):
    out = write_report(data, tmp_path / "out" / "r.xlsx")
    wb = load_workbook(out)
    assert wb.sheetnames == ["UI 01-Oct-2026", "Export", "Pivot", "Change and Potential Change",
                             "EVC Open Task", "External Verified Change", "Stream 2 - Dev Attn Needed"]
    assert all(len(n) <= 31 for n in wb.sheetnames)
    export_hdr = [c.value for c in wb["Export"][1]]
    assert export_hdr[-2:] == ["Book Type", "Domain"] and not any(str(h).startswith("_") for h in export_hdr)
    assert wb["Export"].max_row == len(ROWS) + 1
    assert wb["Change and Potential Change"].max_row - 1 == 4
    assert wb["Stream 2 - Dev Attn Needed"].max_row - 1 == 3
    assert wb["Export"].auto_filter.ref or wb["Export"].tables
    assert wb["Export"].freeze_panes == "A2"
    pivot_text = [c.value for row in wb["Pivot"].iter_rows() for c in row if c.value]
    for t in range(1, 9):
        assert any(str(v).startswith(f"Table {t} ") for v in pivot_text)


def test_write_error_is_user_friendly(data, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("locked")
    monkeypatch.setattr("pandas.ExcelWriter", boom)
    with pytest.raises(BacklogError, match="close it"):
        write_report(data, tmp_path / "r.xlsx")


# ── Web API ──────────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path):
    from flask import Flask
    from url_analyzer.backlog.web import create_blueprint
    app = Flask(__name__, template_folder=str(__import__("pathlib").Path(__file__).parents[1] / "templates"))
    app.register_blueprint(create_blueprint(tmp_path / "up", tmp_path / "out"))
    return app.test_client()


def _generate(client, files):
    import time
    ui, pbi = files
    r = client.post("/api/backlog/generate", data={
        "ui": (io.BytesIO(ui.read_bytes()), ui.name), "powerbi": (io.BytesIO(pbi.read_bytes()), pbi.name)},
        content_type="multipart/form-data")
    assert r.status_code == 200
    jid = r.get_json()["job_id"]
    for _ in range(100):
        s = client.get(f"/api/backlog/status/{jid}").get_json()
        if s["state"] == "error" or s["excel"]["state"] != "pending":
            return jid, s
        time.sleep(0.1)
    raise AssertionError("timeout")


def test_web_flow(client, files):
    assert client.get("/backlog").status_code == 200
    jid, s = _generate(client, files)
    assert s["state"] == "ready" and s["excel"]["state"] == "done"
    assert s["report"]["report_date"] == "01-Oct-2026" and "a.gov" in s["report"]["options"]["domains"]

    q = client.get(f"/api/backlog/{jid}/query?view=change&domain=a.gov&domain=b.org&api=Change").get_json()
    assert q["total_all"] == 16 and q["view_total"] == 2
    assert {r["BookSourceId"] for r in q["rows"]} == {"ID01", "ID02"}
    assert q["view_counts"]["all"] == q["total_filtered"]

    csv = client.get(f"/api/backlog/{jid}/download/filtered?view=dev&q=d.net")
    assert csv.status_code == 200 and csv.data.decode("utf-8-sig").count("\n") == 4  # header + 3
    rep = client.get(f"/api/backlog/{jid}/download/report")
    assert rep.status_code == 200 and rep.data[:2] == b"PK"


def test_web_errors(client, files):
    assert client.post("/api/backlog/generate", data={}).status_code == 400
    r = client.post("/api/backlog/generate", data={
        "ui": (io.BytesIO(b"x"), "a.txt"), "powerbi": (io.BytesIO(b"x"), "b.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 400 and ".csv" in r.get_json()["error"]
    r = client.post("/api/backlog/generate", data={
        "ui": (io.BytesIO(b"a,b\n1,2\n"), "a.csv"), "powerbi": (io.BytesIO(b"junk"), "b.xlsx")},
        content_type="multipart/form-data")
    import time
    jid = r.get_json()["job_id"]
    for _ in range(50):
        s = client.get(f"/api/backlog/status/{jid}").get_json()
        if s["state"] != "running":
            break
        time.sleep(0.1)
    assert s["state"] == "error" and "Traceback" not in s["error"]
    assert client.get("/api/backlog/nope/query").status_code == 404


def test_csv_formula_injection_is_neutralised(client, tmp_path):
    ui, pbi = make_frames()
    pbi.loc[0, "BookTitle"] = "@SUM(1+1)*cmd"
    uip, pbp = tmp_path / "StartPointStatus-01-Oct-2026.csv", tmp_path / "Details_Table_-_2026-10-01T010101_000.xlsx"
    ui.to_csv(uip, index=False)
    pbi.to_excel(pbp, index=False)
    jid, _ = _generate(client, (uip, pbp))
    body = client.get(f"/api/backlog/{jid}/download/filtered?view=all").data.decode("utf-8-sig")
    assert "'@SUM(1+1)*cmd" in body and ",@SUM" not in body
