"""Document links WITHOUT a .pdf extension: detected by several independent signals, always verified by what the server
returns (so a hint can never create a false document). Includes the gazette.govt.nz markup that was missed before."""
import pandas as pd
import pytest

from tests.helpers import ENGLISH, PDF_HEADERS, FakeResp, make_analyzer, text_pdf
from url_analyzer import html_analyzer as H
from url_analyzer.analysis import export_analysis_results
from url_analyzer.analysis.document_analyzer import detect_content_type, disposition_filename

PAGE = "https://example.gov/notice/id/2014-au935"
HTML = {"Content-Type": "text/html; charset=utf-8"}


def page(body, head=""):
    return FakeResp(200, f"<html><head>{head}</head><body>{body}</body></html>".encode(), HTML)


def pdf(pages=3, **headers):
    return FakeResp(200, text_pdf(ENGLISH, pages), {**PDF_HEADERS, **headers})


def analyze(routes, url=PAGE, **settings):
    a, sess = make_analyzer(routes, **settings)
    return a.analyze(url), sess


# ── the reported case ────────────────────────────────────────────────────────
GAZETTE = ('<a class="pdf relative mt-8 w-full" href="/notice/id/2014-au935/pdf"><dl><dt class="sr-only">Title</dt>'
           '<dd>View PDF</dd><dd>Supplement: Registered Bank Disclosure Statements</dd></dl>'
           '<svg aria-hidden="true" class="h-6"></svg></a>')


def test_gazette_style_pdf_link_is_found_followed_and_analysed():
    doc = "https://example.gov/notice/id/2014-au935/pdf"
    r, _ = analyze({PAGE: page(GAZETTE), doc: pdf(5, **{"Content-Disposition": "attachment; filename=Supplement_RegBnk21Feb14.pdf"})})
    assert (r.url_type, r.document_link_type, r.document_link_count) == ("LANDING_PAGE", "SINGLE", 1)
    assert r.document_url == doc and r.page_count == "5" and r.language == "English" and r.analysis_status == "Success"


def test_the_link_text_pattern_really_matches_now():
    # regression: the pattern was once written with a backspace character instead of \b and never matched anything
    assert H._WORDS.search("Title View PDF Description Supplement") and H._WORDS.search("Download the report")
    assert not H._WORDS.search("pdfs and downloaders") and "\x08" not in H._WORDS.pattern


# ── independent signals (each alone is enough to become a candidate) ─────────
@pytest.mark.parametrize("anchor", [
    '<a href="/notice/9/pdf">Open</a>',                                  # path ends in /pdf
    '<a class="btn-pdf" href="/v/9">Open</a>',                           # class
    '<a class="icon" href="/v/9"><i class="fa fa-file-pdf"></i> Open</a>',   # class on an inner icon
    '<a type="application/pdf" href="/v/9">Open</a>',                    # declared mime type
    '<a download href="/v/9">Open</a>',                                  # download attribute
    '<a href="/v/9?format=pdf">Open</a>',                                # ?format=pdf
    '<a href="/v/9"><img src="/img/pdf.png" alt="PDF"></a>',             # icon alt text
    '<a href="/v/9" aria-label="Download full text">Open</a>',           # aria-label
    '<a href="/v/9" title="View PDF">Open</a>',                          # title
    '<a href="/get?fileId=9">Open</a>',                                  # file-id style URL
])
def test_each_signal_makes_a_candidate_and_verification_confirms_it(anchor):
    html = f"<div>{anchor}</div>"
    cands = H.find_candidate_links(html, PAGE)
    assert len(cands) == 1, anchor
    r, _ = analyze({PAGE: page(anchor), cands[0]: pdf(4)})
    assert (r.document_link_type, r.page_count) == ("SINGLE", "4"), anchor


def test_page_level_declarations_citation_pdf_url_and_alternate_link():
    head = '<meta name="citation_pdf_url" content="/paper/1/fulltext"><link rel="alternate" type="application/pdf" href="/paper/1/print">'
    assert H.find_candidate_links("<html></html>", PAGE) == []
    got = H.find_candidate_links(f"<html><head>{head}</head><body></body></html>", PAGE)
    assert got == ["https://example.gov/paper/1/fulltext", "https://example.gov/paper/1/print"]


def test_object_and_iframe_declared_as_pdf_without_extension():
    html = '<object type="application/pdf" data="/viewer?id=3"></object><iframe type="application/pdf" src="/embed/3"></iframe>'
    assert set(H.find_candidate_links(html, PAGE)) == {"https://example.gov/viewer?id=3", "https://example.gov/embed/3"}


def test_strongest_candidates_come_first_then_page_order():
    html = ('<a href="/weak1" title="Download">w1</a><a type="application/pdf" href="/strong">s</a>'
            '<a class="pdf" href="/medium">m</a><a href="/weak2" title="Download">w2</a>')
    assert [u.rsplit("/", 1)[-1] for u in H.find_candidate_links(html, PAGE)] == ["strong", "medium", "weak1", "weak2"]


# ── verification: hints never create a false document ────────────────────────
def test_a_pdf_class_on_a_link_that_returns_html_is_not_a_document():
    r, _ = analyze({PAGE: page('<a class="pdf" href="/about-pdfs">About</a>'), "https://example.gov/about-pdfs": page("info")})
    assert (r.document_link_type, r.document_link_count) == ("NONE", 0)


def test_download_links_to_non_documents_are_ignored():
    r, _ = analyze({PAGE: page('<a href="/app" title="Download our app">app</a>'),
                    "https://example.gov/app": FakeResp(200, b"MZ", {"Content-Type": "application/x-msdownload"})})
    assert r.document_link_type == "NONE"


def test_ordinary_links_without_any_signal_are_never_requested():
    r, sess = analyze({PAGE: page('<a href="/news">News</a><a href="/contact">Contact</a><a href="/notice/9">Notice</a>')})
    assert r.document_link_type == "NONE" and [c[1] for c in sess.calls] == [PAGE]


def test_self_links_anchors_and_script_urls_are_skipped():
    html = f'<a class="pdf" href="{PAGE}">self</a><a class="pdf" href="#top">top</a><a class="pdf" href="javascript:void(0)">js</a>'
    assert H.find_candidate_links(html, PAGE) == []


def test_two_urls_for_the_same_document_count_once():
    html = '<a class="pdf" href="/a/pdf">one</a><a class="pdf" href="/b/pdf">two</a>'
    r, _ = analyze({PAGE: page(html), "https://example.gov/a/pdf": FakeResp(302, b"", {"Location": "/files/x.pdf"}),
                    "https://example.gov/b/pdf": FakeResp(302, b"", {"Location": "/files/x.pdf"}), "https://example.gov/files/x.pdf": pdf(2)})
    assert (r.document_link_type, r.document_link_count, r.document_url) == ("SINGLE", 1, "https://example.gov/files/x.pdf")


def test_several_distinct_documents_are_multiple():
    html = '<a class="pdf" href="/a/pdf">a</a><a class="pdf" href="/b/pdf">b</a>'
    r, _ = analyze({PAGE: page(html), "https://example.gov/a/pdf": pdf(2), "https://example.gov/b/pdf": pdf(3)})
    assert (r.document_link_type, r.document_link_count) == ("MULTIPLE", 2) and r.page_count == "N/A"


def test_extension_links_and_candidates_combine():
    html = '<a href="/files/one.pdf">one</a><a class="pdf" href="/two/pdf">two</a>'
    r, _ = analyze({PAGE: page(html), "https://example.gov/two/pdf": pdf(2)})
    assert r.document_link_type == "MULTIPLE" and r.document_urls == ["https://example.gov/files/one.pdf", "https://example.gov/two/pdf"]


def test_probe_budget_is_bounded():
    html = "".join(f'<a class="pdf" href="/n{i}/pdf">x</a>' for i in range(40))
    routes = {PAGE: page(html), **{f"https://example.gov/n{i}/pdf": page("no") for i in range(40)}}
    r, sess = analyze(routes)
    assert r.document_link_type == "NONE" and len(sess.calls) <= 1 + 12


# ── download file name as evidence ───────────────────────────────────────────
def test_generic_content_type_with_pdf_filename_is_a_pdf():
    assert disposition_filename('attachment; filename="Report 2024.pdf"') == "Report 2024.pdf"
    assert disposition_filename("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.docx") == "r%C3%A9sum%C3%A9.docx"
    assert detect_content_type("application/octet-stream", "http://x/get?id=1", b"junk", "report.pdf") == "PDF"
    assert detect_content_type("application/octet-stream", "http://x/get?id=1", b"junk", "") == "Other"
    a, _ = make_analyzer({"https://example.gov/get?id=1": FakeResp(200, text_pdf(ENGLISH, 2), {
        "Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=report.pdf"})})
    r = a.analyze("https://example.gov/get?id=1")
    assert (r.url_type, r.page_count) == ("DIRECT_PDF", "2")


# ── the Content-Type header is always reported (image/gif and friends) ───────
@pytest.mark.parametrize("header,body,category", [
    ("image/gif", b"GIF89a....", "Other"), ("image/png", b"\x89PNG....", "Other"), ("application/json", b"{}", "Other"),
    ("text/html; charset=utf-8", b"<html></html>", "HTML"), ("application/pdf", text_pdf(ENGLISH, 1), "PDF")])
def test_header_content_type_is_captured_for_every_kind_of_response(header, body, category):
    r, _ = analyze({PAGE: FakeResp(200, body, {"Content-Type": header})})
    assert r.mime_type == header.split(";")[0] and r.content_type == category


def test_header_content_type_is_captured_even_for_error_responses():
    r, _ = analyze({PAGE: FakeResp(404, b"nope", {"Content-Type": "text/plain; charset=utf-8"})})
    assert (r.status, r.mime_type) == ("NOT_WORKING", "text/plain")


def test_header_content_type_in_the_export_and_page(tmp_path):
    a, _ = make_analyzer({PAGE: FakeResp(200, b"GIF89a....", {"Content-Type": "image/gif"})})
    path = export_analysis_results([a.analyze(PAGE)], tmp_path / "o.xlsx")
    df = pd.read_excel(path, dtype=object)
    cols = list(df.columns)
    assert df.loc[0, "Header Content-Type"] == "image/gif" and cols.index("Header Content-Type") == cols.index("Content Type") + 1
    from pathlib import Path
    html = (Path(__file__).parents[1] / "templates" / "url_tool.html").read_text(encoding="utf-8")
    assert "Header Content-Type" in html and "'mime_type'" in html
