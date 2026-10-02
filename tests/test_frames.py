"""Documents shown in an <iframe> / <embed> / <object> (and opened by buttons): found as landing-page documents.
When a viewer frame and a download link/button are both present, the same document is SINGLE and different ones MULTIPLE."""
import pytest

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer.analysis.inputs import document_key
from url_analyzer.html_analyzer import find_embedded_documents, unwrap_viewer

HTML = {"Content-Type": "text/html; charset=utf-8"}
PAGE = "https://example.gov/report"
A = "https://example.gov/docs/a.pdf"
B = "https://example.gov/docs/b.pdf"


def page(body):
    return FakeResp(200, f"<html><body>{body}</body></html>".encode(), HTML)


def pdf(pages=4):
    return FakeResp(200, text_pdf(ENGLISH, pages), {**PDF_HEADERS, "Content-Length": "2048"})


def run(body, extra=None):
    routes = {PAGE: page(body), A: pdf(4), B: pdf(9), **(extra or {})}
    a, sess = make_analyzer(routes)
    return a.analyze(PAGE), sess


def noted(r, text):
    return any(text in n for n in r.notes)


# ── a frame is a landing-page document ───────────────────────────────────────
def test_iframe_with_pdf_extension_is_a_single_document():
    r, _ = run(f'<iframe src="{A}"></iframe>')
    assert (r.url_type, r.document_link_type, r.document_link_count, r.document_url, r.page_count) == ("LANDING_PAGE", "SINGLE", 1, A, "4")
    assert noted(r, "shown in an embedded frame")


def test_viewer_wrapper_is_unwrapped_to_the_real_document():
    r, sess = run('<iframe src="/pdfjs/web/viewer.html?file=%2Fdocs%2Fa.pdf"></iframe>')
    assert (r.document_link_type, r.document_url, r.page_count) == ("SINGLE", A, "4")
    assert not any("viewer.html" in c[1] for c in sess.calls)          # the viewer page itself is never fetched


def test_google_viewer_wrapper():
    r, _ = run('<iframe src="https://docs.google.com/viewer?url=https%3A%2F%2Fexample.gov%2Fdocs%2Fa.pdf&embedded=true"></iframe>')
    assert (r.document_link_type, r.document_url) == ("SINGLE", A)


def test_frame_without_extension_that_serves_a_pdf():
    r, _ = run('<iframe src="/getdoc?id=5"></iframe>', {"https://example.gov/getdoc?id=5": pdf(6)})
    assert (r.document_link_type, r.page_count, r.document_url) == ("SINGLE", "6", "https://example.gov/getdoc?id=5")


def test_frame_holding_an_html_page_that_contains_the_pdf():
    inner = page(f'<a href="{A}">Download the report</a>')
    r, _ = run('<iframe src="/viewer-page"></iframe>', {"https://example.gov/viewer-page": inner})
    assert (r.document_link_type, r.document_url) == ("SINGLE", A) and noted(r, "embedded frame")


def test_frame_depth_is_bounded():
    # page -> frame page -> frame page is followed; a document only reachable through a further hop is not chased
    mid = page('<iframe src="/inner"></iframe>')
    inner = page('<iframe src="/innermost"></iframe>')
    innermost = page(f'<embed src="{A}">')
    r, sess = run('<iframe src="/mid"></iframe>', {"https://example.gov/mid": mid, "https://example.gov/inner": inner,
                                                    "https://example.gov/innermost": innermost})
    assert r.document_link_type == "NONE"
    assert not any(c[1].endswith("/innermost") for c in sess.calls)


def test_frame_page_whose_own_frame_holds_the_pdf():
    mid = page('<iframe src="/inner"></iframe>')
    r, _ = run('<iframe src="/mid"></iframe>', {"https://example.gov/mid": mid, "https://example.gov/inner": pdf(5)})
    assert (r.document_link_type, r.page_count) == ("SINGLE", "5")


def test_embed_and_object_tags():
    assert run(f'<embed src="{A}" type="application/pdf">')[0].document_link_type == "SINGLE"
    assert run(f'<object data="{A}" type="application/pdf"></object>')[0].document_url == A


def test_frame_redirecting_to_a_pdf_uses_the_final_address():
    r, _ = run('<iframe src="/open?id=9"></iframe>', {"https://example.gov/open?id=9": FakeResp(302, b"", {"Location": "/docs/a.pdf"})})
    assert (r.document_link_type, r.document_url) == ("SINGLE", A)


# ── iframe AND button/link: same document -> SINGLE, different -> MULTIPLE ───
def test_iframe_and_button_pointing_to_the_same_document_is_single():
    r, _ = run(f'<iframe src="{A}"></iframe><a class="btn" href="{A}">Download PDF</a>')
    assert (r.document_link_type, r.document_link_count) == ("SINGLE", 1)
    assert noted(r, "point to the same document - reported as SINGLE")


def test_same_document_through_a_viewer_and_a_relative_button():
    r, _ = run('<iframe src="/pdfjs/viewer.html?file=/docs/a.pdf"></iframe><a href="docs/a.pdf#page=2">Download</a>')
    assert (r.document_link_type, r.document_link_count) == ("SINGLE", 1)
    assert noted(r, "same document")


def test_same_document_when_only_scheme_www_or_case_differ():
    r, _ = run(f'<iframe src="{A}"></iframe><a href="http://WWW.example.gov/docs/a.pdf">Download</a>',
               {"http://WWW.example.gov/docs/a.pdf": pdf(4)})
    assert (r.document_link_type, r.document_link_count) == ("SINGLE", 1)


def test_iframe_and_button_pointing_to_different_documents_is_multiple():
    r, _ = run(f'<iframe src="{A}"></iframe><a href="{B}">Download</a>')
    assert (r.document_link_type, r.document_link_count) == ("MULTIPLE", 2)
    assert noted(r, "do not point to the same document") and noted(r, "reported as MULTIPLE")
    assert r.document_url == ""                        # never picks one at random


def test_frame_redirecting_to_the_button_target_counts_as_the_same_document():
    r, _ = run(f'<iframe src="/open?id=9"></iframe><a href="{A}">Download</a>',
               {"https://example.gov/open?id=9": FakeResp(302, b"", {"Location": "/docs/a.pdf"})})
    assert (r.document_link_type, r.document_link_count) == ("SINGLE", 1)


def test_button_element_with_click_handler_counts_as_a_link():
    r, _ = run(f'<iframe src="{A}"></iframe><button onclick="window.open(\'/docs/a.pdf\')">Download</button>')
    assert (r.document_link_type, r.document_link_count) == ("SINGLE", 1) and noted(r, "same document")
    r2, _ = run(f'<iframe src="{A}"></iframe><button data-href="/docs/b.pdf">Download</button>')
    assert r2.document_link_type == "MULTIPLE"


def test_button_alone_is_a_document_link():
    r, _ = run('<button onclick="location.href=\'/docs/a.pdf\'">Get PDF</button>')
    assert (r.document_link_type, r.document_url) == ("SINGLE", A)


def test_plain_link_alone_unchanged_and_no_frame_note():
    r, _ = run(f'<a href="{A}">Download</a>')
    assert r.document_link_type == "SINGLE" and not noted(r, "iframe")


# ── frames that are not documents ────────────────────────────────────────────
def test_video_hidden_and_failing_frames_are_ignored():
    body = ('<iframe src="https://www.youtube.com/embed/abc"></iframe><iframe width="1" height="1" src="/pixel"></iframe>'
            '<iframe src="about:blank"></iframe><iframe src="/gone"></iframe>')
    r, sess = run(body, {"https://example.gov/gone": FakeResp(404, b"", HTML)})
    assert r.document_link_type == "NONE"
    assert not any("youtube" in c[1] or "/pixel" in c[1] for c in sess.calls)


def test_frame_to_an_internal_address_is_refused():
    r, sess = run('<iframe src="http://127.0.0.1/admin"></iframe>', {})
    assert r.document_link_type == "NONE" and not any("127.0.0.1" in c[1] for c in sess.calls)


def test_frame_pdf_and_link_pdf_are_counted_once_when_identical():
    r, _ = run(f'<iframe src="{A}"></iframe><embed src="{A}"><a href="{A}">x</a>')
    assert r.document_link_count == 1


# ── building blocks ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b,same", [
    ("https://Example.gov/a.pdf", "http://www.example.gov/a.pdf", True),
    ("https://example.gov/a.pdf#page=3", "https://example.gov/a.pdf", True),
    ("https://example.gov/my%20file.pdf", "https://example.gov/my file.pdf", True),
    ("https://example.gov/a.pdf", "https://example.gov/b.pdf", False),
    ("https://example.gov/get?id=1", "https://example.gov/get?id=2", False),
    ("https://example.gov/a.pdf", "https://other.gov/a.pdf", False),
])
def test_document_key(a, b, same):
    assert (document_key(a) == document_key(b)) is same


def test_unwrap_viewer():
    assert unwrap_viewer("https://s.gov/web/viewer.html?file=%2Fa.pdf") == "https://s.gov/a.pdf"
    assert unwrap_viewer("https://view.officeapps.live.com/op/embed.aspx?src=https%3A%2F%2Fs.gov%2Fa.docx") == "https://s.gov/a.docx"
    assert unwrap_viewer("https://drive.google.com/file/d/XYZ/preview") == "https://drive.google.com/uc?export=download&id=XYZ"
    assert unwrap_viewer("https://s.gov/page?file=x") == "https://s.gov/page?file=x"          # not a viewer: unchanged
    assert unwrap_viewer("https://s.gov/viewer?file=javascript:alert(1)") == "https://s.gov/viewer?file=javascript:alert(1)"


def test_find_embedded_documents_order_and_limit():
    html = "".join(f'<iframe src="/f{i}"></iframe>' for i in range(12))
    assert [f["target"] for f in find_embedded_documents(html, PAGE, limit=3)] == [f"https://example.gov/f{i}" for i in range(3)]


# ── a PDF.js viewer mounted at an arbitrary path (e.g. sebi.gov.in uses /web/?file=<absolute pdf url>) ──
def test_viewer_at_a_generic_path_with_an_absolute_file_parameter():
    base = "https://www.sebi.gov.in/legal/master-circulars/may-2024/master-circular-_83689.html"
    pdf_url = "https://www.sebi.gov.in/sebi_data/attachdocs/may-2024/1717066343989.pdf"
    body = (f"<html><body><iframe src='../../../web/?file={pdf_url}' width='100%' style='max-height:90%; height:600px;' "
            "title='Master Circular' allowfullscreen></iframe></body></html>")
    a, sess = make_analyzer({base: FakeResp(200, body.encode(), HTML), pdf_url: pdf(8)})
    r = a.analyze(base)
    assert (r.url_type, r.document_link_type, r.document_link_count, r.document_url, r.page_count) == (
        "LANDING_PAGE", "SINGLE", 1, pdf_url, "8")
    assert not any("/web/" in c[1] for c in sess.calls)           # the viewer page itself is never fetched


def test_unwrap_viewer_generic_path_rules():
    assert unwrap_viewer("https://s.gov/web/?file=https://s.gov/d/a.pdf") == "https://s.gov/d/a.pdf"
    assert unwrap_viewer("https://s.gov/web/?file=%2Fd%2Fa.pdf%3Fv%3D2") == "https://s.gov/d/a.pdf?v=2"
    assert unwrap_viewer("https://s.gov/anything?url=https://s.gov/a.docx#p=1") == "https://s.gov/a.docx"
    # a parameter that is not a document address leaves an ordinary page alone
    assert unwrap_viewer("https://s.gov/search?file=report") == "https://s.gov/search?file=report"
    assert unwrap_viewer("https://s.gov/page?url=https://other.org/home") == "https://s.gov/page?url=https://other.org/home"
