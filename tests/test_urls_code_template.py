"""Code and SpideringTemplate reach URL Analysis: hand-off, Excel upload, pasted data, results, search UI and every export."""
import io
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer.analysis import BookRecord, analyze_urls, export_analysis_results, parse_book_data, parse_pasted, read_excel_records
from url_analyzer.backlog.web import create_blueprint
from url_analyzer.services.url_analysis_service import UrlAnalysisService
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

TEMPLATES = str(Path(__file__).parents[1] / "templates")
U1, U2 = "https://a.gov/one.pdf", "https://b.gov/page"
ROUTES = lambda: {U1: FakeResp(200, text_pdf(ENGLISH, 2), PDF_HEADERS),                       # noqa: E731
                  U2: FakeResp(200, b"<html>no links</html>", {"Content-Type": "text/html"})}

UI = pd.DataFrame({"DocumentId": ["D1", "D2"], "Code": ["DE--ONE--REG", "FR--TWO--REG"],
                   "SpideringTemplate": ["DE--ONE--REG-SPIDER", "FR-TEMPLATE-2"], "BookCategory": ["PDF", "PDF"]})
PBI = pd.DataFrame({"BookSourceId": ["D1", "D2"], "BookTitle": ["One", "Two"], "API Result": "Change", "Link to the Issuance": [U1, U2]})


@pytest.fixture
def env(tmp_path):
    analyzer, _ = make_analyzer(ROUTES(), cache_ttl=0)
    svc = UrlAnalysisService(tmp_path / "o", analyzer)
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(create_blueprint(tmp_path / "bu", tmp_path / "bo"))
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "o", tmp_path / "u", service=svc))
    return type("E", (), {"c": app.test_client(), "tmp": tmp_path, "analyzer": analyzer})


def wait(c, jid):
    events = []
    for _ in range(200):
        s = c.get(f"/api/url-analysis/{jid}/status?from={len(events)}").get_json()
        events += s["events"]
        if s["state"] != "running":
            return {e["idx"]: e for e in events}
        time.sleep(0.02)
    raise AssertionError("timeout")


def backlog_job(e, with_code=True):
    ui = UI if with_code else UI.drop(columns=["Code", "SpideringTemplate"])
    u, p = e.tmp / "StartPointStatus-01-Oct-2026.csv", e.tmp / "Details_Table_-_2026-10-01T010101_000.xlsx"
    ui.to_csv(u, index=False); PBI.to_excel(p, index=False)
    rid = e.c.post("/api/backlog/generate", data={"ui": (io.BytesIO(u.read_bytes()), u.name), "powerbi": (io.BytesIO(p.read_bytes()), p.name)},
                   content_type="multipart/form-data").get_json()["job_id"]
    for _ in range(200):
        if e.c.get(f"/api/backlog/status/{rid}").get_json()["state"] != "running":
            break
        time.sleep(0.05)
    sent = e.c.post(f"/api/backlog/{rid}/send-to-url-tool", json={"rows": [0, 1]}).get_json()
    items = e.c.get(f"/api/transfer/{sent['transfer_id']}").get_json()["items"]
    jid = e.c.post("/api/url-analysis/analyze", json={"mode": "items", "items": items}).get_json()["job_id"]
    return items, jid


def read_xlsx(resp):
    assert resp.status_code == 200 and resp.data[:2] == b"PK"
    return pd.read_excel(io.BytesIO(resp.data), dtype=object).fillna("")


# ── Daily Backlog -> URL Analysis ────────────────────────────────────────────
def test_backlog_handoff_carries_code_and_template(env):
    items, jid = backlog_job(env)
    assert [(i["document_id"], i["code"], i["spidering_template"]) for i in items] == [
        ("D1", "DE--ONE--REG", "DE--ONE--REG-SPIDER"), ("D2", "FR--TWO--REG", "FR-TEMPLATE-2")]
    ev = wait(env.c, jid)
    assert {(e["code"], e["spidering_template"]) for e in ev.values()} == {("DE--ONE--REG", "DE--ONE--REG-SPIDER"), ("FR--TWO--REG", "FR-TEMPLATE-2")}


def test_columns_are_in_every_url_analysis_download(env):
    _, jid = backlog_job(env)
    wait(env.c, jid)
    full = read_xlsx(env.c.get(f"/api/url-analysis/{jid}/download"))
    assert "Code" in full.columns and "SpideringTemplate" in full.columns
    cols = list(full.columns)                                                                       # next to the book's own columns
    assert cols.index("Book Type") < cols.index("Code") < cols.index("SpideringTemplate") < cols.index("HTTP Status")
    assert dict(zip(full["Document ID"], full["Code"])) == {"D1": "DE--ONE--REG", "D2": "FR--TWO--REG"}
    sel = read_xlsx(env.c.post(f"/api/url-analysis/{jid}/download", json={"indices": [1]}))        # Download Selected
    assert sel["Document ID"].tolist() == ["D2"] and sel.loc[0, "SpideringTemplate"] == "FR-TEMPLATE-2"


def test_columns_are_left_out_when_no_book_has_them(env):
    _, jid = backlog_job(env, with_code=False)
    wait(env.c, jid)
    assert not {"Code", "SpideringTemplate"} & set(read_xlsx(env.c.get(f"/api/url-analysis/{jid}/download")).columns)


def test_cached_results_keep_each_books_own_code(tmp_path):
    analyzer, _ = make_analyzer(ROUTES())                                              # caching ON
    first = analyze_urls([BookRecord(url=U1, code="C-A", spidering_template="T-A")], analyzer=analyzer)[0]
    second = analyze_urls([BookRecord(url=U1, code="C-B", spidering_template="T-B")], analyzer=analyzer)[0]
    assert (first.code, second.code, second.spidering_template, second.cached) == ("C-A", "C-B", "T-B", True)


# ── Excel upload ─────────────────────────────────────────────────────────────
def excel(tmp_path, **cols):
    p = tmp_path / "books.xlsx"
    pd.DataFrame({"Document ID": ["X1", "X2"], "Book Title": ["A", "B"], "URL": [U1, U2], **cols}).to_excel(p, index=False)
    return p


def test_excel_detects_code_and_template_columns_and_exports_them(env):
    p = excel(env.tmp, Code=["c1", "c2"], SpideringTemplate=["t1", "t2"], Region=["UK", "US"])
    ins = env.c.post("/api/url-analysis/excel/inspect", data={"file": (io.BytesIO(p.read_bytes()), "books.xlsx")}, content_type="multipart/form-data").get_json()
    assert (ins["code_column"], ins["template_column"], ins["id_column"]) == ("Code", "SpideringTemplate", "Document ID")
    jid = env.c.post("/api/url-analysis/excel/analyze", json={"upload_id": ins["upload_id"], "url_column": "URL", "id_column": "Document ID",
                     "title_column": "Book Title", "code_column": ins["code_column"], "template_column": ins["template_column"]}).get_json()["job_id"]
    ev = wait(env.c, jid)
    assert {(e["code"], e["spidering_template"]) for e in ev.values()} == {("c1", "t1"), ("c2", "t2")}
    assert "Code" not in next(iter(ev.values()))["extra"] and next(iter(ev.values()))["extra"] == {"Region": ev[0]["extra"]["Region"]}
    df = read_xlsx(env.c.get(f"/api/url-analysis/{jid}/download"))
    assert df["Code"].tolist() == ["c1", "c2"] and df["SpideringTemplate"].tolist() == ["t1", "t2"] and "Region" in df.columns
    assert df.columns.tolist().count("Code") == 1                                      # not duplicated as a "source" column


def test_excel_columns_can_be_chosen_by_hand_or_left_out(tmp_path):
    p = excel(tmp_path, Ref=["r1", "r2"], Tpl=["t1", "t2"])
    d = read_excel_records(str(p), "URL", "Document ID", "Book Title", "Ref", "Tpl")
    assert [(r.code, r.spidering_template) for r in d.records] == [("r1", "t1"), ("r2", "t2")] and d.source_columns == []
    none = read_excel_records(str(p), "URL", "Document ID", "Book Title")
    assert [(r.code, r.spidering_template) for r in none.records] == [("", ""), ("", "")] and none.source_columns == ["Ref", "Tpl"]
    from url_analyzer.analysis import InputError
    with pytest.raises(InputError, match="does not exist"):
        read_excel_records(str(p), "URL", code_column="Nope")


def test_code_header_is_no_longer_mistaken_for_the_document_id(tmp_path):
    p = tmp_path / "c.xlsx"
    pd.DataFrame({"Document ID": ["X1"], "Code": ["C1"], "URL": [U1]}).to_excel(p, index=False)
    from url_analyzer.analysis import inspect_excel
    ins = inspect_excel(str(p))
    assert (ins.id_column, ins.code_column) == ("Document ID", "Code")


# ── pasted book data ─────────────────────────────────────────────────────────
def test_pasted_book_data_with_code_and_template_headers():
    text = "Document ID\tBook Title\tCode\tSpideringTemplate\tURL\nD1\tOne\tC1\tT1\t" + U1 + "\n"
    rec = parse_pasted(text).items[0]
    assert (rec.document_id, rec.book_title, rec.code, rec.spidering_template, rec.url) == ("D1", "One", "C1", "T1", U1)
    assert rec.extra == {}                                                              # mapped, not left over as an extra column
    assert parse_book_data("Code\tURL\nC9\t" + U1).items[0].code == "C9"


# ── CLI ──────────────────────────────────────────────────────────────────────
def test_cli_exports_the_columns(tmp_path, monkeypatch):
    import url_analyzer.main  # noqa: F401  (the package re-exports a function named `main`)
    cli = sys.modules["url_analyzer.main"]
    analyzer, _ = make_analyzer(ROUTES(), cache_ttl=0)
    monkeypatch.setattr(cli, "URLAnalyzer", lambda settings: analyzer)
    src = excel(tmp_path, Ref=["r1", "r2"], Tpl=["t1", "t2"])
    out = tmp_path / "out.xlsx"
    cli.process(str(src), str(out), log_dir=str(tmp_path / "logs"), id_column="Document ID", title_column="Book Title",
                code_column="Ref", template_column="Tpl")
    df = pd.read_excel(out, dtype=object).fillna("")
    assert df["Code"].tolist() == ["r1", "r2"] and df["SpideringTemplate"].tolist() == ["t1", "t2"]


# ── the page ─────────────────────────────────────────────────────────────────
def test_url_analysis_page_has_the_search_boxes_and_selectors(env):
    html = env.c.get("/url-analysis").get_data(as_text=True)
    for needle in ('id="code-q"', 'id="tpl-q"', 'id="xl-code"', 'id="xl-tpl"', "r.spidering_template"):
        assert needle in html, needle
    assert "Code</th><th>SpideringTemplate" in html                                    # hand-off preview table
