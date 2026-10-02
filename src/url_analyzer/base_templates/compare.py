"""Compare a URL-analysis dataset with the Base Templates table, joined on domain.

Dataset A (uploaded):  Document ID, Domain, Spider Template, URL   (one row per document)
Dataset B (this tool): Cached Domain, Monitoring Template          (the Base Templates table)

Every dataset-A row gets flags saying what has to be fixed; every Base Template gets flags too, so both sides
can be reviewed. This module is pure (no Flask, no files written) so the rules are easy to test.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..analysis.inputs import HEADER_ALIASES, _norm_header
from ..backlog import config as backlog_config
from ..backlog.normalize import extract_domain

MAX_ROWS = 500_000
HEADER_SCAN_ROWS = 25

# flag code -> (label, level, explanation). level "error" = the row needs a fix, "info" = worth a look.
FLAGS = {
    "MISSING_URL": ("Missing URL", "error", "The URL cell is empty."),
    "MISSING_SPIDER_TEMPLATE": ("Missing Spider Template", "error", "The Spider Template cell is empty."),
    "MISSING_DOMAIN": ("Missing Domain", "error", "No domain, and none could be worked out from the URL."),
    "NO_BASE_TEMPLATE": ("No Base Template", "error",
                         "The domain is not in the Cached Domain column of any Base Template (add a base template for it)."),
    "WWW_ONLY_MATCH": ("Matched ignoring www", "info",
                       "The domain only matched a Base Template after ignoring 'www.' - check the Cached Domain spelling."),
}
BASE_FLAGS = {
    "BASE_NO_CACHED_DOMAIN": ("No Cached Domain", "error", "The Base Template has no usable Cached Domain, so it can never be joined."),
    "BASE_NOT_USED": ("No documents in dataset", "info", "No row of the uploaded dataset uses this Base Template's domain."),
}
PLACEHOLDERS = {"nan", "none", "null", "n/a", "na", "-", "--", "#n/a"}

FIELD_ALIASES = {
    "document_id": HEADER_ALIASES["document_id"],
    "domain": HEADER_ALIASES["domain"],
    "spidering_template": HEADER_ALIASES["spidering_template"] + ["spider template", "spidertemplate", "spider templates", "template"],
    "url": HEADER_ALIASES["url"],
}
FIELD_LABELS = {"document_id": "Document ID", "domain": "Domain", "spidering_template": "Spider Template", "url": "URL"}


class CompareError(ValueError):
    """A problem with the uploaded dataset that is safe to show to the user."""


def blank(value) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in PLACEHOLDERS or (isinstance(value, float) and pd.isna(value))


def text(value) -> str:
    return "" if blank(value) else str(value).strip()


# ── reading dataset A ────────────────────────────────────────────────────────
@dataclass
class Dataset:
    filename: str
    sheets: list[str]
    sheet: str
    columns: list[str]
    frame: pd.DataFrame                 # data rows only, columns = `columns`
    row_numbers: list[int]              # spreadsheet row number of every data row
    header_row: int
    mapping: dict = field(default_factory=dict)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, n in enumerate(names, 1):
        n = str(n).strip() or f"Column {i}"
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}.{seen[n] - 1}")
    return out


def _read_csv(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            sample = raw[:65536].decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        enc, sample = "latin-1", raw[:65536].decode("latin-1")
    try:
        sep = csv.Sniffer().sniff(sample.split("\n", 5)[0] or ",", delimiters=",;\t|").delimiter
    except csv.Error:
        sep = ","
    return pd.read_csv(io.BytesIO(raw), sep=sep, header=None, dtype=str, keep_default_na=False, encoding=enc,
                       engine="python" if len(raw) < 20_000_000 else "c", on_bad_lines="skip")


def sheet_names(path: str | Path) -> list[str]:
    if str(path).lower().endswith((".csv", ".txt")):
        return ["(csv)"]
    try:
        with pd.ExcelFile(path) as xl:
            return list(xl.sheet_names)
    except Exception as e:
        raise CompareError(f"The file could not be read as an Excel workbook ({type(e).__name__}).") from e


def load_dataset(path: str | Path, filename: str = "", sheet: str = "") -> Dataset:
    path = Path(path)
    filename = filename or path.name
    names = sheet_names(path)
    if names == ["(csv)"]:
        raw, chosen = _read_csv(path), "(csv)"
    else:
        # prefer a sheet named like the URL Analysis export when none is chosen, else the first non-empty one
        order = [sheet] if sheet else sorted(names, key=lambda n: (_norm_header(n) != "url analysis"))
        raw, chosen = None, ""
        try:
            with pd.ExcelFile(path) as xl:
                for n in order:
                    if n not in names:
                        raise CompareError(f"Sheet '{n}' was not found. Sheets in this file: {', '.join(names)}")
                    frame = xl.parse(n, header=None, dtype=object, keep_default_na=False)
                    if not frame.empty:
                        raw, chosen = frame, n
                        break
        except CompareError:
            raise
        except Exception as e:
            raise CompareError(f"The file could not be read ({type(e).__name__}).") from e
    if raw is None or raw.empty:
        raise CompareError("The file contains no data.")
    raw = raw[raw.map(lambda v: str(v).strip() not in ("", "nan", "None", "NaT")).any(axis=1)]   # fully empty rows (index = sheet row, kept)
    if raw.empty:
        raise CompareError("The file contains no data.")

    # header row = the first of the top rows that names at least two of the four fields
    best, header_at = 0, 0
    for pos in range(min(HEADER_SCAN_ROWS, len(raw))):
        cells = {_norm_header(v) for v in raw.iloc[pos].tolist() if not blank(v)}
        score = sum(any(a in cells for a in al) for al in FIELD_ALIASES.values())
        if score > best:
            best, header_at = score, pos
        if score == len(FIELD_ALIASES):
            break
    columns = _dedupe([str(v) if not blank(v) else "" for v in raw.iloc[header_at].tolist()])
    body = raw.iloc[header_at + 1:].copy()
    body.columns = columns
    if len(body) > MAX_ROWS:
        raise CompareError(f"The dataset has {len(body):,} rows; the limit is {MAX_ROWS:,}.")
    row_numbers = [int(i) + 1 for i in body.index]              # index of the raw frame = 0-based sheet row
    body = body.reset_index(drop=True)
    ds = Dataset(filename, names, chosen, columns, body, row_numbers, int(raw.index[header_at]) + 1)
    ds.mapping = detect_mapping(columns)
    return ds


def detect_mapping(columns: list[str]) -> dict:
    """Best column for each field, by header name. Missing fields are None."""
    out, taken = {}, set()
    for fld in ("url", "spidering_template", "domain", "document_id"):
        out[fld] = next((c for c in columns if c not in taken and _norm_header(c) in FIELD_ALIASES[fld]), None)
        if out[fld]:
            taken.add(out[fld])
    return out


def check_mapping(columns: list[str], mapping: dict) -> dict:
    clean = {}
    for fld in FIELD_ALIASES:
        col = mapping.get(fld) or None
        if col is not None and col not in columns:
            raise CompareError(f"The column '{col}' (for {FIELD_LABELS[fld]}) does not exist in the file.")
        clean[fld] = col
    if not clean["url"]:
        raise CompareError("Choose the column that holds the URL.")
    if not clean["spidering_template"]:
        raise CompareError("Choose the column that holds the Spider Template.")
    chosen = [c for c in clean.values() if c]
    if len(chosen) != len(set(chosen)):
        raise CompareError("The same column was chosen for two different fields.")
    return clean


# ── the comparison ───────────────────────────────────────────────────────────
def _nowww(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _hosts(cached_domain: str) -> list[str]:
    """Hostnames in a Cached Domain cell (one cell may hold several, separated by commas)."""
    out = []
    for part in re.split(r"[,;\n]+", cached_domain or ""):
        host = extract_domain(part.strip()) if part.strip() else backlog_config.UNKNOWN_DOMAIN
        if host != backlog_config.UNKNOWN_DOMAIN and host not in out:
            out.append(host)
    return out


@dataclass
class Comparison:
    rows: list[dict]
    domains: list[dict]
    base: list[dict]
    summary: dict


def compare(ds_or_frame, mapping: dict, base_rows: list[dict], row_numbers: list[int] | None = None, source: dict | None = None) -> Comparison:
    if isinstance(ds_or_frame, Dataset):
        frame, row_numbers = ds_or_frame.frame, ds_or_frame.row_numbers
        source = source or {"filename": ds_or_frame.filename, "sheet": ds_or_frame.sheet}
    else:
        frame = ds_or_frame
    row_numbers = row_numbers or list(range(2, len(frame) + 2))
    mapping = check_mapping(list(frame.columns), mapping)

    exact: dict[str, list[dict]] = defaultdict(list)
    loose: dict[str, list[dict]] = defaultdict(list)
    base = []
    for b in base_rows:
        hosts = _hosts(b.get("domain", ""))
        item = {"id": b.get("id", ""), "cached_domain": b.get("domain", ""), "template": b.get("template", ""),
                "created_date": b.get("created_date", ""), "dev_name": b.get("dev_name", ""), "hosts": hosts,
                "flags": [], "documents": 0}
        if not hosts:
            item["flags"].append("BASE_NO_CACHED_DOMAIN")
        for h in hosts:
            exact[h].append(item)
            loose[_nowww(h)].append(item)
        base.append(item)

    def names(items: list[dict]) -> list[str]:
        seen: list[str] = []
        for it in items:
            if it["template"] and it["template"] not in seen:
                seen.append(it["template"])
        return seen

    rows, doms = [], {}
    cols = {k: frame[v].tolist() if v else [""] * len(frame) for k, v in mapping.items()}
    for i in range(len(frame)):
        url, tpl = text(cols["url"][i]), text(cols["spidering_template"][i])
        raw_domain = text(cols["domain"][i])
        host = extract_domain(raw_domain) if raw_domain else backlog_config.UNKNOWN_DOMAIN
        from_url = False
        if host == backlog_config.UNKNOWN_DOMAIN and url:
            host, from_url = extract_domain(url), True
        flags = []
        if not url:
            flags.append("MISSING_URL")
        if not tpl:
            flags.append("MISSING_SPIDER_TEMPLATE")
        match_type, matched = "", []
        if host == backlog_config.UNKNOWN_DOMAIN:
            host = ""
            flags.append("MISSING_DOMAIN")
        elif host in exact:
            match_type, matched = "Exact", exact[host]
        elif _nowww(host) in loose:
            match_type, matched = "Ignoring www", loose[_nowww(host)]
            flags.append("WWW_ONLY_MATCH")
        if host and not matched:
            flags.append("NO_BASE_TEMPLATE")
        for it in {id(m): m for m in matched}.values():
            it["documents"] += 1
        status = "Needs fix" if any(FLAGS[f][1] == "error" for f in flags) else ("Check" if flags else "OK")
        rows.append({"source_row": row_numbers[i], "document_id": text(cols["document_id"][i]), "domain": host,
                     "domain_from_url": from_url, "spidering_template": tpl, "url": url, "flags": flags, "status": status,
                     "match_type": match_type, "base_templates": names(matched), "base_count": len(names(matched))})
        d = doms.setdefault(host, {"domain": host or "(none)", "rows": 0, "needs_fix": 0, "missing_url": 0, "missing_template": 0,
                                   "base_templates": names(matched), "base_count": len(names(matched)), "match_type": match_type})
        d["rows"] += 1
        d["needs_fix"] += status == "Needs fix"
        d["missing_url"] += "MISSING_URL" in flags
        d["missing_template"] += "MISSING_SPIDER_TEMPLATE" in flags

    for d in doms.values():
        d["status"] = ("No domain" if d["domain"] == "(none)" else "No Base Template" if not d["base_count"]
                       else "Needs fix" if d["needs_fix"] else "OK")
    for b in base:
        if b["hosts"] and not b["documents"]:
            b["flags"].append("BASE_NOT_USED")
    domains = sorted(doms.values(), key=lambda d: (-d["needs_fix"], -d["rows"], d["domain"]))

    flag_counts = Counter(f for r in rows for f in r["flags"])
    base_flag_counts = Counter(f for b in base for f in b["flags"])
    summary = {
        "source": source or {}, "dataset_rows": len(rows), "base_rows": len(base),
        "rows_ok": sum(r["status"] == "OK" for r in rows), "rows_check": sum(r["status"] == "Check" for r in rows),
        "rows_needs_fix": sum(r["status"] == "Needs fix" for r in rows),
        "flags": {code: flag_counts.get(code, 0) for code in FLAGS},
        "base_flags": {code: base_flag_counts.get(code, 0) for code in BASE_FLAGS},
        "domains": len(domains), "domains_without_base": sum(d["status"] == "No Base Template" for d in domains),
        "domains_derived_from_url": sum(r["domain_from_url"] for r in rows),
        "mapping": mapping,
    }
    return Comparison(rows, domains, base, summary)


def filter_rows(rows: list[dict], flag: str = "", status: str = "", q: str = "", domain: str = "") -> list[dict]:
    out = rows
    if flag:
        out = [r for r in out if flag in r["flags"]]
    if status:
        out = [r for r in out if r["status"] == status]
    if domain:
        out = [r for r in out if r["domain"] == domain]
    tokens = q.lower().split()
    if tokens:
        out = [r for r in out if all(t in " ".join([r["document_id"], r["domain"], r["spidering_template"], r["url"],
                                                    " ".join(r["base_templates"])]).lower() for t in tokens)]
    return out
