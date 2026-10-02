"""Page source supplied by the user (copied from their own browser) - for pages that show automated clients a
verification page. The page itself is never requested; only the document it links to is."""
import time
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer.analysis import BookRecord, analyze_urls, export_analysis_results
from url_analyzer.services.url_analysis_service import UrlAnalysisService
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

PAGE = "https://www.boi.example/roles/public-reports/report/"
PDF = "https://www.boi.example/media/dc2bv5uw/692.pdf"
HTML = {"Content-Type": "text/html; charset=UTF-8"}
RADWARE = '<html><head><title>Radware Page </title></head><body><script>var __uzdbm_1 = "x";</script></body></html>'
REAL = ('<html><body><h1>דיווח</h1><a href="/about">About</a>'
        '<a class="btn btn-primary download-button" href="/media/dc2bv5uw/692.pdf">להורדת הקובץ</a></body></html>')


def pdf_resp(pages=2):
    return FakeResp(200, text_pdf(ENGLISH, pages), PDF_HEADERS)


# ── engine ───────────────────────────────────────────────────────────────────
def test_supplied_page_source_finds_and_analyses_the_linked_document_without_requesting_the_page():
    a, sess = make_analyzer({PDF: pdf_resp(2)})                       # PAGE is NOT routed: requesting it would raise
    r = a.analyze_html(PAGE, REAL)
    assert (r.status, r.url_type, r.document_link_type, r.document_link_count) == ("WORKING", "LANDING_PAGE", "SINGLE", 1)
    assert (r.document_url, r.page_count, r.language, r.analysis_status) == (PDF, "2", "English", "Success")
    assert [c[1] for c in sess.calls] == [PDF]                          # only the document was fetched
    assert any("page source supplied by the user" in n for n in r.notes) and r.analyzed_at and not r.cached


def test_relative_links_resolve_against_the_page_url_or_a_base_tag():
    a, _ = make_analyzer({})
    html = '<html><head><base href="https://cdn.example/files/"></head><body><a href="r.pdf">r</a><a href="s.pdf">s</a></body></html>'
    r = a.analyze_html(PAGE, html)
    assert r.document_urls == ["https://cdn.example/files/r.pdf", "https://cdn.example/files/s.pdf"] and r.document_link_type == "MULTIPLE"


def test_extensionless_candidates_in_supplied_html_are_still_verified():
    html = '<a class="pdf" href="/notice/9/pdf">View PDF</a><a class="pdf" href="/about-pdfs">About</a>'
    a, _ = make_analyzer({"https://www.boi.example/notice/9/pdf": pdf_resp(4), "https://www.boi.example/about-pdfs": FakeResp(200, b"<html>x</html>", HTML)})
    r = a.analyze_html(PAGE, f"<html><body>{html}</body></html>")
    assert (r.document_link_type, r.page_count) == ("SINGLE", "4")


def test_no_links_in_supplied_html_is_none_and_the_browser_fallback_is_not_used():
    class Boom:
        def render(self, url):
            raise AssertionError("must not render a page the user supplied")
    a, _ = make_analyzer({})
    a.renderer = Boom()
    r = a.analyze_html(PAGE, "<html><body><p>Just text</p></body></html>")
    assert (r.document_link_type, r.document_link_count, r.analysis_status) == ("NONE", 0, "Success")


def test_pasting_the_verification_page_itself_is_explained():
    a, _ = make_analyzer({})
    r = a.analyze_html(PAGE, RADWARE)
    assert r.status == "BLOCKED" and "pasted HTML is a Radware verification page" in r.error and "page source" in r.error


@pytest.mark.parametrize("url,html,fragment", [
    ("", REAL, "Empty URL"), ("not a url", REAL, "Invalid URL"), ("ftp://x.example/a", REAL, "Unsupported URL scheme"),
    ("http://127.0.0.1/x", REAL, "private or local"), (PAGE, "", "No page source"), (PAGE, "just words, no markup", "No page source")])
def test_bad_input_fails_clearly_without_network(url, html, fragment):
    a, sess = make_analyzer({})
    r = a.analyze_html(url, html)
    assert r.analysis_status == "Failed" and fragment in r.error and sess.calls == []


def test_batch_mixes_fetched_and_supplied_records_in_order():
    a, sess = make_analyzer({"https://other.example/p": FakeResp(200, b"<html>none</html>", HTML), PDF: pdf_resp(3)})
    recs = [BookRecord(url="https://other.example/p", document_id="N"), BookRecord(url=PAGE, document_id="S", page_html=REAL)]
    res = analyze_urls(recs, analyzer=a)
    assert [r.document_id for r in res] == ["N", "S"] and [r.document_link_type for r in res] == ["NONE", "SINGLE"]
    assert PAGE not in [c[1] for c in sess.calls]


def test_the_supplied_html_is_not_part_of_the_export(tmp_path):
    a, _ = make_analyzer({PDF: pdf_resp(2)})
    res = analyze_urls([BookRecord(url=PAGE, page_html=REAL)], analyzer=a)
    path = export_analysis_results(res, tmp_path / "o.xlsx")
    df = pd.read_excel(path, dtype=object).fillna("")
    assert "page_html" not in " ".join(df.columns).lower() and REAL not in " ".join(map(str, df.iloc[0].tolist()))
    assert "supplied by the user" in df.loc[0, "Notes"]


# ── web API ──────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path):
    analyzer, sess = make_analyzer({PAGE: FakeResp(200, RADWARE.encode(), HTML), PDF: pdf_resp(6),
                                    "https://ok.example/p": FakeResp(200, b"<html>nothing</html>", HTML)}, cache_ttl=0)
    svc = UrlAnalysisService(tmp_path / "o", analyzer)
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / "templates"))
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "o", tmp_path / "u", service=svc))
    return type("E", (), {"c": app.test_client(), "sess": sess, "svc": svc})


def wait(c, jid, start=0):
    events = []
    for _ in range(200):
        s = c.get(f"/api/url-analysis/{jid}/status?from={start + len(events)}").get_json()
        events += s["events"]
        if s["state"] != "running":
            return s, events
        time.sleep(0.02)
    raise AssertionError("timeout")


def test_page_source_tab_flow(env):
    r = env.c.post("/api/url-analysis/analyze", json={"mode": "page_source", "url": PAGE, "html": REAL, "document_id": "B1", "book_title": "Report"})
    assert r.status_code == 200 and r.get_json()["source"] == "Pasted Page Source"
    s, ev = wait(env.c, r.get_json()["job_id"])
    e = ev[0]
    assert (e["document_id"], e["book_title"], e["document_link_type"], e["page_count"], e["document_url"]) == ("B1", "Report", "SINGLE", "6", PDF)
    assert PAGE not in [c[1] for c in env.sess.calls] and s["source"] == "Pasted Page Source"


@pytest.mark.parametrize("body,code,fragment", [
    ({"mode": "page_source", "url": "", "html": REAL}, 400, "address of the page"),
    ({"mode": "page_source", "url": PAGE, "html": "no markup here at all"}, 400, "HTML source"),
    ({"mode": "page_source", "url": PAGE, "html": "<" + "a" * (5 * 1024 * 1024 + 10)}, 413, "too large")])
def test_page_source_tab_validation(env, body, code, fragment):
    r = env.c.post("/api/url-analysis/analyze", json=body)
    assert r.status_code == code and fragment in r.get_json()["error"]


def test_blocked_row_can_be_fixed_by_pasting_the_page_source(env):
    jid = env.c.post("/api/url-analysis/analyze", json={"mode": "urls", "text": f"{PAGE}\nhttps://ok.example/p"}).get_json()["job_id"]
    s, ev = wait(env.c, jid)
    by = {e["url"]: e for e in ev}
    blocked = by[PAGE]
    assert (blocked["status"], blocked["document_link_type"]) == ("BLOCKED", "") and by["https://ok.example/p"]["document_link_type"] == "NONE"
    r = env.c.post(f"/api/url-analysis/{jid}/page-source", json={"index": blocked["idx"], "html": REAL})
    assert r.status_code == 200
    s2, ev2 = wait(env.c, jid, start=len(ev))
    fixed = ev2[-1]
    assert fixed["idx"] == blocked["idx"] and (fixed["status"], fixed["document_link_type"], fixed["page_count"]) == ("WORKING", "SINGLE", "6")
    assert len(ev2) == 1                                                              # only that row was re-analysed
    pages_fetched = [c[1] for c in env.sess.calls].count(PAGE)
    assert pages_fetched == 1                                                         # the blocked page was not requested again
    # re-analysing the row later keeps using the supplied source instead of hitting the site again
    assert env.c.post(f"/api/url-analysis/{jid}/reanalyze", json={"indices": [blocked["idx"]]}).status_code == 200
    s3, ev3 = wait(env.c, jid, start=len(ev) + len(ev2))
    assert ev3[-1]["document_link_type"] == "SINGLE" and [c[1] for c in env.sess.calls].count(PAGE) == 1


def test_page_source_endpoint_errors(env):
    jid = env.c.post("/api/url-analysis/analyze", json={"mode": "urls", "text": PAGE}).get_json()["job_id"]
    wait(env.c, jid)
    url = f"/api/url-analysis/{jid}/page-source"
    assert env.c.post("/api/url-analysis/nope/page-source", json={"index": 0, "html": REAL}).status_code == 404
    assert env.c.post(url, json={"index": 5, "html": REAL}).status_code == 400
    assert env.c.post(url, json={"index": "0", "html": REAL}).status_code == 400
    assert env.c.post(url, json={"index": 0, "html": "words only"}).status_code == 400
    assert env.c.post(url, json={"index": 0, "html": "<" + "a" * (5 * 1024 * 1024 + 10)}).status_code == 413


def test_page_has_the_tab_button_and_dialog(env):
    html = env.c.get("/url-analysis").get_data(as_text=True)
    for needle in ('data-t="source"', 'id="p-source"', 'id="src-html"', 'id="ps-dlg"', "Paste page source", "document.documentElement.outerHTML", "page-source"):
        assert needle in html, needle
