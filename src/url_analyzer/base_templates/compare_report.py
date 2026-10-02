"""Excel report for a Base Template comparison: one row per document with clear flag columns."""
from __future__ import annotations

import io
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .compare import BASE_FLAGS, FLAGS, Comparison
from .excel import _safe

RED, AMBER, GREEN = (PatternFill("solid", fgColor=c) for c in ("F8CBAD", "FFE699", "C6E0B4"))
HEAD = PatternFill("solid", fgColor="1F3864")
STATUS_FILL = {"Needs fix": RED, "Check": AMBER, "OK": GREEN, "No Base Template": RED, "No domain": RED}
FLAG_COLUMNS = ["MISSING_URL", "MISSING_SPIDER_TEMPLATE", "MISSING_DOMAIN", "NO_BASE_TEMPLATE"]   # shown as YES/blank columns


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], widths: list[int], name: str,
           fills: dict[int, dict] | None = None) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for r in rows:
        ws.append([_safe(v) for v in r])
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name="Arial")
            c.alignment = Alignment(vertical="top", wrap_text=False)
            fill = (fills or {}).get(c.column - 1, {}).get(c.value)
            if fill:
                c.fill = fill
    ws.freeze_panes = "A2"
    t = Table(displayName=name, ref=f"A1:{get_column_letter(len(headers))}{max(2, len(rows) + 1)}")
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
    ws.add_table(t)


def _row_cells(r: dict) -> list:
    flag_cols = ["YES" if code in r["flags"] else "" for code in FLAG_COLUMNS]
    return [r["source_row"], r["document_id"], r["domain"], r["spidering_template"], r["url"], r["status"],
            "; ".join(FLAGS[f][0] for f in r["flags"]), *flag_cols, r["match_type"],
            "; ".join(r["base_templates"]), r["base_count"]]


def _row_headers() -> list[str]:
    return ["Source Row", "Document ID", "Domain", "Spider Template", "URL", "Status", "Flags",
            *[FLAGS[c][0] for c in FLAG_COLUMNS], "Domain Match", "Base Template(s)", "Base Template Count"]


ROW_WIDTHS = [11, 40, 30, 38, 60, 12, 46, 13, 22, 14, 18, 14, 50, 12]


def build_report(cmp: Comparison, rows: list[dict] | None = None) -> bytes:
    """`rows` limits the Review sheet to a subset (e.g. the current filter); the other sheets always describe the whole run."""
    shown = cmp.rows if rows is None else rows
    s = cmp.summary
    wb = Workbook()
    wb.remove(wb.active)

    def fills_for(headers: list[str]) -> dict:
        d = {headers.index("Status"): STATUS_FILL}
        for code in FLAG_COLUMNS:
            d[headers.index(FLAGS[code][0])] = {"YES": RED}
        return d

    h = _row_headers()
    _sheet(wb, "Review", h, [_row_cells(r) for r in shown], ROW_WIDTHS, "Review", fills_for(h))
    fix = [r for r in shown if r["status"] == "Needs fix"]
    _sheet(wb, "Needs Fix", h, [_row_cells(r) for r in fix], ROW_WIDTHS, "NeedsFix", fills_for(h))

    dh = ["Domain", "Documents", "Needing fix", "Missing URL", "Missing Spider Template", "Base Template Count", "Base Template(s)", "Status"]
    _sheet(wb, "Domains", dh, [[d["domain"], d["rows"], d["needs_fix"], d["missing_url"], d["missing_template"], d["base_count"],
                                "; ".join(d["base_templates"]), d["status"]] for d in cmp.domains],
           [34, 11, 12, 12, 22, 18, 60, 18], "Domains", {7: STATUS_FILL})

    bh = ["Cached Domain", "Monitoring Template", "Created Date", "Dev Name", "Documents in Dataset", "Flags", "Status"]
    brows = [[b["cached_domain"], b["template"], b["created_date"], b["dev_name"], b["documents"],
              "; ".join(BASE_FLAGS[f][0] for f in b["flags"]),
              "Needs fix" if "BASE_NO_CACHED_DOMAIN" in b["flags"] else ("Check" if b["flags"] else "OK")] for b in cmp.base]
    _sheet(wb, "Base Templates Review", bh, brows, [40, 52, 14, 18, 20, 36, 12], "BaseReview", {6: STATUS_FILL})

    src = s.get("source", {})
    lines = [["Base Template Analysis", ""], ["Generated", time.strftime("%Y-%m-%d %H:%M:%S")],
             ["Dataset file", src.get("filename", "")], ["Sheet", src.get("sheet", "")],
             ["Columns used", ", ".join(f"{k}: {v}" for k, v in s["mapping"].items() if v)],
             ["Rows in this Review sheet", len(shown)], ["", ""],
             ["Documents in dataset", s["dataset_rows"]], ["OK", s["rows_ok"]], ["Check (info flags only)", s["rows_check"]],
             ["Needs fix", s["rows_needs_fix"]], ["", ""], ["FLAGS ON DOCUMENTS", "Rows"]]
    lines += [[f"{FLAGS[c][0]}  ({FLAGS[c][1]})", n] for c, n in s["flags"].items()]
    lines += [["", ""], ["Domains in dataset", s["domains"]], ["Domains with no Base Template", s["domains_without_base"]],
              ["Domains taken from the URL", s["domains_derived_from_url"]], ["", ""],
              ["Base Templates compared", s["base_rows"]]]
    lines += [[f"{BASE_FLAGS[c][0]}  ({BASE_FLAGS[c][1]})", n] for c, n in s["base_flags"].items()]
    lines += [["", ""], ["HOW TO READ THIS", ""]]
    lines += [[f"{FLAGS[c][0]}", FLAGS[c][2]] for c in FLAGS]
    lines += [[f"{BASE_FLAGS[c][0]}", BASE_FLAGS[c][2]] for c in BASE_FLAGS]
    lines += [["Join rule", "Documents are joined to Base Templates on the domain (lowercase hostname, no scheme/path/port)."],
              ["Source Row", "The row number in the uploaded file, so each row can be found and fixed there."]]
    ws = wb.create_sheet("SUMMARY", 0)
    for line in lines:
        ws.append([_safe(v) for v in line])
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 100
    for row in ws.iter_rows():
        for c in row:
            c.font = Font(name="Arial", bold=c.column == 1 and (c.value or "").isupper() if isinstance(c.value, str) else False)
            c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws["A1"].font = Font(name="Arial", bold=True, size=14)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
