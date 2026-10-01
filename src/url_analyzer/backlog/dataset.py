"""Build the analysed backlog dataset (normalise once, reuse everywhere)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

from ..logger import log
from . import config as C
from .loader import (BacklogError, find_col, read_powerbi, read_ui_csv, require_column,
                     validate_path)
from .matching import match_book_types
from .normalize import (canonical_labels, categorize_api, derive_report_date, extract_domain,
                        is_blank, normalize_api_result, normalize_book_type)

Progress = Callable[[str], None]


@dataclass
class BacklogData:
    export: pd.DataFrame            # Power BI data + Book Type + Domain + helper (_*) columns
    ui: pd.DataFrame
    report_date: date
    ui_name: str
    pbi_name: str
    stats: dict
    warnings: list[str] = field(default_factory=list)
    api_col: str = "API Result"
    id_col: str | None = None
    title_col: str | None = None
    link_col: str | None = None
    date_col: str | None = None
    filter_cols: dict[str, str] = field(default_factory=dict)  # label -> helper column

    @property
    def public_columns(self) -> list[str]:
        return [c for c in self.export.columns if not str(c).startswith("_")]

    @property
    def output_stem(self) -> str:
        return Path(self.pbi_name).stem


def _step(progress: Progress | None, msg: str) -> None:
    log.info(msg)
    if progress:
        progress(msg)


def build_dataset(ui_path: str, pbi_path: str, progress: Progress | None = None) -> BacklogData:
    ui_file = validate_path(ui_path, "Reg Transform UI", (".csv",))
    pbi_file = validate_path(pbi_path, "Power BI", (".xlsx", ".xls"))
    warnings: list[str] = []

    _step(progress, "Loading Reg Transform UI data...")
    ui, skipped, w = read_ui_csv(str(ui_file))
    warnings += w
    _step(progress, "Loading Power BI data...")
    pbi = read_powerbi(str(pbi_file))

    api_col = require_column(pbi, C.PBI_API_COLS, "Power BI file", "API Result")

    _step(progress, "Matching Book Types...")
    match = match_book_types(ui, pbi)

    export = pbi.copy()
    for derived in (C.BOOK_TYPE_COL, C.DOMAIN_COL):
        if derived in export.columns:
            warnings.append(f"The Power BI file already had a '{derived}' column; it was replaced.")
    low_match = match.matched < C.MIN_MATCH_RATIO * max(len(pbi), 1)
    if low_match:
        warnings.insert(0, (
            f"LOW MATCH: only {match.matched:,} of {len(pbi):,} Power BI records ({match.matched / max(len(pbi), 1):.1%}) "
            f"were found in the Reg Transform CSV, which has just {len(ui):,} rows. This looks like a partial or filtered "
            f"export, so {match.unmatched:,} records show Book Type 'Unmatched' and the Book Type list and counts are "
            "incomplete. Upload the full StartPointStatus export for the same day."))
    # Book Type = BookCategory of the matched Reg Transform record. The Power BI file only provides the link.
    export[C.BOOK_TYPE_COL] = match.book_type.values
    if match.key_source != "configured":
        warnings.append(f"The configured match key was not usable; records were matched on "
                        f"UI '{match.ui_key}' <-> Power BI '{match.pbi_key}' (auto-detected).")
    if match.unmatched:
        warnings.append(f"{match.unmatched} Power BI record(s) have no matching Reg Transform record: Book Type is "
                        "'Unmatched' (no Book Type was assigned). They stay in the data but are not in the "
                        "PDF/Other action lists.")
    if match.blank_category:
        warnings.append(f"{match.blank_category} matched record(s) have a blank BookCategory in the Reg Transform CSV: "
                        "Book Type is 'Unknown' (not in the PDF/Other action lists).")
    if match.duplicate_ui_keys:
        msg = (f"{match.duplicate_ui_keys} identifier(s) appear more than once in the Reg Transform CSV "
               f"({match.duplicate_ui_rows} surplus rows); the first occurrence in the file was used")
        if match.conflicting_duplicates:
            msg += f", and {match.conflicting_duplicates} of them have conflicting BookCategory values"
        warnings.append(msg + f". Examples: {', '.join(match.duplicate_samples)}.")
    if match.duplicate_pbi_keys:
        warnings.append(f"{match.duplicate_pbi_keys} BookSourceId value(s) appear more than once in the Power BI file; "
                        "each row gets the same Book Type.")

    _step(progress, "Extracting Domains...")
    link_col = find_col(export, C.PBI_LINK_COLS)
    if link_col:
        links = export[link_col]
    elif match.ui_urls is not None:
        links = match.ui_urls
        warnings.append("No source-link column in the Power BI file; domains were taken from the "
                        "Reg Transform CSV 'Url' column.")
    else:
        links = pd.Series([None] * len(export), index=export.index)
        warnings.append("No source-link column was found; all domains are 'Unknown'.")
    export[C.DOMAIN_COL] = links.map(extract_domain)

    _step(progress, "Generating backlog summaries...")
    api_norm = export[api_col].map(normalize_api_result)
    cat_of = {n: categorize_api(n) for n in api_norm.unique()}
    export["_api"] = api_norm
    export["_api_label"] = canonical_labels(export[api_col], api_norm)
    export["_cat"] = api_norm.map(cat_of)
    export["_row"] = range(len(export))
    export["_in_scope"] = export[C.BOOK_TYPE_COL].isin(C.ACTION_BOOK_TYPES)
    export["_matched"] = match.matched_mask.values

    id_col = find_col(export, C.PBI_ID_COLS)
    title_col = find_col(export, C.PBI_TITLE_COLS)
    parts = [export[c].fillna("").astype(str) for c in (id_col, title_col, link_col) if c]
    parts += [export[C.DOMAIN_COL], export["_api_label"]]
    search = parts[0]
    for p in parts[1:]:
        search = search + " | " + p
    export["_search"] = search.str.lower()

    filter_cols: dict[str, str] = {}
    for label, cands in C.OPTIONAL_FILTERS:
        col = find_col(export, cands)
        if col is None:
            continue
        vals = export[col].map(lambda v: "(blank)" if is_blank(v) else str(v).strip())
        n = vals.nunique()
        if 1 < n <= C.MAX_FILTER_OPTIONS:
            helper = f"_f_{label}"
            export[helper] = vals
            filter_cols[label] = helper

    date_col = None
    for cand in C.DATE_COLS:
        col = find_col(export, [cand])
        if col is not None:
            parsed = pd.to_datetime(export[col], errors="coerce")
            if parsed.notna().any():
                export["_date"] = parsed
                date_col = col
                break

    bt = export[C.BOOK_TYPE_COL]
    stats = {
        "powerbi_rows": len(pbi), "ui_rows": len(ui), "ui_rows_skipped": skipped,
        "matched": match.matched, "unmatched": match.unmatched,
        "duplicate_ui_keys": match.duplicate_ui_keys, "duplicate_ui_rows": match.duplicate_ui_rows,
        "conflicting_duplicates": match.conflicting_duplicates, "blank_category": match.blank_category,
        "duplicate_pbi_keys": match.duplicate_pbi_keys, "ui_unmatched": match.ui_unmatched,
        "low_match": low_match, "match_ui_key": match.ui_key, "match_pbi_key": match.pbi_key, "match_source": match.key_source,
        "book_type_counts": {str(k): int(v) for k, v in bt.value_counts().items()},
        "pdf_other": int(bt.isin(C.ACTION_BOOK_TYPES).sum()),
        "html_or_html_pdf": int(bt.isin([C.BT_HTML, C.BT_HTML_PDF]).sum()),
    }
    report_date = derive_report_date(ui_file.name, pbi_file.name)
    data = BacklogData(
        export=export, ui=ui, report_date=report_date, ui_name=ui_file.name, pbi_name=pbi_file.name,
        stats=stats, warnings=warnings, api_col=api_col, id_col=id_col, title_col=title_col,
        link_col=link_col, date_col=date_col, filter_cols=filter_cols,
    )
    from .filters import view_mask
    for sheet, view in C.ACTION_SHEETS.items():
        n = int(view_mask(export, view).sum())
        stats.setdefault("action_counts", {})[sheet] = n
        log.info("%s: %d records", sheet, n)
    log.info("PDF/Other records: %d; HTML/HTML + PDF records: %d",
             stats["pdf_other"], stats["html_or_html_pdf"])
    return data
