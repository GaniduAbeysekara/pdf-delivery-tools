"""Bot-protection (verification) pages must be reported as BLOCKED - never as "this page has no documents".
Based on the Bank of Israel page: HTTP 200 + a Radware 'Loader page' with no links."""
import pytest

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer.analysis import analyze_urls, summarize
from url_analyzer.html_analyzer import detect_bot_protection, find_document_links

PAGE = "https://www.boi.example/roles/public-reports/report/"
HTML = {"Content-Type": "text/html; charset=UTF-8"}
RADWARE = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" /><title>Radware Page </title>'
           '<meta name="description" content="Loader page." /></head><body>'
           '<script>var __uzdbm_1 = "76c412a1";var __uzdbm_2 = "YWVk";</script></body></html>')
# what the real page looks like once a browser has run the challenge
REAL = ('<html><body><h1>Report</h1><a href="/about">About</a>'
        '<a class="btn btn-primary download-button" href="/media/dc2bv5uw/692.pdf">להורדת הקובץ</a></body></html>')


def resp(body, status=200):
    return FakeResp(status, body.encode("utf-8"), HTML)


class Renderer:
    def __init__(self, html):
        self.html, self.calls = html, []

    def render(self, url):
        self.calls.append(url)
        return self.html


# ── detection ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("vendor,html", [
    ("Radware", RADWARE),
    ("Cloudflare", "<html><head><title>Just a moment...</title></head><body>Checking</body></html>"),
    ("Cloudflare", '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>'),
    ("Imperva/Incapsula", '<iframe src="/_Incapsula_Resource?SWJIYLWA=1"></iframe>'),
    ("Akamai", "<h1>Access Denied</h1>Reference&#32;#18.1 https://errors.edgesuite.net/18.1"),
    ("DataDome", '<script src="https://ct.captcha-delivery.com/c.js"></script>'),
    ("PerimeterX", '<div id="px-captcha"></div>'),
    ("Sucuri", "<title>Sucuri WebSite Firewall - Access Denied</title>"),
    ("a browser check", "<p>Please enable JavaScript and cookies to continue</p>"),
])
def test_known_challenge_pages_are_recognised(vendor, html):
    assert detect_bot_protection(html) == vendor


def test_normal_pages_are_not_mistaken_for_challenges():
    for html in (REAL, "<html><title>Annual report</title><body>We enable JavaScript features. Cookies policy.</body></html>",
                 "<html><body>An article about Cloudflare and Radware products.</body></html>", "", None):
        assert detect_bot_protection(html) == ""


# ── engine ───────────────────────────────────────────────────────────────────
def test_challenge_page_is_blocked_not_no_document():
    a, _ = make_analyzer({PAGE: resp(RADWARE)})
    r = a.analyze(PAGE)
    assert (r.status, r.url_type, r.analysis_status) == ("BLOCKED", "UNKNOWN", "Unknown")
    assert (r.document_link_type, r.document_link_count, r.document_urls) == ("", None, [])
    assert r.http_status == 200 and r.mime_type == "text/html"                       # the header is still reported
    assert "Radware" in r.error and "browser fallback" in r.error


def test_blocked_pages_are_not_counted_as_no_document_link():
    a, _ = make_analyzer({PAGE: resp(RADWARE), "https://x.example/p": resp("<html>nothing</html>")})
    got = dict(summarize(analyze_urls([{"url": PAGE}, {"url": "https://x.example/p"}], analyzer=a)))
    assert got["No Document Link"] == 1 and got["Not Working"] == 1 and got["Landing Page"] == 1


def test_a_page_with_links_is_never_treated_as_blocked_even_if_it_mentions_a_challenge_script():
    html = '<html><script src="/cdn-cgi/challenge-platform/x.js"></script><a href="/files/r.pdf">r</a></html>'
    a, _ = make_analyzer({PAGE: resp(html), "https://www.boi.example/files/r.pdf": FakeResp(200, text_pdf(ENGLISH, 3), PDF_HEADERS)})
    r = a.analyze(PAGE)
    assert (r.status, r.document_link_type, r.page_count) == ("WORKING", "SINGLE", "3")


def test_browser_fallback_that_gets_the_real_page_finds_the_download_link():
    pdf = "https://www.boi.example/media/dc2bv5uw/692.pdf"
    rend = Renderer(REAL)
    a, _ = make_analyzer({PAGE: resp(RADWARE), pdf: FakeResp(200, text_pdf(ENGLISH, 12), PDF_HEADERS)}, )
    a.renderer = rend
    r = a.analyze(PAGE)
    assert rend.calls == [PAGE]
    assert (r.status, r.document_link_type, r.document_url, r.page_count) == ("WORKING", "SINGLE", pdf, "12")
    assert any("rendering the page in a browser" in n for n in r.notes)


def test_browser_fallback_that_is_blocked_too_says_so():
    a, _ = make_analyzer({PAGE: resp(RADWARE)})
    a.renderer = Renderer(RADWARE)
    r = a.analyze(PAGE)
    assert r.status == "BLOCKED" and "browser fallback was blocked too" in r.error


def test_browser_fallback_that_renders_a_real_page_without_documents_is_none():
    a, _ = make_analyzer({PAGE: resp(RADWARE)})
    a.renderer = Renderer("<html><body><p>Just text, no files.</p></body></html>")
    assert a.analyze(PAGE).document_link_type == "NONE"


def test_unavailable_browser_fallback_keeps_the_blocked_verdict():
    a, _ = make_analyzer({PAGE: resp(RADWARE)})
    a.renderer = Renderer("")                                                          # e.g. Playwright not installed
    assert a.analyze(PAGE).status == "BLOCKED"


def test_the_real_page_markup_yields_a_single_document_link():
    assert [l.url for l in find_document_links(REAL, PAGE)] == ["https://www.boi.example/media/dc2bv5uw/692.pdf"]
