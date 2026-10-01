"""The shared URL analysis engine, tested per URL and per batch with all network access mocked."""
import io
import socket
import threading
import time

import pytest
import requests

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, docx_bytes, make_analyzer, text_pdf
from url_analyzer.analysis import BookRecord, analyze_urls
from url_analyzer.analysis.document_analyzer import detect_content_type, human_size, parse_http_date
from url_analyzer.pdf_analyzer import _parse_pdf_date, inspect_pdf


# ── helpers under the engine ─────────────────────────────────────────────────
def test_detect_content_type():
    assert detect_content_type("application/pdf; charset=x", "http://a/x", b"") == "PDF"
    assert detect_content_type("application/octet-stream", "http://a/x", b"%PDF-1.7") == "PDF"
    assert detect_content_type("text/html", "http://a/x.pdf", b"<html>") == "HTML"
    assert detect_content_type("application/msword", "http://a/x", b"") == "DOC"
    assert detect_content_type("", "http://a/x.docx", b"PK\x03\x04") == "DOCX"
    assert detect_content_type("image/png", "http://a/x.png", b"") == "Other"


def test_human_size_and_dates():
    assert [human_size(n) for n in (None, 512, 2048, 2_516_582)] == ["Unknown", "512 B", "2.0 KB", "2.4 MB"]
    assert parse_http_date("Thu, 01 Jan 2026 00:00:00 GMT") == "2026-01-01" and parse_http_date("") == ""
    assert _parse_pdf_date("D:20231201143000") == "2023-12-01" and _parse_pdf_date("") == ""


def test_inspect_pdf_variants():
    assert "Invalid PDF" in inspect_pdf(b"NOT A PDF").problem
    info = inspect_pdf(text_pdf(ENGLISH, 4))
    assert (info.pages, info.language, info.problem) == (4, "English", "")
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(100, 100); w.encrypt("pw")
    buf = io.BytesIO(); w.write(buf)
    assert "Protected document" in inspect_pdf(buf.getvalue()).problem


# ── single URL ───────────────────────────────────────────────────────────────
def test_direct_pdf_page_count_size_language_last_modified():
    body = text_pdf(ENGLISH, pages=3)
    url = "https://example.gov/files/report.pdf"
    a, _ = make_analyzer({url: FakeResp(200, body, {**PDF_HEADERS, "Content-Length": str(len(body))})})
    r = a.analyze(url)
    assert (r.http_status, r.final_status, r.analysis_status, r.status) == (200, 200, "Success", "WORKING")
    assert (r.url_type, r.document_link_type, r.document_url) == ("DIRECT_PDF", "DIRECT", url) and r.text_extraction_status == "OK"
    assert r.content_type == "PDF" and r.mime_type == "application/pdf" and r.direct_document
    assert r.page_count == "3" and r.language == "English" and r.last_modified == "2025-10-01"
    assert r.file_size_bytes == len(body) and r.file_size.endswith("KB") and r.domain == "example.gov" and r.error == ""
    assert r.analyzed_at and not r.cached


def test_pdf_without_header_dates_or_text():
    a, _ = make_analyzer({"https://example.gov/a.pdf": FakeResp(200, text_pdf("", 2), {"Content-Type": "application/pdf"})})
    r = a.analyze("https://example.gov/a.pdf")
    assert r.page_count == "2" and r.language == "Unknown" and r.last_modified == "Unknown"


@pytest.mark.parametrize("status,message,state", [(404, "HTTP 404 - Not found", "NOT_WORKING"), (403, "HTTP 403 - Access denied", "BLOCKED"),
                                                   (500, "HTTP 500 - Server error", "NOT_WORKING"),
                                                   (401, "HTTP 401 - Authentication required", "BLOCKED")])
def test_http_errors_are_reported_not_raised(status, message, state):
    a, _ = make_analyzer({"https://example.gov/x": FakeResp(status, b"nope", {"Content-Type": "text/html"})})
    r = a.analyze("https://example.gov/x")
    assert (r.http_status, r.analysis_status, r.error, r.status, r.url_type) == (status, "Failed", message, state, "NOT_WORKING")
    assert not r.direct_document and r.page_count == "N/A"


def test_redirect_is_followed_and_final_url_captured():
    a, _ = make_analyzer({"https://example.gov/old": FakeResp(301, b"", {"Location": "/files/new.pdf"}),
                          "https://example.gov/files/new.pdf": FakeResp(200, text_pdf(ENGLISH, 1), PDF_HEADERS)})
    r = a.analyze("https://example.gov/old")
    assert (r.http_status, r.final_status, r.redirects, r.status) == (301, 200, 1, "WORKING")
    assert r.final_url == "https://example.gov/files/new.pdf" and r.content_type == "PDF" and r.analysis_status == "Success"


def test_redirect_loop_stops():
    a, _ = make_analyzer({"https://example.gov/a": FakeResp(302, b"", {"Location": "https://example.gov/a"})}, max_redirects=3)
    r = a.analyze("https://example.gov/a")
    assert r.analysis_status == "Failed" and r.error == "Too many redirects"


def test_html_page_is_not_a_direct_document_and_links_are_counted():
    html = b'<html><body><a href="/a.pdf">A</a><a href="/b.docx">B</a><a href="/c.html">C</a></body></html>'
    a, _ = make_analyzer({"https://example.gov/reports/123": FakeResp(
        200, html, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(html))})})
    r = a.analyze("https://example.gov/reports/123")
    assert r.content_type == "HTML" and not r.direct_document and r.page_count == "N/A" and r.analysis_status == "Success"
    assert (r.url_type, r.document_link_type, r.document_link_count) == ("LANDING_PAGE", "MULTIPLE", 2)
    assert r.document_urls == ["https://example.gov/a.pdf", "https://example.gov/b.docx"] and r.file_size_bytes == len(html)


def test_html_error_page_served_for_pdf_url():
    a, _ = make_analyzer({"https://example.gov/gone.pdf": FakeResp(200, b"<html>Not found</html>", {"Content-Type": "text/html"})})
    r = a.analyze("https://example.gov/gone.pdf")
    assert r.content_type == "HTML" and not r.direct_document and r.analysis_status == "Unknown" and "looks like a .pdf" in r.error


def test_direct_doc_and_docx():
    a, _ = make_analyzer({
        "https://example.gov/a.doc": FakeResp(200, b"\xd0\xcf\x11\xe0" + b"x" * 50, {"Content-Type": "application/msword", "Content-Length": "54"}),
        "https://example.gov/b.docx": FakeResp(200, docx_bytes(7), {
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})})
    doc, docx = a.analyze("https://example.gov/a.doc"), a.analyze("https://example.gov/b.docx")
    assert (doc.content_type, doc.direct_document, doc.page_count, doc.analysis_status, doc.file_size) == ("DOC", True, "Unknown", "Success", "54 B")
    assert (docx.content_type, docx.direct_document, docx.page_count, docx.language) == ("DOCX", True, "7", "English")


@pytest.mark.parametrize("url,fragment", [
    ("not a url", "Invalid URL"), ("nope", "Invalid URL (not a web address)"), ("ftp://example.com/a.pdf", "Unsupported URL scheme"),
    ("file:///etc/passwd", "Unsupported URL scheme"), ("javascript:alert(1)", "Unsupported URL scheme"),
    ("http://", "Invalid URL"), ("", "Empty URL"), ("   ", "Empty URL")])
def test_invalid_urls_fail_without_network(url, fragment):
    a, sess = make_analyzer({})
    r = a.analyze(url)
    assert r.analysis_status == "Failed" and fragment in r.error and sess.calls == []


def test_private_and_local_targets_are_blocked():
    a, sess = make_analyzer({}, resolver=lambda h: ["10.0.0.5"])
    for u in ("http://intranet.example/doc.pdf", "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/x"):
        r = a.analyze(u)
        assert r.analysis_status == "Failed" and "private or local" in r.error, u
    assert sess.calls == []


def test_redirect_to_private_address_is_blocked():
    resolver = lambda h: ["127.0.0.1"] if h == "internal.example" else ["93.184.216.34"]  # noqa: E731
    a, sess = make_analyzer({"https://example.gov/r": FakeResp(302, b"", {"Location": "http://internal.example/secret"})}, resolver=resolver)
    r = a.analyze("https://example.gov/r")
    assert r.analysis_status == "Failed" and "unsafe address" in r.error and [c[1] for c in sess.calls] == ["https://example.gov/r"]


def test_network_failures_are_captured_individually():
    a, _ = make_analyzer({"https://slow.example/a.pdf": requests.exceptions.ReadTimeout("slow"),
                          "https://down.example/a.pdf": requests.exceptions.ConnectionError("refused")})
    t, c = a.analyze("https://slow.example/a.pdf"), a.analyze("https://down.example/a.pdf")
    assert t.error == "Timeout after 30 seconds" and t.analysis_status == "Failed" and c.error.startswith("Connection failed")


def test_dns_failure_message():
    def boom(host):
        raise socket.gaierror("nope")
    a, _ = make_analyzer({}, resolver=boom)
    assert "DNS lookup failed" in a.analyze("https://does-not-exist.example/a").error


def test_invalid_ssl_certificate_falls_back_and_notes_it():
    body = text_pdf(ENGLISH, 1)

    def route(kw):
        if kw["verify"]:
            raise requests.exceptions.SSLError("certificate verify failed")
        return FakeResp(200, body, PDF_HEADERS)
    a, sess = make_analyzer({"https://selfsigned.example/a.pdf": route})
    r = a.analyze("https://selfsigned.example/a.pdf")
    assert r.analysis_status == "Success" and r.page_count == "1" and any("SSL certificate" in n for n in r.notes)
    assert [c[2] for c in sess.calls] == [True, False]


def test_broken_protected_and_oversized_pdfs_do_not_fail_the_url():
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(100, 100); w.encrypt("secret")
    buf = io.BytesIO(); w.write(buf)
    a, _ = make_analyzer({
        "https://example.gov/bad.pdf": FakeResp(200, b"%PDF-1.4 this is not really a pdf", {"Content-Type": "application/pdf"}),
        "https://example.gov/locked.pdf": FakeResp(200, buf.getvalue(), {"Content-Type": "application/pdf"}),
        "https://example.gov/huge.pdf": FakeResp(200, b"%PDF-1.4", {"Content-Type": "application/pdf", "Content-Length": str(500 * 1024 * 1024)})},
        max_download_bytes=50 * 1024 * 1024)
    bad, locked, huge = (a.analyze(f"https://example.gov/{n}.pdf") for n in ("bad", "locked", "huge"))
    assert bad.analysis_status == "Unknown" and bad.page_count == "Unknown" and "Invalid PDF" in bad.error
    assert locked.analysis_status == "Unknown" and "Protected document" in locked.error and locked.http_status == 200
    assert huge.analysis_status == "Unknown" and "larger than" in huge.error and huge.file_size == "500.0 MB"


# ── batches, cache, concurrency ──────────────────────────────────────────────
def test_batch_mixed_working_and_broken_keeps_order_and_continues():
    routes = {"https://a.example/ok.pdf": FakeResp(200, text_pdf(ENGLISH, 4), PDF_HEADERS),
              "https://b.example/missing": FakeResp(404, b"", {"Content-Type": "text/html"}),
              "https://c.example/boom": requests.exceptions.ConnectionError("down"),
              "https://d.example/page": FakeResp(200, b"<html></html>", {"Content-Type": "text/html"})}
    a, _ = make_analyzer(routes)
    records = [{"document_id": f"D{i}", "book_title": f"Book {i}", "url": u} for i, u in enumerate(routes)] + [{"document_id": "D9", "url": "nonsense"}]
    seen = []
    results = analyze_urls(records, analyzer=a, on_result=lambda i, r: seen.append(i))
    assert [r.document_id for r in results] == ["D0", "D1", "D2", "D3", "D9"] and sorted(seen) == [0, 1, 2, 3, 4]
    assert [r.analysis_status for r in results] == ["Success", "Failed", "Failed", "Success", "Failed"]
    assert results[0].page_count == "4" and results[0].book_title == "Book 0" and results[1].error == "HTTP 404 - Not found"


def test_duplicate_urls_fetched_once_and_cache_is_flagged():
    url = "https://example.gov/a.pdf"
    a, sess = make_analyzer({url: FakeResp(200, text_pdf(ENGLISH, 1), PDF_HEADERS)})
    res = analyze_urls([BookRecord(url=url, document_id="A"), BookRecord(url=url + "#x", document_id="B")], analyzer=a)
    assert [r.document_id for r in res] == ["A", "B"] and len(sess.calls) == 1 and not any(r.cached for r in res)
    again = a.analyze(url)
    assert len(sess.calls) == 1 and again.cached and again.analyzed_at == res[0].analyzed_at and again.page_count == "1"


def test_force_refresh_bypasses_the_cache():
    url = "https://example.gov/a.pdf"
    a, sess = make_analyzer({url: FakeResp(200, text_pdf(ENGLISH, 1), PDF_HEADERS)})
    analyze_urls([{"url": url}], analyzer=a)
    cached = analyze_urls([{"url": url}], analyzer=a)[0]
    fresh = analyze_urls([{"url": url}], analyzer=a, force_refresh=True)[0]
    assert cached.cached and not fresh.cached and len(sess.calls) == 2


def test_cache_expires_and_transient_errors_are_not_cached():
    url = "https://flaky.example/a.pdf"
    attempts = {"n": 0}

    def route(kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.ConnectionError("blip")
        return FakeResp(200, text_pdf(ENGLISH, 1), PDF_HEADERS)
    a, _ = make_analyzer({url: route})
    assert a.analyze(url).analysis_status == "Failed" and a.analyze(url).analysis_status == "Success"
    ok = make_analyzer({"https://example.gov/a": FakeResp(200, b"<html></html>", {"Content-Type": "text/html"})}, cache_ttl=0)[0]
    ok.analyze("https://example.gov/a")
    assert not ok.analyze("https://example.gov/a").cached


def test_per_host_limit_is_respected():
    live, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def route(kw):
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        time.sleep(0.05)
        with lock:
            live["n"] -= 1
        return FakeResp(200, b"<html></html>", {"Content-Type": "text/html"})
    routes = {f"https://same.example/{i}": route for i in range(8)}
    a, _ = make_analyzer(routes, max_workers=8, per_host_limit=2)
    analyze_urls([{"url": u} for u in routes], analyzer=a)
    assert peak["n"] <= 2


def test_empty_batch():
    assert analyze_urls([], analyzer=make_analyzer({})[0]) == []
