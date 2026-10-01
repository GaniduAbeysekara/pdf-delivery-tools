"""CLI for the PDF Daily Backlog report: `run.bat daily-workflow --ui X.csv --powerbi Y.xlsx`."""
from __future__ import annotations

import argparse
import sys

from .backlog import BacklogError, build_dataset, default_output_path, write_report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="url_analyzer daily-workflow",
                                description="Build the PDF Team daily backlog workbook.")
    p.add_argument("--ui", required=True, help="Reg Transform UI export (StartPointStatus-*.csv)")
    p.add_argument("--powerbi", required=True, help="Power BI Details Table export (.xlsx)")
    p.add_argument("--output", default="", help="Output .xlsx (default: output/<powerbi name>_Report.xlsx)")
    a = p.parse_args(argv)
    try:
        data = build_dataset(a.ui, a.powerbi, progress=print)
        out = write_report(data, a.output or default_output_path(data))
    except BacklogError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    s = data.stats
    print(f"\nSaved: {out}")
    print(f"Loaded {s['powerbi_rows']:,} Power BI records and {s['ui_rows']:,} Reg Transform records")
    print(f"Matched {s['matched']:,} / unmatched {s['unmatched']:,} "
          f"(UI '{s['match_ui_key']}' <-> Power BI '{s['match_pbi_key']}')")
    print(f"PBI records: {s['powerbi_rows']:,} | UI records: {s['ui_rows']:,} | BookCategory matched: {s['matched']:,} | "
          f"Unmatched: {s['unmatched']:,} | Duplicate UI identifiers: {s['duplicate_ui_keys']:,} | "
          f"blank BookCategory (Unknown): {s['blank_category']:,}")
    print(f"PDF/Other: {s['pdf_other']:,} | HTML & HTML + PDF: {s['html_or_html_pdf']:,}")
    for name, n in s["action_counts"].items():
        print(f"  {name}: {n:,} records")
    for w in data.warnings:
        print(f"  Warning: {w}")


if __name__ == "__main__":
    main()
