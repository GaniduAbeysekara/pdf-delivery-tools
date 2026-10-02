"""Excel import/export for the Base Templates table."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .store import FIELDS, LABELS

ALIASES = {
    "created_date": ["created date", "createddate", "date created", "date"],
    "template": ["monitoring templates", "monitoring templates spideringtemplate", "monitoring template", "spidering template", "spideringtemplate", "template", "templates"],
    "domain": ["cached domain", "cached domains", "domain", "domains"],
    "added_to_replit": ["added to replit", "added to replit?", "replit"],
    "dev_name": ["dev name", "developer", "dev", "devname", "developer name"],
    "comments": ["comments", "comment", "notes", "note"],
}
HEADER_SCAN_ROWS = 25
_NORM = re.compile(r"[^a-z0-9?]+")


class ImportError_(ValueError):
    """Workbook problem that is safe to show to the user."""


def _norm(value) -> str:
    return _NORM.sub(" ", str(value).lower()).strip() if value is not None and not (isinstance(value, float) and pd.isna(value)) else ""


def pick_sheet(names: list[str], wanted: str = "") -> str:
    if wanted:
        for n in names:
            if n == wanted:
                return n
        raise ImportError_(f"Sheet '{wanted}' was not found. Sheets in this file: {', '.join(names)}")
    for n in names:
        if _norm(n) == "base template":
            return n
    close = [n for n in names if "base" in _norm(n) and "template" in _norm(n)]
    if close:
        return close[0]
    if len(names) == 1:
        return names[0]
    raise ImportError_(f"Could not tell which sheet holds the Base Templates. Sheets in this file: {', '.join(names)}. "
                       "Rename the right one to 'Base Template' or choose it from the list.")


def read_rows(source, sheet: str = "") -> dict:
    """Read the Base Template sheet from an xlsx path/file object.

    Returns {sheet, sheets, rows: [dict], skipped_blank, dev_members}. The header row is located automatically
    (sheets often have a title or empty rows above it). Fully empty rows are ignored.
    """
    try:
        with pd.ExcelFile(source) as xl:
            names = list(xl.sheet_names)
            chosen = pick_sheet(names, sheet)
            raw = xl.parse(chosen, header=None, dtype=object, keep_default_na=False)   # keep text such as 'N/A' as written
    except ImportError_:
        raise
    except Exception as e:                                           # corrupt / not an Excel file
        raise ImportError_(f"The file could not be read as an Excel workbook ({type(e).__name__}).") from e

    header_at, colmap = None, {}
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        cells = [_norm(v) for v in raw.iloc[i].tolist()]
        found = {}
        for field, names_ in ALIASES.items():
            for c, text in enumerate(cells):
                if text in names_ and c not in found.values():
                    found[field] = c
                    break
        if "template" in found:
            header_at, colmap = i, found
            break
    if header_at is None:
        raise ImportError_(f"Sheet '{chosen}' has no 'Monitoring Templates' column in its first {HEADER_SCAN_ROWS} rows.")

    rows, blank = [], 0
    for i in range(header_at + 1, len(raw)):
        values = raw.iloc[i].tolist()
        row = {f: ("" if (c >= len(values) or values[c] is None or (isinstance(values[c], float) and pd.isna(values[c])))
                   else values[c]) for f, c in colmap.items()}
        for f in FIELDS:
            row.setdefault(f, "")
        if not any(str(v).strip() for v in row.values()):
            blank += 1
            continue
        for f in ("template", "domain", "added_to_replit", "dev_name", "comments"):
            if isinstance(row[f], float) and row[f].is_integer():
                row[f] = int(row[f])
            if isinstance(row[f], str) and row[f][:2] in ("'=", "'+", "'-", "'@"):
                row[f] = row[f][1:]                                  # undo the export's formula-injection guard
        rows.append(row)
    return {"sheet": chosen, "sheets": names, "rows": rows, "skipped_blank": blank, "columns_found": sorted(colmap)}


def build_workbook(rows: list[dict]) -> bytes:
    """An .xlsx with the same columns as the team's sheet, as an Excel Table (filter buttons)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Base Template"
    ws.append([LABELS[f] for f in FIELDS])
    for r in rows:
        ws.append([_safe(r.get(f, "")) for f in FIELDS])
    head_fill = PatternFill("solid", fgColor="1F3864")
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
    widths = {"created_date": 14, "template": 46, "domain": 40, "added_to_replit": 16, "dev_name": 18, "comments": 60}
    for i, f in enumerate(FIELDS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths[f]
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name="Arial")
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    last = max(2, len(rows) + 1)
    table = Table(displayName="BaseTemplates", ref=f"A1:{get_column_letter(len(FIELDS))}{last}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    ws.add_table(table)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _safe(value):
    """Stop spreadsheet formula injection: text starting with = + - @ is stored as plain text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value
