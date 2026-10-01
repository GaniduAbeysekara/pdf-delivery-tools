"""Input adapters: every source of URLs is turned into the same list of BookRecords for the engine.

* parse_urls / parse_book_data  - pasted text (one URL per line, or rows copied from Excel)
* read_excel_records            - an uploaded Excel workbook (URL column detected or chosen)
(The Daily Backlog hand-off builds BookRecords itself; see backlog/web.py.)
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import pandas as pd

from ..backlog.normalize import extract_domain
from .models import BookRecord

MAX_ITEMS = 5000
_URL_RE = re.compile(r"https?://[^\s<>\"'“”]+", re.I)
_BARE_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:/\S*)?$", re.I)
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{3,63}$")
_TRAIL = ".,;:)]}>”'\""

HEADER_ALIASES = {
    "document_id": ["document id", "documentid", "doc id", "docid", "book id", "bookid", "booksourceid",
                    "book source id", "source id", "id", "code", "cubebookid"],
    "book_title": ["book title", "booktitle", "title", "name", "book name", "document title"],
    "url": ["url", "source url", "source link", "document url", "link", "link to the issuance", "source_link",
            "href", "web address", "document link"],
    "domain": ["domain", "source domain"],
    "api_result": ["api result", "apiresult", "result"],
    "book_type": ["book type", "booktype", "bookcategory", "book category", "category"],
}
URL_COLUMN_PRIORITY = ["url", "source url", "source link", "document url", "link", "link to the issuance",
                       "document link", "web address", "href", "source_link"]


class InputError(ValueError):
    """A problem with the user's input that is safe to show as-is."""


@dataclass
class ParseOutcome:
    items: list[BookRecord] = field(default_factory=list)
    duplicates_removed: int = 0
    rows_without_url: int = 0
    invalid_lines: int = 0
    notes: list[str] = field(default_factory=list)
    source_columns: list[str] = field(default_factory=list)


def clean_url(raw: str) -> str:
    return raw.strip().rstrip(_TRAIL) if raw else ""


def url_key(url: str) -> str:
    """De-duplication / cache key: scheme and host case-insensitive, fragment ignored."""
    try:
        p = urlparse(url.strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params, p.query, ""))
    except ValueError:
        return url.strip()


def _urls_in(text: str) -> list[str]:
    return [clean_url(m) for m in _URL_RE.findall(text or "") if clean_url(m)]


def dedupe(items: list[BookRecord], out: ParseOutcome) -> ParseOutcome:
    seen: set[str] = set()
    for it in items:
        k = url_key(it.url)
        if k in seen:
            out.duplicates_removed += 1
            continue
        seen.add(k)
        if not it.domain:
            it.domain = extract_domain(it.url)
        out.items.append(it)
    if len(out.items) > MAX_ITEMS:
        out.notes.append(f"Only the first {MAX_ITEMS} URLs will be analysed (you provided {len(out.items)}).")
        out.items = out.items[:MAX_ITEMS]
    return out


# ── pasted text ──────────────────────────────────────────────────────────────
def parse_urls(text: str) -> ParseOutcome:
    """One URL per line (also tolerates several per line, tabs, Excel copies). Invalid lines are kept
    so they surface as Failed rows instead of silently disappearing."""
    out, items = ParseOutcome(), []
    for line in (text or "").splitlines():
        line = line.strip().strip("﻿")
        if not line:
            continue
        found = _urls_in(line)
        if found:
            items += [BookRecord(url=u) for u in found]
            continue
        for cell in [c.strip().strip('"') for c in re.split(r"[\t,;]", line) if c.strip()] or [line]:
            if _BARE_RE.match(cell):
                items.append(BookRecord(url="https://" + cell))
            else:
                out.invalid_lines += 1
                items.append(BookRecord(url=cell))
    return dedupe(items, out)


def _norm_header(cell: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(cell).strip().lower())


def _map_header(row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, cell in enumerate(row):
        h = _norm_header(cell)
        for field_name, aliases in HEADER_ALIASES.items():
            if field_name not in mapping and h in aliases:
                mapping[field_name] = i
    return mapping


def _detect_delimiter(lines: list[str]) -> str | None:
    if any("\t" in ln for ln in lines):
        return "\t"
    sample = "\n".join(lines[:10])
    try:
        d = csv.Sniffer().sniff(sample, delimiters=",;|").delimiter
    except csv.Error:
        return None
    counts = [ln.count(d) for ln in lines[:10]]
    return d if counts and min(counts) > 0 else None


def _looks_like_id(cell: str) -> bool:
    c = cell.strip()
    return bool(_GUID_RE.match(c) or (_CODE_RE.match(c) and any(ch.isdigit() for ch in c)))


def looks_tabular(text: str) -> bool:
    return any("\t" in ln for ln in (text or "").splitlines())


def parse_book_data(text: str) -> ParseOutcome:
    """Pasted rows (tab/CSV from Excel, with or without header) or free text containing URLs."""
    out = ParseOutcome()
    lines = [ln for ln in (text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return out
    delim = _detect_delimiter(lines)
    items: list[BookRecord] = []

    if delim:
        rows = [[c.strip() for c in r] for r in csv.reader(lines, delimiter=delim)]
        mapping = _map_header(rows[0]) if not _urls_in(" ".join(rows[0])) else {}
        if len(mapping) >= 2 and "url" in mapping:
            names = {v: k for k, v in mapping.items()}
            out.notes.append("Detected columns: " + ", ".join(
                f"{rows[0][i]} → {k.replace('_', ' ')}" for k, i in mapping.items()))
            extras_seen: list[str] = []
            for r in rows[1:]:
                cell = lambda k: r[mapping[k]] if k in mapping and mapping[k] < len(r) else ""  # noqa: E731
                urls = _urls_in(cell("url")) or ([clean_url(cell("url"))] if _BARE_RE.match(cell("url")) else [])
                if not urls:
                    out.rows_without_url += 1
                    continue
                extra = {rows[0][i]: v for i, v in enumerate(r) if i not in names and v and i < len(rows[0])}
                extras_seen += [k for k in extra if k not in extras_seen]
                items.append(BookRecord(
                    url=urls[0] if urls[0].lower().startswith("http") else "https://" + urls[0],
                    document_id=cell("document_id"), book_title=cell("book_title"), domain=cell("domain"),
                    api_result=cell("api_result"), book_type=cell("book_type"), extra=extra))
            out.source_columns = extras_seen
        else:
            out.notes.append("No header row recognised; document ID, title and URL were detected from cell contents.")
            for r in rows:
                urls = _urls_in(" ".join(r))
                if not urls:
                    out.rows_without_url += 1
                    continue
                rest = [c for c in r if c and not _URL_RE.search(c)]
                doc_id = next((c for c in rest if _looks_like_id(c)), "")
                titles = [c for c in rest if c != doc_id and " " in c.strip()]
                items.append(BookRecord(url=urls[0], document_id=doc_id,
                                        book_title=max(titles, key=len) if titles else ""))
    else:
        out.notes.append("Free text: URLs were extracted from each line; remaining text was used as the title.")
        for ln in lines:
            urls = _urls_in(ln)
            if not urls:
                out.rows_without_url += 1
                continue
            remainder = _URL_RE.sub(" ", ln)
            doc_id = next((t.strip(",;|") for t in remainder.split() if _looks_like_id(t.strip(",;|"))), "")
            title = re.sub(r"\s+", " ", remainder.replace(doc_id, " ") if doc_id else remainder).strip(" -|,;:\t")
            items.append(BookRecord(url=urls[0], document_id=doc_id, book_title=title))

    if out.rows_without_url:
        out.notes.append(f"{out.rows_without_url} row(s) had no URL and were skipped.")
    return dedupe(items, out)


def parse_pasted(text: str, mode: str = "auto") -> ParseOutcome:
    """Pasted text -> records. `auto` treats tab-separated text (copied from Excel) as book data."""
    if mode == "book" or (mode == "auto" and looks_tabular(text)):
        return parse_book_data(text)
    return parse_urls(text)


# ── Excel workbook ───────────────────────────────────────────────────────────
@dataclass
class ExcelInspection:
    sheet: str
    columns: list[str]
    row_count: int
    url_column: str | None
    confident: bool
    candidates: list[str]
    id_column: str | None
    title_column: str | None
    preview: list[dict]


@dataclass
class ExcelRecords:
    records: list[BookRecord]
    source_columns: list[str]        # original columns preserved in `record.extra`, in file order
    url_column: str
    id_column: str | None
    title_column: str | None
    sheet: str
    skipped_blank: int = 0
    notes: list[str] = field(default_factory=list)


def _cell_str(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def load_excel_frame(path: str) -> tuple[pd.DataFrame, str]:
    try:
        with pd.ExcelFile(path) as xls:          # closes the file handle (an open handle blocks deleting it on Windows)
            for sheet in xls.sheet_names:
                df = xls.parse(sheet, dtype=object).dropna(how="all").dropna(axis=1, how="all")
                if not df.empty:
                    seen: dict[str, int] = {}
                    cols = []
                    for c in df.columns:
                        name = str(c).strip() or "Column"
                        seen[name] = seen.get(name, 0) + 1
                        cols.append(name if seen[name] == 1 else f"{name}.{seen[name] - 1}")
                    df.columns = cols
                    return df.reset_index(drop=True), str(sheet)
    except InputError:
        raise
    except Exception as e:
        raise InputError(f"The Excel file could not be read: {e}") from e
    raise InputError("The Excel file contains no data rows.")


def _url_share(series: pd.Series) -> float:
    vals = [_cell_str(v) for v in series.head(200)]
    vals = [v for v in vals if v]
    if not vals:
        return 0.0
    return sum(bool(_URL_RE.match(v) or _BARE_RE.match(v)) for v in vals) / len(vals)


def detect_url_column(df: pd.DataFrame) -> tuple[str | None, bool, list[str]]:
    """(column, confident, candidates). Header names are checked first, then the cell contents."""
    cols = list(df.columns)
    norm = {c: _norm_header(c) for c in cols}
    alias_hits = [c for pri in URL_COLUMN_PRIORITY for c in cols if norm[c] == pri]
    share = {c: _url_share(df[c]) for c in cols}
    value_hits = [c for c in cols if share[c] >= 0.6 and c not in alias_hits]
    candidates = alias_hits + value_hits
    if len(alias_hits) == 1 and share[alias_hits[0]] >= 0.3:
        return alias_hits[0], True, candidates
    if len(alias_hits) > 1:
        good = [c for c in alias_hits if share[c] >= 0.6]
        return ((good[0], True, candidates) if len(good) == 1 else ((good or alias_hits)[0], False, candidates))
    if len(value_hits) == 1:
        return value_hits[0], True, candidates
    return (candidates[0] if candidates else None), False, candidates


def _pick(df: pd.DataFrame, field_name: str, exclude: set) -> str | None:
    for c in df.columns:
        if c not in exclude and _norm_header(c) in HEADER_ALIASES[field_name]:
            return c
    return None


def inspect_excel(path: str) -> ExcelInspection:
    df, sheet = load_excel_frame(path)
    url_col, confident, candidates = detect_url_column(df)
    exclude = {url_col} if url_col else set()
    id_col = _pick(df, "document_id", exclude)
    title_col = _pick(df, "book_title", exclude | ({id_col} if id_col else set()))
    preview = [{c: _cell_str(r[c]) for c in df.columns} for _, r in df.head(5).iterrows()]
    return ExcelInspection(sheet=sheet, columns=list(df.columns), row_count=len(df), url_column=url_col,
                           confident=confident, candidates=candidates, id_column=id_col, title_column=title_col,
                           preview=preview)


def _as_url(value: str) -> str:
    v = value.strip()
    found = _urls_in(v)
    if found:
        return found[0]
    return "https://" + v if _BARE_RE.match(v) else v


def read_excel_records(path: str, url_column: str | None = None, id_column: str | None = None,
                       title_column: str | None = None, *, sheet_df: tuple[pd.DataFrame, str] | None = None) -> ExcelRecords:
    """Excel rows -> BookRecords. Every source column is kept in `record.extra` (nothing is thrown away)
    and every row is kept - identical URLs on different rows are each reported (but fetched once)."""
    df, sheet = sheet_df or load_excel_frame(path)
    notes: list[str] = []
    if not url_column:
        url_column, confident, _ = detect_url_column(df)
        if not url_column:
            raise InputError("No URL column could be found. Choose the column that contains the URLs.")
        if not confident:
            notes.append(f"The URL column was not certain; '{url_column}' was used.")
    if url_column not in df.columns:
        raise InputError(f"The column '{url_column}' does not exist in the workbook, or it has no values.")
    if id_column and id_column not in df.columns:
        raise InputError(f"The column '{id_column}' does not exist in the workbook.")
    if title_column and title_column not in df.columns:
        raise InputError(f"The column '{title_column}' does not exist in the workbook.")
    mapped = {c for c in (url_column, id_column, title_column) if c}
    source_columns = [c for c in df.columns if c not in mapped]

    records, skipped = [], 0
    for _, row in df.iterrows():
        cells = {c: _cell_str(row[c]) for c in df.columns}
        raw = cells[url_column]
        if not raw:
            skipped += 1
            continue
        url = _as_url(raw)
        records.append(BookRecord(
            url=url, document_id=cells.get(id_column, "") if id_column else "",
            book_title=cells.get(title_column, "") if title_column else "",
            domain=extract_domain(url), extra={c: cells[c] for c in source_columns}))
    if skipped:
        notes.append(f"{skipped} row(s) had no URL and were skipped.")
    if len(records) > MAX_ITEMS:
        notes.append(f"Only the first {MAX_ITEMS} rows will be analysed (the workbook has {len(records)}).")
        records = records[:MAX_ITEMS]
    if not records:
        raise InputError(f"The column '{url_column}' has no URLs.")
    shared = len(records) - len({url_key(r.url) for r in records})
    if shared:
        notes.append(f"{shared} row(s) repeat a URL from another row; each URL is fetched once and every row keeps its result.")
    return ExcelRecords(records=records, source_columns=source_columns, url_column=url_column,
                        id_column=id_column, title_column=title_column, sheet=sheet, skipped_blank=skipped, notes=notes)
