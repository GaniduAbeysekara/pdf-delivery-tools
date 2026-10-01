"""Tests for HTML analyzer."""
from __future__ import annotations

import pytest

from src.url_analyzer.html_analyzer import find_document_links


BASE = "https://example.com"

HTML_SINGLE_PDF = """
<html><body>
  <a href="/files/report.pdf">Download PDF</a>
  <a href="/about">About us</a>
</body></html>
"""

HTML_MULTIPLE_PDFS = """
<html><body>
  <a href="/files/chapter1.pdf">Chapter 1</a>
  <a href="/files/chapter2.pdf">Chapter 2</a>
  <a href="/files/appendix.pdf">Appendix</a>
</body></html>
"""

HTML_NO_PDF = """
<html><body>
  <a href="/about">About</a>
  <a href="/contact">Contact</a>
</body></html>
"""

HTML_RELATIVE_PDF = """
<html><body>
  <a href="documents/report.pdf">Report</a>
</body></html>
"""

HTML_DUPLICATE_PDFS = """
<html><body>
  <a href="/files/report.pdf">Download PDF</a>
  <a href="/files/report.pdf">View PDF</a>
  <a href="/files/report.pdf">PDF Version</a>
</body></html>
"""

HTML_DOCX = """
<html><body>
  <a href="/files/policy.docx">Download Policy</a>
</body></html>
"""

HTML_MIXED = """
<html><body>
  <a href="/files/report.pdf">PDF Report</a>
  <a href="/files/data.docx">Word Document</a>
</body></html>
"""


def test_single_pdf():
    links = find_document_links(HTML_SINGLE_PDF, BASE)
    assert len(links) == 1
    assert links[0].url == "https://example.com/files/report.pdf"
    assert links[0].document_type == "PDF"


def test_multiple_pdfs():
    links = find_document_links(HTML_MULTIPLE_PDFS, BASE)
    assert len(links) == 3


def test_no_documents():
    links = find_document_links(HTML_NO_PDF, BASE)
    assert len(links) == 0


def test_relative_url_resolved():
    links = find_document_links(HTML_RELATIVE_PDF, BASE)
    assert len(links) == 1
    assert links[0].url.startswith("https://example.com")


def test_deduplication():
    links = find_document_links(HTML_DUPLICATE_PDFS, BASE)
    assert len(links) == 1


def test_docx_detected():
    links = find_document_links(HTML_DOCX, BASE)
    assert len(links) == 1
    assert links[0].document_type == "DOCX"


def test_mixed_types():
    links = find_document_links(HTML_MIXED, BASE)
    assert len(links) == 2
    types = {l.document_type for l in links}
    assert "PDF" in types
    assert "DOCX" in types


def test_empty_html():
    links = find_document_links("", BASE)
    assert links == []
