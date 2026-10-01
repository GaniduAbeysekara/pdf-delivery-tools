"""Acceptance: Paste URLs, Excel upload, Daily Backlog selection and the CLI all use ONE engine, ONE result model,
ONE dashboard API and ONE exporter. If a second implementation ever appears these tests fail."""
import importlib.util
import io
import re
import time
from pathlib import Path

import pandas as pd
import pytest
import requests
from flask import Flask

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer.analysis.engine import URLAnalyzer
from url_analyzer.backlog.web import create_blueprint
from url_analyzer.services.url_analysis_service import UrlAnalysisService
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

SRC = Path(__file__).parents[1] / "src" / "url_analyzer"
TEMPLATES = str(Path(__file__).parents[1] / "templates")
U1, U2, U3 = "https://example.gov/a.pdf", "https://example.gov/missing", "https://example.gov/page"
ROUTES = lambda: {  # noqa: E731
    U1: FakeResp(200, text_pdf(ENGLISH, 3), {**PDF_HEADERS, "Content-Length": "2048"}),
    U2: FakeResp(404, b"", {"Content-Type": "text/html"}),
    U3: FakeResp(200, b"<html><body>No downloads here</body></html>", {"Content-Type": "text/html"})}
ANALYSIS_FIELDS = ["domain", "http_status", "final_status", "final_url", "redirects", "mime_type", "content_type",
                   "direct_document", "page_count", "file_size_bytes", "file_size", "language", "last_modified",
                   "analysis_status", "error", "status", "url_type", "document_url", "document_link_type",
                   "document_link_count", "document_urls", "text_extraction_status"]
STANDARD = ["Document ID", "Book Title", "URL", "Domain", "HTTP Status", "Final URL", "Content Type", "Direct Document",
            "Page Count", "File Size", "Language", "Last Modified", "Analysis Status", "Error"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    analyzer, sess = make_analyzer(ROUTES(), cache_ttl=0)            # no caching: every path really runs the engine
    fetches = []
    original = URLAnalyzer._fetch_and_inspect
    monkeypatch.setattr(URLAnalyzer, "_fetch_and_inspect", lambda self, url, r, *a, **k: fetches.append(url) or original(self, url, r, *a, **k))
    exports = []
    import url_analyzer.services.url_analysis_service as svc_mod
    real_export = svc_mod.export_analysis_results
    monkeypatch.setattr(svc_mod, "export_analysis_results", lambda *a, **k: exports.append(1) or real_export(*a, **k))
    svc = UrlAnalysisService(tmp_path / "out", analyzer)
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(create_blueprint(tmp_path / "bl_up", tmp_path / "bl_out"))
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out", tmp_path / "up", service=svc))
    return type("Env", (), {"client": app.test_client(), "fetches": fetches, "exports": exports, "tmp": tmp_path,
                            "analyzer": analyzer, "svc": svc})


def wait_job(client, jid):
    events = []
    for _ in range(200):
        s = client.get(f"/api/url-analysis/{jid}/status?from={len(events)}").get_json()
        events += s["events"]
        if s["state"] != "running":
            return s, {e["idx"]: e for e in events}
        time.sleep(0.02)
    raise AssertionError("timeout")


def run_paste(e):
    r = e.client.post("/api/url-analysis/analyze", json={"mode": "auto", "text": "\n".join([U1, U2, U3])})
    return r.get_json()["job_id"]


def run_excel(e):
    p = e.tmp / "books.xlsx"
    pd.DataFrame({"Document ID": ["X1", "X2", "X3"], "Book Title": ["A", "B", "C"], "URL": [U1, U2, U3], "Region": ["UK", "US", "FR"]}).to_excel(p, index=False)
    ins = e.client.post("/api/url-analysis/excel/inspect", data={"file": (io.BytesIO(p.read_bytes()), "books.xlsx")},
                        content_type="multipart/form-data").get_json()
    assert ins["url_column"] == "URL" and ins["confident"]
    r = e.client.post("/api/url-analysis/excel/analyze", json={"upload_id": ins["upload_id"], "url_column": ins["url_column"],
                                                               "id_column": ins["id_column"], "title_column": ins["title_column"]})
    return r.get_json()["job_id"]


def run_backlog(e):
    ui, pbi = e.tmp / "StartPointStatus-01-Oct-2026.csv", e.tmp / "Details_Table_-_2026-10-01T010101_000.xlsx"
    pd.DataFrame({"DocumentId": ["B1", "B2", "B3", "B4"], "BookCategory": ["PDF", "PDF", "Other", "HTML"]}).to_csv(ui, index=False)
    pd.DataFrame({"BookSourceId": ["B1", "B2", "B3", "B4"], "BookTitle": ["A", "B", "C", "D"], "API Result": "Change",
                  "Link to the Issuance": [U1, U2, U3, ""]}).to_excel(pbi, index=False)
    jid = e.client.post("/api/backlog/generate", data={"ui": (io.BytesIO(ui.read_bytes()), ui.name), "powerbi": (io.BytesIO(pbi.read_bytes()), pbi.name)},
                        content_type="multipart/form-data").get_json()["job_id"]
    for _ in range(100):
        if e.client.get(f"/api/backlog/status/{jid}").get_json()["state"] != "running":
            break
        time.sleep(0.05)
    sent = e.client.post(f"/api/backlog/{jid}/send-to-url-tool", json={"rows": [0, 1, 2, 3]}).get_json()
    items = e.client.get(f"/api/transfer/{sent['transfer_id']}").get_json()["items"]
    assert len(items) == 3 and sent["skipped_blank"] == 1
    return e.client.post("/api/url-analysis/analyze", json={"mode": "items", "items": items}).get_json()["job_id"]


def strip(ev):
    return {k: ev[k] for k in ANALYSIS_FIELDS}


# ── Acceptance tests A / B / C ───────────────────────────────────────────────
def test_A_B_C_produce_identical_analysis_through_the_same_engine(env):
    jobs = {"A": run_paste(env), "B": run_excel(env), "C": run_backlog(env)}
    states, events = {}, {}
    for k, jid in jobs.items():
        states[k], events[k] = wait_job(env.client, jid)
        assert states[k]["state"] == "done" and states[k]["done"] == 3
    # same analysis fields for the same URL, whatever the input path
    by_url = {k: {ev["url"]: strip(ev) for ev in evs.values()} for k, evs in events.items()}
    assert by_url["A"] == by_url["B"] == by_url["C"]
    # the three paths must not merely agree - they must agree on the RIGHT answer
    assert [by_url["A"][u]["analysis_status"] for u in (U1, U2, U3)] == ["Success", "Failed", "Success"]
    assert by_url["A"][U1]["page_count"] == "3" and by_url["A"][U2]["error"] == "HTTP 404 - Not found" and by_url["A"][U3]["content_type"] == "HTML"
    assert (by_url["A"][U1]["url_type"], by_url["A"][U2]["status"], by_url["A"][U3]["document_link_type"]) == ("DIRECT_PDF", "NOT_WORKING", "NONE")
    # same result structure (identical keys)
    assert set(next(iter(events["A"].values()))) == set(next(iter(events["B"].values()))) == set(next(iter(events["C"].values())))
    # only the source metadata differs
    assert [states[k]["source"] for k in "ABC"] == ["Manual URL Input", "Excel Batch Upload", "Daily Backlog"]
    assert states["B"]["source_name"] == "books.xlsx"
    # ONE implementation did the work: 3 paths x 3 URLs through the same engine method
    assert len(env.fetches) == 9


def test_all_three_paths_keep_their_own_book_metadata(env):
    _, a = wait_job(env.client, run_paste(env))
    _, b = wait_job(env.client, run_excel(env))
    _, c = wait_job(env.client, run_backlog(env))
    assert {e["document_id"] for e in a.values()} == {""}
    assert {(e["document_id"], e["book_title"]) for e in b.values()} == {("X1", "A"), ("X2", "B"), ("X3", "C")}
    assert {e["extra"]["Region"] for e in b.values()} == {"UK", "US", "FR"}                      # Excel columns preserved
    assert {(e["document_id"], e["api_result"], e["book_type"]) for e in c.values()} == {("B1", "Change", "PDF"), ("B2", "Change", "PDF"), ("B3", "Change", "Other")}


def test_one_exporter_gives_the_same_columns_and_values(env):
    ids = [run_paste(env), run_excel(env), run_backlog(env)]
    frames = []
    for jid in ids:
        wait_job(env.client, jid)
        resp = env.client.get(f"/api/url-analysis/{jid}/download")
        assert resp.status_code == 200 and resp.data[:2] == b"PK"
        df = pd.read_excel(io.BytesIO(resp.data), dtype=object)
        assert [c for c in df.columns if c in STANDARD] == STANDARD                              # same names, same order
        frames.append(df.sort_values("URL").reset_index(drop=True))
    assert len(env.exports) == 3                                                                 # one exporter used 3 times
    compare = ["URL", "Domain", "HTTP Status", "Final URL", "Content Type", "Direct Document", "Page Count", "File Size", "Language", "Analysis Status", "Error"]
    for other in frames[1:]:
        pd.testing.assert_frame_equal(frames[0][compare].fillna(""), other[compare].fillna(""))
    assert "Region" in frames[1].columns and "Book Type" in frames[2].columns and "API Result" in frames[2].columns
    assert "Region" not in frames[0].columns


def test_page_count_is_one_standard_field_everywhere(env):
    jid = run_excel(env)
    _, ev = wait_job(env.client, jid)
    assert all("page_count" in e and "pages" not in e and "PageCount" not in e for e in ev.values())


def test_excel_needs_a_url_column_choice_when_unsure(env):
    p = env.tmp / "amb.xlsx"
    pd.DataFrame({"URL": [U1], "Source URL": [U2]}).to_excel(p, index=False)
    ins = env.client.post("/api/url-analysis/excel/inspect", data={"file": (io.BytesIO(p.read_bytes()), "amb.xlsx")},
                          content_type="multipart/form-data").get_json()
    assert ins["confident"] is False and set(ins["candidates"]) == {"URL", "Source URL"}
    assert env.client.post("/api/url-analysis/excel/analyze", json={"upload_id": ins["upload_id"]}).status_code == 400
    ok = env.client.post("/api/url-analysis/excel/analyze", json={"upload_id": ins["upload_id"], "url_column": "Source URL"})
    assert ok.status_code == 200
    _, ev = wait_job(env.client, ok.get_json()["job_id"])
    assert ev[0]["url"] == U2 and ev[0]["extra"] == {"URL": U1}                                 # unchosen column preserved
    assert env.client.post("/api/url-analysis/excel/analyze", json={"upload_id": ins["upload_id"], "url_column": "URL"}).status_code == 410


def test_excel_upload_rejects_wrong_files(env):
    r = env.client.post("/api/url-analysis/excel/inspect", data={"file": (io.BytesIO(b"x"), "a.csv")}, content_type="multipart/form-data")
    assert r.status_code == 400
    r = env.client.post("/api/url-analysis/excel/inspect", data={"file": (io.BytesIO(b"junk"), "a.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "could not be read" in r.get_json()["error"]


# ── caching and re-analysis (common to every source) ─────────────────────────
def test_cached_results_are_flagged_and_reanalyze_forces_a_live_check(tmp_path):
    analyzer, sess = make_analyzer(ROUTES())                                                    # default caching ON
    svc = UrlAnalysisService(tmp_path / "out", analyzer)
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out", tmp_path / "up", service=svc))
    c = app.test_client()
    first = c.post("/api/url-analysis/analyze", json={"mode": "urls", "text": U1}).get_json()["job_id"]
    s1, e1 = wait_job(c, first)
    second = c.post("/api/url-analysis/analyze", json={"mode": "urls", "text": U1}).get_json()["job_id"]
    s2, e2 = wait_job(c, second)
    assert e1[0]["cached"] is False and e2[0]["cached"] is True and len(sess.calls) == 1
    assert e2[0]["analyzed_at"] == e1[0]["analyzed_at"]                                         # tells you when the live check was
    assert c.post(f"/api/url-analysis/{second}/reanalyze", json={"indices": [0]}).status_code == 200
    s3, e3 = wait_job(c, second)
    for _ in range(100):
        s3, e3b = wait_job(c, second)
        if s3["state"] == "done":
            break
    st = c.get(f"/api/url-analysis/{second}/status?from=0").get_json()
    latest = {}
    for ev in st["events"]:
        latest[ev["idx"]] = ev
    assert latest[0]["cached"] is False and len(sess.calls) == 2
    assert c.post("/api/url-analysis/nope/reanalyze", json={}).status_code == 404


def test_reanalyze_validation(env):
    jid = run_paste(env)
    wait_job(env.client, jid)
    assert env.client.post(f"/api/url-analysis/{jid}/reanalyze", json={"indices": "x"}).status_code == 400
    assert env.client.post(f"/api/url-analysis/{jid}/reanalyze", json={"indices": [99]}).status_code == 409


# ── architecture guards ──────────────────────────────────────────────────────
def test_no_second_http_or_pdf_implementation_exists():
    for legacy in ("document_detector", "url_checker", "http_client", "excel_handler"):
        assert importlib.util.find_spec(f"url_analyzer.{legacy}") is None, f"legacy module {legacy} is back"
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(SRC).as_posix()
        if re.search(r"^\s*(import requests|from requests)", text, re.M) and not rel.startswith("analysis/"):
            offenders.append(rel)
        if re.search(r"^\s*import pypdf|^\s*from pypdf", text, re.M) and rel not in ("pdf_analyzer.py",):
            offenders.append(rel)
    assert offenders == [], f"HTTP/PDF logic outside the shared engine: {offenders}"
    exporters = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                 if re.search(r"TableStyleInfo", p.read_text(encoding="utf-8")) and "backlog" not in p.parts]
    assert exporters == ["analysis/export.py"], exporters


def test_cli_excel_batch_uses_the_same_engine_and_exporter(tmp_path, monkeypatch):
    import sys
    import url_analyzer.main  # noqa: F401  (the package re-exports a function named `main`)
    cli = sys.modules["url_analyzer.main"]
    analyzer, _ = make_analyzer(ROUTES(), cache_ttl=0)
    monkeypatch.setattr(cli, "URLAnalyzer", lambda settings: analyzer)
    calls = {"engine": 0, "export": 0}
    real_analyze, real_export = cli.analyze_urls, cli.export_analysis_results
    monkeypatch.setattr(cli, "analyze_urls", lambda *a, **k: calls.__setitem__("engine", calls["engine"] + 1) or real_analyze(*a, **k))
    monkeypatch.setattr(cli, "export_analysis_results", lambda *a, **k: calls.__setitem__("export", calls["export"] + 1) or real_export(*a, **k))
    src = tmp_path / "in.xlsx"
    pd.DataFrame({"Document ID": ["X1", "X2", "X3"], "Book Title": ["A", "B", "C"], "URL": [U1, U2, U3], "Region": ["UK", "US", "FR"]}).to_excel(src, index=False)
    out = tmp_path / "out.xlsx"
    results = cli.process(str(src), str(out), log_dir=str(tmp_path / "logs"))
    assert calls == {"engine": 1, "export": 1} and [r.analysis_status for r in results] == ["Success", "Failed", "Success"]
    df = pd.read_excel(out, dtype=object)
    assert [c for c in df.columns if c in STANDARD] == STANDARD and "Region" in df.columns
    assert df.loc[0, "Page Count"] == 3 or str(df.loc[0, "Page Count"]) == "3"
