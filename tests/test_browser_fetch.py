"""Sites that refuse automated requests: the browser-window fallback and the 'Download PDFs' folder.

A local replica site behaves like a Cloudflare-protected one: a plain HTTP client (no Sec-Fetch headers) gets HTTP 403, a real
browser is served the file, and one page can show a verification page that is cleared when 'the person' approves it."""
import csv
import http.server
import threading
import time
from pathlib import Path

import pytest
from flask import Flask

from tests.helpers import text_pdf
from url_analyzer.analysis import AnalyzerSettings, BookRecord, URLAnalyzer, analyze_urls
from url_analyzer.analysis import downloads as dl
from url_analyzer.analysis.browser_fetch import BrowserFetcher
from url_analyzer.analysis.engine import BROWSER_NOTE
from url_analyzer.services.url_analysis_service import UrlAnalysisService
from url_analyzer.ui.url_analysis import create_url_analysis_blueprint

H = {"X-Requested-With": "fetch"}
needs_browser = pytest.mark.skipif(not __import__("importlib").util.find_spec("playwright"), reason="Playwright is not installed")
PDF = text_pdf("hello", pages=3)
CHALLENGE = ("<html><head><title>Just a moment...</title></head><body>Checking your browser <script>"
             "setInterval(() => fetch('/status').then(r => r.text()).then(t => { if (t === 'ok') location.reload(); }), 300);</script></body></html>")


class Site:
    def __init__(self):
        self.challenge = False                     # every page shows the verification page until approved
        self.approved = False
        self.hits: list[tuple[str, bool]] = []     # (path, was it a browser?)

    def reset(self, challenge=False):
        self.challenge, self.approved, self.hits = challenge, False, []


@pytest.fixture(scope="module")
def site():
    s = Site()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Server", "cloudflare")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            browser = "Sec-Fetch-Mode" in self.headers
            s.hits.append((path, browser))
            if path == "/status":
                return self.send(200, "ok" if s.approved else "wait", "text/plain")
            if path == "/open.pdf":
                return self.send(200, PDF, "application/pdf")
            if path == "/slow.pdf":                                    # open to everyone, but the body takes its time
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(PDF)))
                self.end_headers()
                self.wfile.write(PDF[:100])
                self.wfile.flush()
                time.sleep(6)
                try:
                    self.wfile.write(PDF[100:])
                except OSError:
                    pass
                return
            if path == "/open.html":
                return self.send(200, "<html><body>just an open page</body></html>")
            if not browser:                                            # a script, not a browser
                return self.send(403, "<html><title>Access denied</title><body>Error 1020</body></html>")
            if s.challenge and not s.approved:
                return self.send(403, CHALLENGE)
            if path in ("/", "/index.html"):
                return self.send(200, "<html><body>home</body></html>")
            if path.endswith(".pdf"):
                return self.send(200, PDF, "application/pdf")
            if path == "/landing":
                return self.send(200, '<html><body><a href="/media/doc-a.pdf">Rules</a></body></html>')
            if path == "/page.html":
                return self.send(200, "<html><body>just a page</body></html>")
            return self.send(404, "not found")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s.url = f"http://127.0.0.1:{srv.server_port}"
    yield s
    srv.shutdown()


@pytest.fixture(scope="module")
def fetcher(tmp_path_factory):
    f = BrowserFetcher(tmp_path_factory.mktemp("profile"), headless=True, can_wait=False, approve_timeout=20, validate=lambda u: "")
    yield f
    f.close()


def make(fetcher, **kw):
    a = URLAnalyzer(AnalyzerSettings(retries=0, max_workers=2, allow_private_hosts=True, read_timeout=15, **kw), browser_fetcher=fetcher)
    return a


# ── the analysis ─────────────────────────────────────────────────────────────
def test_a_refused_url_is_blocked_without_the_browser_option(site, fetcher):
    site.reset()
    r = analyze_urls([BookRecord(url=site.url + "/media/doc-a.pdf", document_id="X1")], analyzer=make(fetcher))[0]
    assert (r.status, r.analysis_status, r.http_status) == ("BLOCKED", "Failed", 403) and r.error == "HTTP 403 - Access denied"
    assert any("Open blocked sites in a browser window" in n for n in r.notes)


@needs_browser
def test_the_browser_option_reads_the_refused_file(site, fetcher):
    site.reset()
    r = analyze_urls([BookRecord(url=site.url + "/media/doc-a.pdf", document_id="X1")], analyzer=make(fetcher), use_browser=True)[0]
    assert (r.status, r.analysis_status, r.url_type, r.content_type) == ("WORKING", "Success", "DIRECT_PDF", "PDF")
    assert (r.http_status, r.final_status, r.page_count) == (403, 200, "3") and BROWSER_NOTE in r.notes
    assert r.file_size_bytes == len(PDF)


@needs_browser
def test_an_unapproved_verification_page_stays_blocked_with_a_reason(site, fetcher):
    site.reset(challenge=True)
    r = analyze_urls([BookRecord(url=site.url + "/media/doc-b.pdf")], analyzer=make(fetcher), use_browser=True)[0]
    assert r.status == "BLOCKED" and "verification page" in r.error and "browser window was tried too" in r.error


@needs_browser
def test_a_verification_page_approved_by_the_person_lets_the_file_through(site, tmp_path):
    site.reset(challenge=True)
    f = BrowserFetcher(tmp_path / "p", headless=True, can_wait=True, approve_timeout=30, validate=lambda u: "")
    try:
        def person():                                   # "the person clicks Approve" a moment after the verification page appears
            while not any(browser and path != "/status" for path, browser in site.hits):
                time.sleep(0.1)
            time.sleep(1.5)
            site.approved = True

        threading.Thread(target=person, daemon=True).start()
        bf = f.fetch(site.url + "/media/doc-c.pdf")
        assert bf.ok and bf.data == PDF and bf.approved_by_user
    finally:
        f.close()


@needs_browser
def test_a_landing_page_behind_the_check_is_read_and_its_single_document_followed(site, fetcher):
    site.reset()
    r = analyze_urls([BookRecord(url=site.url + "/landing")], analyzer=make(fetcher), use_browser=True)[0]
    # the landing page is refused (403) -> read through the browser -> one link -> followed (through the browser too)
    assert (r.status, r.url_type, r.document_link_type) == ("WORKING", "LANDING_PAGE", "SINGLE")
    assert r.page_count == "3" and r.document_url.endswith("/media/doc-a.pdf")


def test_the_option_is_part_of_the_cache_key(site, fetcher):
    site.reset()
    a = make(fetcher)
    plain = a.analyze(site.url + "/media/doc-a.pdf")
    assert plain.status == "BLOCKED"
    assert a._cache_key("x", "|browser") != a._cache_key("x")


# ── names and folders ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("DOC-123", "DOC-123"), ("A/B:C*D?", "A_B_C_D_"), ("  spaced   id  ", "spaced id"), ("trailing.", "trailing"),
    ("CON", "_CON"), ("", ""), ("a" * 300, "a" * 120)])
def test_file_names_are_safe_and_keep_the_document_id(raw, expected):
    assert dl.safe_name(raw) == expected


def test_each_download_gets_its_own_new_folder(tmp_path):
    a = dl.new_folder(tmp_path / "base", "Daily Backlog")
    b = dl.new_folder(tmp_path / "base", "Daily Backlog")
    assert a != b and a.parent == b.parent == tmp_path / "base" and a.is_dir() and b.is_dir() and a.name.startswith("Daily Backlog_")


def test_names_are_unique_ignoring_case_and_rows_without_an_id_are_still_saved():
    n, it = dl.Namer(), lambda i, d: dl.Item(i, d, "", "u", "t")
    assert [n.name(it(1, "ab"), ".pdf"), n.name(it(2, "AB"), ".pdf"), n.name(it(3, ""), ".pdf"), n.name(it(4, "ab"), ".docx")] == [
        "ab.pdf", "AB_2.pdf", "NoDocumentId_Row3.pdf", "ab.docx"]


# ── saving the files ─────────────────────────────────────────────────────────
@needs_browser
def test_bulk_download_names_files_by_document_id_in_a_new_folder(site, fetcher, tmp_path):
    site.reset()
    a = make(fetcher)
    recs = [BookRecord(url=site.url + "/media/doc-a.pdf", document_id="DOC/1"),                 # refused to scripts, read through the browser
            BookRecord(url=site.url + "/open.pdf", document_id="OPEN-2"),                          # open to everyone
            BookRecord(url=site.url + "/open.pdf?again=1", document_id="OPEN-3"),
            BookRecord(url=site.url + "/page.html", document_id="PAGE-4"),                         # not a document
            BookRecord(url=site.url + "/media/doc-z.pdf", document_id="")]                         # no Document ID
    results = analyze_urls(recs, analyzer=a, use_browser=True)
    items = dl.plan(results, None)
    folder = dl.new_folder(tmp_path / "Backlog PDFs", "Test run")
    saved = dl.run(a, items, folder, use_browser=True)
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["DOC_1.pdf", "NoDocumentId_Row5.pdf", "OPEN-2.pdf", "OPEN-3.pdf", "download_report.csv"]
    assert all((folder / n).read_bytes() == PDF for n in files if n.endswith(".pdf")) and not list(folder.glob("*.part"))
    by_row = {s.row: s for s in saved}
    assert by_row[1].via == "browser window" and by_row[2].via == "direct" and by_row[3].status == "Saved"
    assert by_row[4].status == "Skipped" and "no document" in by_row[4].error.lower()
    rows = list(csv.reader(open(folder / "download_report.csv", encoding="utf-8-sig")))
    assert rows[0][:3] == ["Row", "Document ID", "Title"] and len(rows) == 6 and rows[4][6] == "Skipped"


def test_same_address_is_fetched_once_and_copied_for_each_document_id(site, fetcher, tmp_path):
    site.reset()
    a = make(fetcher)
    items = [dl.Item(1, "ID-A", "", "u", site.url + "/open.pdf"), dl.Item(2, "ID-B", "", "u", site.url + "/open.pdf")]
    folder = dl.new_folder(tmp_path, "dup")
    saved = dl.run(a, items, folder)
    assert sorted(p.name for p in folder.glob("*.pdf")) == ["ID-A.pdf", "ID-B.pdf"] and [s.status for s in saved] == ["Saved", "Saved"]
    assert sum(1 for p, _ in site.hits if p == "/open.pdf") == 1


def test_without_the_browser_option_a_refused_file_is_reported_not_saved(site, fetcher, tmp_path):
    site.reset()
    a = make(fetcher)
    items = [dl.Item(1, "ID-A", "", "u", site.url + "/media/doc-a.pdf")]
    saved = dl.run(a, items, dl.new_folder(tmp_path, "x"))
    assert saved[0].status == "Failed" and "403" in saved[0].error and "browser window" in saved[0].error


def test_what_can_be_saved_is_decided_from_the_analysis(site):
    from url_analyzer.analysis.models import URLAnalysisResult as R
    direct = R(url="https://a/x.pdf", document_id="1", direct_document=True, content_type="PDF", final_url="https://a/x.pdf")
    single = R(url="https://a/page", document_id="2", document_link_type="SINGLE", document_url="https://a/real.pdf")
    multi = R(url="https://a/m", document_id="3", document_link_type="MULTIPLE", document_link_count=4)
    none = R(url="https://a/n", document_id="4", document_link_type="NONE")
    blocked = R(url="https://a/b.pdf", document_id="5", status="BLOCKED", analysis_status="Failed")
    dead = R(url="https://a/d", document_id="6", status="NOT_WORKING", analysis_status="Failed", error="HTTP 404 - Not found")
    items = dl.plan([direct, single, multi, none, blocked, dead, None], None)
    assert [(i.target, bool(i.skip)) for i in items] == [("https://a/x.pdf", False), ("https://a/real.pdf", False), ("", True), ("", True),
                                                          ("https://a/b.pdf", False), ("", True), ("", True)]
    assert dl.plan([direct, single], [1])[0].document_id == "2"


# ── the web tool ─────────────────────────────────────────────────────────────
@pytest.fixture
def web(site, fetcher, tmp_path):
    a = make(fetcher)
    svc = UrlAnalysisService(tmp_path / "out" / "url_analysis", a)
    app = Flask(__name__, template_folder=str(Path(__file__).parents[1] / "templates"))
    app.register_blueprint(create_url_analysis_blueprint(tmp_path / "out" / "url_analysis", tmp_path / "up", service=svc))
    app.testing = True
    return app.test_client(), svc, tmp_path


def wait_for(c, url, key="state", tries=400):
    for _ in range(tries):
        d = c.get(url).get_json()
        if d[key] != "running":
            return d
        time.sleep(0.15)
    raise AssertionError("timeout")


@needs_browser
def test_the_tool_reads_blocked_sites_in_a_browser_window_and_saves_the_pdfs(web, site):
    c, svc, tmp = web
    site.reset()
    text = f"DOC-1\t{site.url}/media/doc-a.pdf\nDOC-2\t{site.url}/open.pdf"
    job = c.post("/api/url-analysis/analyze", json={"mode": "auto", "text": text, "browser": True}).get_json()["job_id"]
    st = wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    assert st["state"] == "done" and st["use_browser"] and {e["status"] for e in st["events"]} == {"WORKING"}
    defaults = c.get("/api/url-analysis/downloads/defaults").get_json()
    assert defaults["base"].endswith("pdf_downloads") and defaults["local"]
    assert c.post(f"/api/url-analysis/{job}/downloads", json={}).status_code == 400                      # needs the fetch header
    mine = tmp / "My Backlog PDFs"
    assert c.post(f"/api/url-analysis/{job}/downloads", json={"folder": "relative/path"}, headers=H).status_code == 400
    info = c.post(f"/api/url-analysis/{job}/downloads", json={"folder": str(mine), "browser": True}, headers=H).get_json()
    assert info["saveable"] == 2 and Path(info["folder"]).parent == mine.resolve()
    d = wait_for(c, f"/api/url-analysis/downloads/{info['download_id']}")
    assert (d["state"], d["saved"], d["failed"], d["skipped"], d["done"]) == ("done", 2, 0, 0, 2)
    assert sorted(p.name for p in Path(d["folder"]).glob("*.pdf")) == ["DOC-1.pdf", "DOC-2.pdf"]
    assert Path(d["folder"]).parent == mine.resolve()                                                    # a NEW folder inside the chosen one


def test_default_folder_is_new_and_next_to_the_output_not_in_downloads(web, site):
    c, svc, tmp = web
    site.reset()
    job = c.post("/api/url-analysis/analyze", json={"mode": "items", "items": [{"url": site.url + "/open.pdf", "document_id": "A1"}]}).get_json()["job_id"]
    wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    info = c.post(f"/api/url-analysis/{job}/downloads", json={"indices": [0]}, headers=H).get_json()
    d = wait_for(c, f"/api/url-analysis/downloads/{info['download_id']}")
    folder = Path(d["folder"])
    assert d["state"] == "done" and folder.parent == (tmp / "out" / "pdf_downloads").resolve() and (folder / "A1.pdf").read_bytes() == PDF
    assert "Downloads" not in folder.parts[-3:]


def test_nothing_to_save_and_unknown_ids(web, site):
    c, svc, tmp = web
    site.reset()
    job = c.post("/api/url-analysis/analyze", json={"mode": "items", "items": [{"url": site.url + "/open.html", "document_id": "P1"}]}).get_json()["job_id"]
    wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    r = c.post(f"/api/url-analysis/{job}/downloads", json={}, headers=H)
    assert r.status_code == 400 and "no document" in r.get_json()["error"].lower()
    assert c.post("/api/url-analysis/nope/downloads", json={}, headers=H).status_code == 404
    assert c.get("/api/url-analysis/downloads/nope").status_code == 404
    assert c.post("/api/url-analysis/downloads/nope/cancel", headers=H).status_code == 404


def test_open_folder_only_opens_a_folder_the_tool_made(web, site, monkeypatch):
    c, svc, tmp = web
    site.reset()
    opened = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: opened.append(cmd[-1]))
    job = c.post("/api/url-analysis/analyze", json={"mode": "items", "items": [{"url": site.url + "/open.pdf", "document_id": "A1"}]}).get_json()["job_id"]
    wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    info = c.post(f"/api/url-analysis/{job}/downloads", json={}, headers=H).get_json()
    wait_for(c, f"/api/url-analysis/downloads/{info['download_id']}")
    assert c.post(f"/api/url-analysis/downloads/{info['download_id']}/open-folder", headers=H).get_json() == {"opened": True}
    assert opened == [info["folder"]]
    assert c.post("/api/url-analysis/downloads/nope/open-folder", headers=H).status_code == 404


def test_reanalyze_can_turn_the_browser_option_on(web, site):
    c, svc, tmp = web
    site.reset()
    job = c.post("/api/url-analysis/analyze", json={"mode": "auto", "text": f"A1\t{site.url}/media/doc-a.pdf"}).get_json()["job_id"]
    st = wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    assert st["events"][0]["status"] == "BLOCKED" and not st["use_browser"]
    if __import__("importlib").util.find_spec("playwright"):
        assert c.post(f"/api/url-analysis/{job}/reanalyze", json={"indices": [0], "browser": True}).status_code == 200
        st = wait_for(c, f"/api/url-analysis/{job}/status?from=0")
        assert st["use_browser"] and [e["status"] for e in st["events"]][-1] == "WORKING"


def test_the_pages_have_the_browser_option_and_the_download_panel():
    import importlib
    html = importlib.import_module("dashboard").create_app().test_client().get("/url-analysis").data.decode()
    for needle in ('id="use-browser"', 'id="dl-pdfs"', 'id="pd-dlg"', 'id="re-blocked"', "/downloads", "open-folder"):
        assert needle in html, needle


# ── skipping a URL that is slow or waiting for approval ─────────────────────────────────────
@needs_browser
def test_a_url_waiting_for_approval_can_be_skipped_and_the_next_one_goes_on(site, tmp_path):
    site.reset(challenge=True)
    f = BrowserFetcher(tmp_path / "p", headless=True, can_wait=True, approve_timeout=60, validate=lambda u: "")
    try:
        def person_skips():
            while not f.state().get("waiting_for_user"):
                time.sleep(0.1)
            assert f.state()["can_skip"] and f.skip_current()

        threading.Thread(target=person_skips, daemon=True).start()
        t0 = time.monotonic()
        bf = f.fetch(site.url + "/media/doc-a.pdf")
        assert bf.skipped and "Skipped" in bf.error and time.monotonic() - t0 < 30            # not the 60 s wait
        site.approved = True                                                                    # the browser is free for the next URL
        nxt = f.fetch(site.url + "/media/doc-b.pdf")
        assert nxt.ok and nxt.data == PDF
    finally:
        f.close()


@needs_browser
def test_the_caller_can_skip_with_a_stop_event(site, tmp_path):
    site.reset(challenge=True)
    f = BrowserFetcher(tmp_path / "p", headless=True, can_wait=True, approve_timeout=60, validate=lambda u: "")
    try:
        stop = threading.Event()
        threading.Timer(3, stop.set).start()
        t0 = time.monotonic()
        bf = f.fetch(site.url + "/media/doc-a.pdf", stop=stop)
        assert bf.skipped and time.monotonic() - t0 < 20
    finally:
        f.close()


def test_a_slow_download_is_skipped_and_the_rest_are_saved(site, fetcher, tmp_path):
    site.reset()
    a = make(fetcher)
    items = [dl.Item(1, "SLOW", "", "u", site.url + "/slow.pdf"), dl.Item(2, "FAST", "", "u", site.url + "/open.pdf")]
    folder = dl.new_folder(tmp_path, "skip")
    gate = {}

    def started(it, ev):
        gate[it.row] = ev
        if it.row == 1:
            threading.Timer(1, ev.set).start()                              # "the person clicks Skip" after a second

    t0 = time.monotonic()
    saved = dl.run(a, items, folder, on_start=started)
    assert time.monotonic() - t0 < 5.5                                      # the slow body needs 6 s: it was not waited for
    by = {s.row: s for s in saved}
    assert (by[1].status, by[1].error) == ("Skipped", "Skipped by you.") and by[2].status == "Saved"
    assert sorted(p.name for p in folder.glob("*.pdf")) == ["FAST.pdf"]


def test_skip_from_the_web_tool(web, site):
    c, svc, tmp = web
    site.reset()
    job = c.post("/api/url-analysis/analyze", json={"mode": "items", "items": [
        {"url": site.url + "/open.pdf", "document_id": "OK1"}]}).get_json()["job_id"]
    wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    svc.get(job)["results"][0].document_url = site.url + "/slow.pdf"                  # make the saved target the slow file
    info = c.post(f"/api/url-analysis/{job}/downloads", json={}, headers=H).get_json()
    did = info["download_id"]
    for _ in range(50):
        d = c.get(f"/api/url-analysis/downloads/{did}").get_json()
        if d["active"]:
            break
        time.sleep(0.1)
    assert d["active"] and d["active"][0]["row"] == 1 and d["active"][0]["document_id"] == "OK1"
    assert c.post(f"/api/url-analysis/downloads/{did}/skip", json={"row": "x"}, headers=H).status_code == 400
    assert c.post(f"/api/url-analysis/downloads/{did}/skip", json={"row": 1}).status_code == 400            # needs the fetch header
    assert c.post(f"/api/url-analysis/downloads/{did}/skip", json={"row": 99}, headers=H).status_code == 404
    assert c.post(f"/api/url-analysis/downloads/{did}/skip", json={"row": 1}, headers=H).get_json() == {"skipped": True}
    d = wait_for(c, f"/api/url-analysis/downloads/{did}")
    assert (d["state"], d["skipped"], d["saved"]) == ("done", 1, 0) and d["events"][0]["status"] == "Skipped"
    assert c.post("/api/url-analysis/downloads/nope/skip", json={"row": 1}, headers=H).status_code == 404


def test_skip_browser_wait_route(web, site):
    c, svc, tmp = web
    site.reset()
    job = c.post("/api/url-analysis/analyze", json={"mode": "items", "items": [{"url": site.url + "/open.pdf", "document_id": "A"}]}).get_json()["job_id"]
    wait_for(c, f"/api/url-analysis/{job}/status?from=0")
    assert c.post(f"/api/url-analysis/{job}/skip-browser", headers=H).status_code == 404                  # nothing is waiting
    assert c.post(f"/api/url-analysis/{job}/skip-browser").status_code == 400
    assert c.post("/api/url-analysis/nope/skip-browser", headers=H).status_code == 404


def test_the_pages_have_the_skip_buttons():
    import importlib
    html = importlib.import_module("dashboard").create_app().test_client().get("/url-analysis").data.decode()
    for needle in ("skip-browser", 'id="pd-active"', "Skip this URL", "/skip"):
        assert needle in html, needle
