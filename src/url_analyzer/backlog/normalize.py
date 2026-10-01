"""Value normalisation: API Result, Book Type, domains, identifiers."""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

import pandas as pd

from . import config as C

_DASHES = re.compile(r"[‐-―�]")
_DEV_RE = re.compile(C.DEV_ISSUE_PATTERN)
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def is_text(series: pd.Series) -> bool:
    """True for object or pandas string dtypes (pandas 3 uses a dedicated str dtype)."""
    return series.dtype == object or pd.api.types.is_string_dtype(series.dtype)


def is_blank(v) -> bool:
    return v is None or (not isinstance(v, (str, bytes)) and bool(pd.isna(v))) or str(v).strip() == ""


def normalize_api_result(v) -> str:
    """Trim, collapse whitespace, unify dashes, lowercase. Blank -> ''."""
    if is_blank(v):
        return ""
    return re.sub(r"\s+", " ", _DASHES.sub("-", str(v))).strip().lower()


def categorize_api(api_norm: str) -> str:
    if api_norm == C.API_CHANGE:
        return C.CAT_CHANGE
    if api_norm == C.API_POTENTIAL:
        return C.CAT_POTENTIAL
    if api_norm == C.API_EVC_OPEN:
        return C.CAT_EVC_OPEN
    if api_norm == C.API_EVC:
        return C.CAT_EVC
    if _DEV_RE.match(api_norm):
        return C.CAT_DEV
    return C.CAT_OTHER


def canonical_labels(raw: pd.Series, norm: pd.Series) -> pd.Series:
    """One display label per normalised value (most common original spelling)."""
    spelled = raw.map(lambda v: "" if is_blank(v) else re.sub(r"\s+", " ", str(v)).strip())
    frame = pd.DataFrame({"n": norm, "s": spelled})
    frame = frame[frame["s"] != ""]
    best = frame.groupby("n")["s"].agg(lambda x: x.value_counts().idxmax()) if len(frame) else {}
    return norm.map(best).fillna("(blank)").astype(object)


def normalize_book_type(v) -> str:
    """Canonical Book Type from a UI BookCategory value.

    Known values get canonical casing (PDF / HTML / HTML + PDF / Other); any other value is
    kept as written (whitespace collapsed); blank -> 'Unknown'.
    """
    if is_blank(v):
        return C.BT_UNKNOWN
    text = re.sub(r"\s+", " ", str(v)).strip()
    t = text.replace(" ", "").upper()
    if t == "PDF":
        return C.BT_PDF
    if t == "HTML":
        return C.BT_HTML
    if t in ("HTML+PDF", "PDF+HTML"):
        return C.BT_HTML_PDF
    if t == "OTHER":
        return C.BT_OTHER
    return text


def extract_domain(url) -> str:
    """Hostname of a URL (lowercase, no port/userinfo); 'Unknown' if blank/invalid."""
    if is_blank(url):
        return C.UNKNOWN_DOMAIN
    s = str(url).strip()
    if "://" not in s:
        s = "http://" + s.lstrip("/")
    try:
        host = (urlparse(s).hostname or "").lower().rstrip(".")
    except ValueError:
        return C.UNKNOWN_DOMAIN
    if not host or not _HOST_RE.match(host) or ("." not in host and host != "localhost"):
        return C.UNKNOWN_DOMAIN
    return host


def _key_value(v):
    if is_blank(v):
        return None
    if isinstance(v, float) and v.is_integer():   # 123.0 (numeric column) must equal "123"
        return str(int(v))
    return str(v).strip().lower()


def normalize_key(series: pd.Series) -> pd.Series:
    """Identifier key: trimmed, lowercase string; numeric IDs compare equal to text IDs; blanks -> None."""
    return series.map(_key_value).astype(object)


def key_stem(series: pd.Series) -> pd.Series:
    """Identifier without file extension and trailing _vN version."""
    k = normalize_key(series)
    return k.map(lambda v: re.sub(r"_v\d+$", "", re.sub(r"\.(xml|pdf|html?)$", "", v)) if isinstance(v, str) else v)


def last_segment(series: pd.Series) -> pd.Series:
    return series.map(lambda v: v.split("--")[-1] if isinstance(v, str) else v)


def derive_report_date(ui_name: str = "", pbi_name: str = "") -> date:
    """Report date from the UI filename (DD-Mon-YYYY), else the Power BI filename, else today."""
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", ui_name or "")
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}-{m.group(2).title()}-{m.group(3)}", "%d-%b-%Y").date()
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", pbi_name or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return date.today()


def format_report_date(d: date) -> str:
    return f"{d.day:02d}-{C.MONTHS[d.month - 1]}-{d.year}"
