"""Vectorised views, filters and summaries over the analysed dataset."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config as C
from .normalize import is_blank, is_text


@dataclass
class FilterSpec:
    domains: list[str] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)       # canonical API Result labels
    book_types: list[str] = field(default_factory=list)
    extra: dict[str, list[str]] = field(default_factory=dict)  # label -> values
    q: str = ""
    code: str = ""        # contains-search on the UI Code
    template: str = ""    # contains-search on the UI SpideringTemplate
    date_from: str = ""
    date_to: str = ""


def view_mask(df: pd.DataFrame, view: str) -> pd.Series:
    """Rows belonging to a view. Action views only include PDF/Other books."""
    cats = C.VIEW_CATEGORIES.get(view)
    if cats is None:
        return pd.Series(True, index=df.index)
    return df["_cat"].isin(cats) & df["_in_scope"]


def apply_filters(df: pd.DataFrame, spec: FilterSpec, filter_cols: dict[str, str] | None = None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if spec.domains:
        mask &= df[C.DOMAIN_COL].isin(spec.domains)
    if spec.apis:
        mask &= df["_api_label"].isin(spec.apis)
    if spec.book_types:
        mask &= df[C.BOOK_TYPE_COL].isin(spec.book_types)
    for label, values in spec.extra.items():
        col = (filter_cols or {}).get(label)
        if values and col in df.columns:
            mask &= df[col].isin(values)
    for text, col in ((spec.code, C.CODE_COL), (spec.template, C.TEMPLATE_COL)):
        if text.strip():
            mask &= df[col].str.contains(text.strip(), case=False, regex=False, na=False)
    for token in spec.q.lower().split():
        mask &= df["_search"].str.contains(token, regex=False)
    if "_date" in df.columns:
        if spec.date_from:
            mask &= df["_date"] >= pd.to_datetime(spec.date_from, errors="coerce")
        if spec.date_to:
            end = pd.to_datetime(spec.date_to, errors="coerce")
            if pd.notna(end):
                mask &= df["_date"] < end + pd.Timedelta(days=1)
    return df[mask]


def cards(df: pd.DataFrame) -> dict:
    """Each card: total count and how many of those are in the PDF/Other action lists."""
    def card(sel: pd.Series) -> dict:
        return {"count": int(sel.sum()), "action": int((sel & df["_in_scope"]).sum())}

    cat = df["_cat"]
    return {
        "total": {"count": len(df), "action": int(df["_in_scope"].sum())},
        "pdf_other": {"count": int(df["_in_scope"].sum()), "action": int(df["_in_scope"].sum())},
        "change": card(cat == C.CAT_CHANGE),
        "potential": card(cat == C.CAT_POTENTIAL),
        "evc_open": card(cat == C.CAT_EVC_OPEN),
        "evc": card(cat == C.CAT_EVC),
        "dev": card(cat == C.CAT_DEV),
    }


def view_counts(df: pd.DataFrame) -> dict[str, int]:
    out = {C.VIEW_ALL: len(df)}
    for v in C.VIEW_CATEGORIES:
        out[v] = int(view_mask(df, v).sum())
    return out


def domain_summary(df: pd.DataFrame, limit: int = 1000) -> list[dict]:
    if df.empty:
        return []
    ct = pd.crosstab(df[C.DOMAIN_COL], df["_cat"])
    for c in (C.CAT_CHANGE, C.CAT_POTENTIAL, C.CAT_EVC_OPEN, C.CAT_EVC, C.CAT_DEV):
        if c not in ct.columns:
            ct[c] = 0
    out = pd.DataFrame({
        "domain": ct.index,
        "total": ct.sum(axis=1).values,
        "change": ct[C.CAT_CHANGE].values,
        "potential": ct[C.CAT_POTENTIAL].values,
        "evc": (ct[C.CAT_EVC_OPEN] + ct[C.CAT_EVC]).values,
        "dev": ct[C.CAT_DEV].values,
    }).sort_values(["total", "domain"], ascending=[False, True], kind="stable").head(limit)
    return out.to_dict("records")


def api_summary(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    g = df.groupby(["_api_label", "_cat"]).size().reset_index(name="count")
    g = g.sort_values(["count", "_api_label"], ascending=[False, True], kind="stable")
    return [{"label": a, "category": c, "count": int(n)}
            for a, c, n in zip(g["_api_label"], g["_cat"], g["count"])]


def ordered_book_types(values) -> list[str]:
    """Book Types present in the data: known ones in a fixed order, then any others alphabetically."""
    present = {str(v) for v in values}
    known = [b for b in C.BOOK_TYPE_ORDER if b in present]
    return known + sorted(present - set(known), key=str.lower)


def book_type_summary(df: pd.DataFrame) -> list[dict]:
    counts = df[C.BOOK_TYPE_COL].value_counts()
    return [{"label": b, "count": int(counts.get(b, 0))} for b in ordered_book_types(counts.index)]


def _cell(v) -> str | int | float:
    if is_blank(v):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float)):
        return v
    return str(v)


def default_columns(public_cols: list[str]) -> list[str]:
    lookup = {c.lower(): c for c in public_cols}
    cols = [lookup[c.lower()] for c in C.DEFAULT_TABLE_COLS if c.lower() in lookup]
    return cols or public_cols[:10]


def table_page(df: pd.DataFrame, columns: list[str], api_col: str, sort: str = "", asc: bool = True,
               page: int = 1, size: int = 50) -> list[dict]:
    size = max(1, min(size, C.MAX_PAGE_SIZE))
    if sort and sort in df.columns:
        key = df["_api_label"] if sort == api_col else df[sort]
        if is_text(key):
            key = key.map(lambda v: "" if is_blank(v) else str(v).lower())
        df = df.assign(_k=key).sort_values("_k", ascending=asc, kind="stable", na_position="last").drop(columns="_k")
    start = (max(page, 1) - 1) * size
    chunk = df.iloc[start:start + size]
    rows = []
    for _, r in chunk.iterrows():
        row = {c: (r["_api_label"] if c == api_col else _cell(r[c])) for c in columns if c in chunk.columns}
        row["_cat"] = r["_cat"]
        row["_row"] = int(r["_row"])
        rows.append(row)
    return rows


def public_view(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in df.columns if not str(c).startswith("_")]]
