"""Input adapters: pasted URLs, pasted book data, and Excel upload (URL-column detection, source-column preservation)."""
import pandas as pd
import pytest

from url_analyzer.analysis import InputError, inspect_excel, parse_book_data, parse_pasted, parse_urls, read_excel_records


# ── pasted text ──────────────────────────────────────────────────────────────
def test_parse_urls_lines_excel_column_and_duplicates():
    text = ("https://example.com/a.pdf\r\n\r\n  https://example.com/b.pdf  \nhttps://EXAMPLE.com/a.pdf\n"
            "www.example.org/c.pdf\nnot a url\nhttps://example.com/d.docx#page=2\n")
    out = parse_urls(text)
    assert [i.url for i in out.items] == ["https://example.com/a.pdf", "https://example.com/b.pdf",
                                          "https://www.example.org/c.pdf", "not a url", "https://example.com/d.docx#page=2"]
    assert out.duplicates_removed == 1 and out.invalid_lines == 1 and out.items[0].domain == "example.com"


def test_parse_urls_empty_and_trailing_punctuation():
    assert parse_urls("").items == [] and parse_urls("  \n\n ").items == []
    assert parse_urls("See (https://example.com/x.pdf).").items[0].url == "https://example.com/x.pdf"


def test_parse_book_data_with_header_from_excel():
    text = ("Document ID\tBook Title\tSource Link\tAPI Result\tNote\n"
            "A001\tAnnual Report 2025\thttps://example.gov/a.pdf\tChange\tcheck\n"
            "A002\tGuidance Note\thttps://example.gov/b.pdf\tChange\t\n"
            "A003\tNo link here\t\tChange\t\n")
    out = parse_book_data(text)
    assert [(i.document_id, i.book_title, i.url, i.api_result) for i in out.items] == [
        ("A001", "Annual Report 2025", "https://example.gov/a.pdf", "Change"),
        ("A002", "Guidance Note", "https://example.gov/b.pdf", "Change")]
    assert out.items[0].extra == {"Note": "check"} and out.rows_without_url == 1 and out.source_columns == ["Note"]


def test_parse_book_data_without_header_and_free_text():
    rows = parse_book_data("0035949a-d378-49cf-b7ce-9a508a21213e\tCode of Practice for work\thttps://wsa.gov.au/c.pdf")
    assert rows.items[0].document_id.startswith("0035949a") and rows.items[0].book_title == "Code of Practice for work"
    free = parse_book_data("ABC-123 Annual Report https://example.com/r.pdf\nsee https://example.com/s.pdf for more")
    assert [i.url for i in free.items] == ["https://example.com/r.pdf", "https://example.com/s.pdf"]
    assert free.items[0].document_id == "ABC-123" and "Annual Report" in free.items[0].book_title


def test_auto_mode_picks_book_parser_only_for_tabular_text():
    assert parse_pasted("A001\tTitle words\thttps://x.gov/a.pdf").items[0].document_id == "A001"
    assert parse_pasted("https://x.gov/a.pdf\nhttps://x.gov/b.pdf").items[1].document_id == ""


# ── Excel ────────────────────────────────────────────────────────────────────
def workbook(tmp_path, data, name="in.xlsx"):
    p = tmp_path / name
    pd.DataFrame(data).to_excel(p, index=False)
    return str(p)


def test_url_column_detected_by_header_name(tmp_path):
    p = workbook(tmp_path, {"Document ID": ["A1", "A2"], "Book Title": ["X", "Y"], "Source Link": ["https://a.gov/1.pdf", "https://a.gov/2.pdf"]})
    ins = inspect_excel(p)
    assert (ins.url_column, ins.confident, ins.id_column, ins.title_column) == ("Source Link", True, "Document ID", "Book Title")
    assert ins.row_count == 2 and ins.preview[0]["Book Title"] == "X"


@pytest.mark.parametrize("header", ["URL", "Source URL", "Source Link", "Link", "Document URL"])
def test_common_url_header_names(tmp_path, header):
    ins = inspect_excel(workbook(tmp_path, {"Other": ["a"], header: ["https://a.gov/1.pdf"]}))
    assert ins.url_column == header and ins.confident


def test_url_column_detected_by_contents_when_header_is_unhelpful(tmp_path):
    ins = inspect_excel(workbook(tmp_path, {"Ref": ["A1", "A2"], "Where": ["https://a.gov/1.pdf", "http://b.org/2"]}))
    assert ins.url_column == "Where" and ins.confident


def test_ambiguous_or_missing_url_column_is_not_confident(tmp_path):
    both = inspect_excel(workbook(tmp_path, {"URL": ["https://a.gov/1"], "Source URL": ["https://b.gov/1"]}))
    assert both.url_column is not None and not both.confident and set(both.candidates) == {"URL", "Source URL"}
    none = inspect_excel(workbook(tmp_path, {"A": ["x", "y"], "B": ["1", "2"]}, "none.xlsx"))
    assert none.url_column is None and not none.confident
    with pytest.raises(InputError, match="No URL column"):
        read_excel_records(workbook(tmp_path, {"A": ["x"]}, "none2.xlsx"))


def test_read_records_preserves_every_source_column_and_row(tmp_path):
    p = workbook(tmp_path, {"Document ID": ["A001", "A002", "A003", "A004"], "Book Title": ["R A", "R B", "R C", "R D"],
                            "URL": ["https://a.gov/a.pdf", "", "a.gov/c.pdf", "https://a.gov/a.pdf"],
                            "Jurisdiction": ["UK", "US", "FR", "UK"], "Priority": [1, 2, 3, 4]})
    d = read_excel_records(p, "URL", "Document ID", "Book Title")
    assert [(r.document_id, r.book_title, r.url) for r in d.records] == [
        ("A001", "R A", "https://a.gov/a.pdf"), ("A003", "R C", "https://a.gov/c.pdf"), ("A004", "R D", "https://a.gov/a.pdf")]
    assert d.records[0].extra == {"Jurisdiction": "UK", "Priority": "1"} and d.source_columns == ["Jurisdiction", "Priority"]
    assert d.skipped_blank == 1 and any("repeat a URL" in n for n in d.notes)        # repeated URL rows are kept


def test_read_records_validates_columns(tmp_path):
    p = workbook(tmp_path, {"URL": ["https://a.gov/1"]})
    with pytest.raises(InputError, match="does not exist"):
        read_excel_records(p, "Nope")
    with pytest.raises(InputError, match="no values"):                      # an entirely empty column
        read_excel_records(workbook(tmp_path, {"URL": [None, None], "x": [1, 2]}, "blank.xlsx"), "URL")


def test_unreadable_workbook(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not excel")
    with pytest.raises(InputError, match="could not be read"):
        inspect_excel(str(bad))
