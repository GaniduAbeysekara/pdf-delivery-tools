"""Book Type must come from the Reg Transform UI BookCategory of the matched record."""
import pandas as pd
import pytest

from url_analyzer.backlog.dataset import build_dataset
from url_analyzer.backlog.matching import match_book_types


def run(tmp_path, ui_rows, pbi_ids, pbi_dtype=str, pbi_cat=None, pbi_cat_name="BookCategory"):
    ui = pd.DataFrame(ui_rows, columns=["DocumentId", "BookCategory"])
    pbi = pd.DataFrame({"BookSourceId": pd.Series(pbi_ids).astype(pbi_dtype),
                        "API Result": "Change", "Link to the Issuance": "https://example.com/a"})
    if pbi_cat is not None:
        pbi[pbi_cat_name] = pbi_cat
    ui_path = tmp_path / "StartPointStatus-01-Oct-2026.csv"
    pbi_path = tmp_path / "Details_Table_-_2026-10-01T010101_000.xlsx"
    ui.to_csv(ui_path, index=False)
    pbi.to_excel(pbi_path, index=False)
    return build_dataset(str(ui_path), str(pbi_path))


def book_types(data):
    return dict(zip(data.export["BookSourceId"].astype(str), data.export["Book Type"]))


UI = [("A001", "PDF"), ("A002", "Other"), ("A003", "HTML"), ("A004", "HTML + PDF")]


@pytest.mark.parametrize("doc,expected", [("A001", "PDF"), ("A002", "Other"), ("A003", "HTML"), ("A004", "HTML + PDF")])
def test_book_type_is_ui_bookcategory(tmp_path, doc, expected):          # tests 1-3 (+HTML + PDF)
    data = run(tmp_path, UI, ["A001", "A002", "A003", "A004"])
    assert book_types(data)[doc] == expected
    assert data.stats["matched"] == 4 and data.stats["unmatched"] == 0


def test_unmatched_pbi_record_is_marked_unmatched_and_kept(tmp_path):    # test 4
    data = run(tmp_path, UI, ["A001", "ZZZ999"])
    assert len(data.export) == 2
    assert book_types(data)["ZZZ999"] == "Unmatched"
    assert data.stats["unmatched"] == 1 and data.stats["matched"] == 1
    assert any("no matching" in w and "Unmatched" in w for w in data.warnings)


def test_duplicate_ui_identifiers_are_detected_and_reported(tmp_path):   # test 5
    ui = UI + [("A001", "HTML"), ("A002", "Other")]       # A001 conflicts, A002 agrees
    data = run(tmp_path, ui, ["A001", "A002"])
    assert data.stats["duplicate_ui_keys"] == 2 and data.stats["duplicate_ui_rows"] == 2
    assert data.stats["conflicting_duplicates"] == 1
    assert book_types(data)["A001"] == "PDF"              # deterministic: first occurrence in file order
    assert any("more than once" in w and "a001" in w.lower() for w in data.warnings)


def test_match_is_robust_to_whitespace_case_and_numeric_ids(tmp_path):
    ui = [(" 1001 ", "PDF"), ("abc-1", "HTML")]
    data = run(tmp_path, ui, [1001, "ABC-1"], pbi_dtype=object)
    assert data.stats["matched"] == 2
    assert book_types(data) == {"1001": "PDF", "ABC-1": "HTML"}


def test_numeric_dtype_ids_match_text_ids():
    ui = pd.DataFrame({"DocumentId": ["1001", "1002"], "BookCategory": ["PDF", "Other"]})
    pbi = pd.DataFrame({"BookSourceId": [1001.0, 1002.0, None], "API Result": "Change"})
    m = match_book_types(ui, pbi)
    assert m.matched == 2 and m.unmatched == 1 and m.empty_pbi_keys == 1


def test_values_are_dynamic_not_hardcoded(tmp_path):
    ui = UI + [("A005", "Epub"), ("A006", None)]
    data = run(tmp_path, ui, ["A001", "A005", "A006"])
    assert book_types(data) == {"A001": "PDF", "A005": "Epub", "A006": "Unknown"}
    assert data.stats["blank_category"] == 1
    from url_analyzer.backlog.filters import ordered_book_types
    assert ordered_book_types(data.export["Book Type"]) == ["PDF", "Unknown", "Epub"]


def test_processing_statistics_present(tmp_path):
    data = run(tmp_path, UI, ["A001", "NOPE"])
    for key in ("powerbi_rows", "ui_rows", "matched", "unmatched", "duplicate_ui_keys"):
        assert key in data.stats
    assert (data.stats["powerbi_rows"], data.stats["ui_rows"]) == (2, 4)


# ── UI CSV BookCategory is the source; Power BI only provides the BookSourceId link ───────────────────────
def test_flow_pbi_booksourceid_to_ui_documentid_to_ui_bookcategory(tmp_path):
    ui = [("A001", "PDF"), ("A002", "HTML"), ("A003", "Other")]
    data = run(tmp_path, ui, ["A003", "A001", "A002"])                       # Power BI order differs from UI order
    assert data.export["Book Type"].tolist() == ["Other", "PDF", "HTML"]       # each row gets ITS record's category
    assert (data.stats["match_ui_key"], data.stats["match_pbi_key"]) == ("DocumentId", "BookSourceId")


def test_a_bookcategory_column_in_power_bi_is_ignored(tmp_path):
    data = run(tmp_path, [("A001", "HTML")], ["A001"], pbi_cat=["PDF"])
    assert book_types(data) == {"A001": "HTML"}                                # the UI value, not the Power BI one
    assert any("Book Category" in c or c == "BookCategory" for c in data.export.columns)   # still preserved as data


def test_blank_ui_category_is_unknown_and_unmatched_is_a_different_label(tmp_path):
    data = run(tmp_path, [("A001", None), ("A002", "PDF")], ["A001", "A002", "NOPE"])
    assert book_types(data) == {"A001": "Unknown", "A002": "PDF", "NOPE": "Unmatched"}
    assert data.stats["blank_category"] == 1 and data.stats["unmatched"] == 1 and data.stats["matched"] == 2
    assert any("blank BookCategory" in w for w in data.warnings)


def test_neither_unknown_nor_unmatched_books_enter_the_action_lists(tmp_path):
    from url_analyzer.backlog import filters as F
    data = run(tmp_path, [("A001", None), ("A002", "PDF")], ["A001", "A002", "NOPE"])
    assert F.view_mask(data.export, "change").tolist() == [False, True, False]
    assert len(data.export) == 3                                              # ... but nothing is dropped


def test_existing_valid_matches_are_unchanged(tmp_path):
    ui = [("A001", "pdf"), ("A002", " HTML + PDF "), ("A003", "Other")]
    data = run(tmp_path, ui, ["A001", "A002", "A003"])
    assert data.export["Book Type"].tolist() == ["PDF", "HTML + PDF", "Other"]
    assert data.stats["unmatched"] == 0 and data.stats["blank_category"] == 0


def test_missing_ui_category_column_is_a_clear_error(tmp_path):
    from url_analyzer.backlog.loader import BacklogError
    ui = pd.DataFrame({"DocumentId": ["A001"]})
    pbi = pd.DataFrame({"BookSourceId": ["A001"], "API Result": ["Change"]})
    with pytest.raises(BacklogError, match="BookCategory"):
        match_book_types(ui, pbi)


def test_ui_ids_missing_from_power_bi_are_counted(tmp_path):
    data = run(tmp_path, UI, ["A001"])
    assert data.stats["ui_unmatched"] == 3                                   # A002..A004 are not in Power BI


def test_input_files_are_released_after_loading(tmp_path):
    """An open handle on the Power BI workbook blocks deleting/renaming it on Windows."""
    run(tmp_path, UI, ["A001"])
    for f in tmp_path.iterdir():
        f.rename(f.with_name("moved_" + f.name))
        f.with_name("moved_" + f.name).unlink()
    assert list(tmp_path.iterdir()) == []


def test_partial_ui_export_triggers_a_prominent_low_match_warning(tmp_path):
    data = run(tmp_path, [("A001", "PDF")], ["A001"] + [f"X{i}" for i in range(9)])      # 1 of 10 matched
    assert data.stats["low_match"] is True and data.warnings[0].startswith("LOW MATCH")
    assert "partial or filtered" in data.warnings[0] and "10.0%" in data.warnings[0]
    assert sorted(set(data.export["Book Type"])) == ["PDF", "Unmatched"]


def test_full_export_has_no_low_match_warning(tmp_path):
    data = run(tmp_path, UI, ["A001", "A002", "A003", "A004"])
    assert data.stats["low_match"] is False and not any(w.startswith("LOW MATCH") for w in data.warnings)
