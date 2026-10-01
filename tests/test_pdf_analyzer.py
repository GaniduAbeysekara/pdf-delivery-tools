"""PDF helper tests (the engine-level behaviour is covered in test_analysis_engine.py)."""
from url_analyzer.pdf_analyzer import _parse_pdf_date, inspect_pdf


def test_parse_pdf_date_standard():
    assert _parse_pdf_date("D:20231201143000") == "2023-12-01"


def test_parse_pdf_date_short():
    assert _parse_pdf_date("D:20240315") == "2024-03-15"


def test_parse_pdf_date_empty():
    assert _parse_pdf_date("") == ""


def test_empty_and_invalid_bytes_report_a_problem():
    assert inspect_pdf(b"").problem
    assert inspect_pdf(b"NOT A PDF CONTENT").problem


def test_real_pdf_page_count():
    from pypdf import PdfWriter
    import io
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(100, 100)
    buf = io.BytesIO()
    w.write(buf)
    info = inspect_pdf(buf.getvalue())
    assert info.pages == 3 and info.problem == ""
