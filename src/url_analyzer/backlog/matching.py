"""Cross-match Power BI records to Reg Transform UI records and decide each record's Book Type.

Flow:   PBI BookSourceId  <->  UI DocumentId  ->  UI BookCategory  ->  Book Type

* The Power BI file only establishes the DocumentId <-> BookSourceId relationship.
* The Book Type is always the BookCategory of the matched Reg Transform UI record - nothing else is consulted
  (a BookCategory-like column in the Power BI file is ignored).
* No UI record for a BookSourceId  ->  ``Unmatched``  (no Book Type is assigned or guessed).
* Matched UI record with a blank BookCategory  ->  ``Unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..logger import log
from . import config as C
from .loader import BacklogError, find_col
from .normalize import is_blank, key_stem, last_segment, normalize_book_type, normalize_key


@dataclass
class MatchResult:
    book_type: pd.Series          # final Book Type per Power BI row (Unmatched / Unknown are explicit values)
    matched_mask: pd.Series       # bool per Power BI row: its BookSourceId exists as a UI DocumentId
    ui_key: str
    pbi_key: str
    key_source: str               # "configured" | "auto-detected"
    matched: int
    unmatched: int                # Power BI rows with no Reg Transform record
    ui_unmatched: int             # distinct Reg Transform DocumentIds that do not exist in Power BI
    duplicate_ui_keys: int        # distinct identifiers that appear more than once in the UI CSV
    duplicate_ui_rows: int        # surplus rows (occurrences beyond the first)
    conflicting_duplicates: int   # duplicated UI identifiers whose BookCategory values disagree
    duplicate_pbi_keys: int = 0
    duplicate_samples: list[str] = field(default_factory=list)
    blank_category: int = 0       # matched rows whose UI BookCategory is blank -> Unknown
    empty_pbi_keys: int = 0
    ui_urls: pd.Series | None = None   # UI Url per Power BI row (fallback for domains)
    ui_code: pd.Series | None = None       # UI Code of the matched record ('' when unmatched / blank)
    ui_template: pd.Series | None = None   # UI SpideringTemplate of the matched record
    code_column: str | None = None         # the UI column used (None when the CSV lacks it)
    template_column: str | None = None

    @property
    def book_type_raw(self) -> pd.Series:      # kept for callers that only need the decided value
        return self.book_type


def _candidates(df: pd.DataFrame, cols: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for want in cols:
        col = find_col(df, [want])
        if col is None:
            continue
        out[col] = normalize_key(df[col])
        stem = key_stem(df[col])
        out[f"{col} (stem)"] = stem
        out[f"{col} (last segment)"] = last_segment(stem)
    return out


def _coverage(ui_key: pd.Series, pbi_key: pd.Series) -> int:
    uset = set(ui_key.dropna())
    return int(pbi_key.isin(uset).sum()) if uset else 0


def _choose_keys(ui: pd.DataFrame, pbi: pd.DataFrame) -> tuple[pd.Series, pd.Series, str, str, str]:
    """Return (ui_key_series, pbi_key_series, ui_name, pbi_name, source). DocumentId <-> BookSourceId first."""
    for ui_want, pbi_want in C.MATCH_KEY_PAIRS:
        uc, pc = find_col(ui, [ui_want]), find_col(pbi, [pbi_want])
        if uc is None or pc is None:
            continue
        uk, pk = normalize_key(ui[uc]), normalize_key(pbi[pc])
        if _coverage(uk, pk) > 0:
            return uk, pk, uc, pc, "configured"
        log.warning("Configured key %s <-> %s matched nothing; falling back to auto-detection", uc, pc)

    ui_keys, pbi_keys = _candidates(ui, C.UI_KEY_COLS), _candidates(pbi, C.PBI_KEY_COLS)
    if not ui_keys:
        raise BacklogError(f"None of the identifier columns {C.UI_KEY_COLS} were found in the Reg Transform CSV.")
    if not pbi_keys:
        raise BacklogError(f"None of the identifier columns {C.PBI_KEY_COLS} were found in the Power BI file.")
    best = (0, None, None)
    for un, us in ui_keys.items():
        for pn, ps in pbi_keys.items():
            n = _coverage(us, ps)
            if n > best[0]:
                best = (n, un, pn)
    if best[0] == 0:
        raise BacklogError(
            "No Power BI records could be matched to the Reg Transform CSV on any identifier "
            f"({list(ui_keys)} vs {list(pbi_keys)}). Please check that both files are for the same day."
        )
    return ui_keys[best[1]], pbi_keys[best[2]], best[1], best[2], "auto-detected"


def _carry(pk: pd.Series, lookup: pd.DataFrame, name: str, col: str | None, matched: pd.Series) -> pd.Series:
    """A UI column mapped onto the Power BI rows through the same key; '' for unmatched rows / missing column."""
    if not col:
        return pd.Series("", index=pk.index, dtype=object)
    return pk.map(lookup[name]).where(matched, "").fillna("").astype(object)


def match_book_types(ui: pd.DataFrame, pbi: pd.DataFrame) -> MatchResult:
    bt_col = find_col(ui, C.UI_BOOK_TYPE_COLS)
    if bt_col is None:
        raise BacklogError(f"Required column '{C.UI_BOOK_TYPE_COLS[0]}' (Book Type) was not found "
                           "in the Reg Transform CSV.")
    uk, pk, ui_name, pbi_name, source = _choose_keys(ui, pbi)
    ui_cat_all = ui[bt_col].map(normalize_book_type)              # blank -> "Unknown"

    # Duplicates in the UI CSV: report them; the FIRST occurrence in file order is used (deterministic).
    keyed = pd.DataFrame({"k": uk, "bt": ui_cat_all}).dropna(subset=["k"])
    counts = keyed["k"].value_counts()
    dup_keys = counts[counts > 1]
    dup_conflicts, dup_samples = 0, []
    if len(dup_keys):
        grouped = keyed[keyed["k"].isin(dup_keys.index)].groupby("k")["bt"].nunique()
        dup_conflicts = int((grouped > 1).sum())
        dup_samples = [str(k) for k in dup_keys.index[:5]]
        log.warning("%d duplicate UI identifiers (%d surplus rows, %d with conflicting BookCategory); "
                    "first occurrence used. Examples: %s",
                    len(dup_keys), int((dup_keys - 1).sum()), dup_conflicts, ", ".join(dup_samples))

    lookup = pd.DataFrame({"k": uk, "bt": ui_cat_all})
    url_col = find_col(ui, C.UI_URL_COLS)
    if url_col:
        lookup["url"] = ui[url_col]
    code_col, tpl_col = find_col(ui, C.UI_CODE_COLS), find_col(ui, C.UI_TEMPLATE_COLS)
    for name, col in (("code", code_col), ("tpl", tpl_col)):
        if col:
            lookup[name] = ui[col].map(lambda v: "" if is_blank(v) else str(v).strip())
    lookup = lookup.dropna(subset=["k"]).drop_duplicates("k", keep="first").set_index("k")

    matched = pk.isin(lookup.index).astype(bool)
    final = pk.map(lookup["bt"]).where(matched, C.BT_UNMATCHED).fillna(C.BT_UNMATCHED).astype(object)
    pbi_dups = pk.dropna().value_counts()

    result = MatchResult(
        book_type=final, matched_mask=matched, ui_key=ui_name, pbi_key=pbi_name, key_source=source,
        matched=int(matched.sum()), unmatched=int((~matched).sum()),
        ui_unmatched=int(len(set(uk.dropna()) - set(pk.dropna()))),
        duplicate_ui_keys=len(dup_keys), duplicate_ui_rows=int((dup_keys - 1).sum()) if len(dup_keys) else 0,
        conflicting_duplicates=dup_conflicts, duplicate_pbi_keys=int((pbi_dups > 1).sum()),
        duplicate_samples=dup_samples, blank_category=int((final == C.BT_UNKNOWN).sum()),
        empty_pbi_keys=int(pk.isna().sum()), ui_urls=pk.map(lookup["url"]) if url_col else None,
        ui_code=_carry(pk, lookup, "code", code_col, matched), ui_template=_carry(pk, lookup, "tpl", tpl_col, matched),
        code_column=code_col, template_column=tpl_col,
    )
    log.info("PBI records: %d | UI records: %d | BookCategory matched: %d | Unmatched: %d | "
             "Duplicate UI identifiers: %d | blank BookCategory (Unknown): %d (key %s <-> %s, %s)",
             len(pbi), len(ui), result.matched, result.unmatched, result.duplicate_ui_keys, result.blank_category,
             ui_name, pbi_name, source)
    return result
