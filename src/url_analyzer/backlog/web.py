"""Flask blueprint for the PDF Daily Backlog dashboard."""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from flask import Blueprint, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from .. import transfer
from ..logger import log
from . import config as C
from . import filters as F
from .dataset import BacklogData, build_dataset
from .excel_report import default_output_path, write_report
from .loader import BacklogError
from ..analysis.inputs import url_key
from .normalize import format_report_date, is_blank, is_text

MAX_REPORTS = 5
MAX_SELECT = 5000


@dataclass
class _Report:
    data: BacklogData
    excel_path: Path | None = None
    excel_state: str = "pending"   # pending | done | error
    excel_error: str = ""


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _spec(args) -> F.FilterSpec:
    extra = {k[2:]: args.getlist(k) for k in args.keys() if k.startswith("f_")}
    return F.FilterSpec(
        domains=args.getlist("domain"), apis=args.getlist("api"),
        book_types=args.getlist("book_type"), extra=extra, q=(args.get("q") or "").strip(),
        date_from=args.get("date_from", ""), date_to=args.get("date_to", ""),
    )


def _meta(data: BacklogData, rid: str) -> dict:
    df = data.export
    cols = data.public_columns
    dates = df["_date"].dropna() if "_date" in df.columns else pd.Series(dtype="datetime64[ns]")
    return {
        "id": rid,
        "report_date": format_report_date(data.report_date),
        "ui_name": data.ui_name, "pbi_name": data.pbi_name,
        "stats": data.stats, "warnings": data.warnings,
        "total": len(df),
        "options": {
            "domains": sorted(df[C.DOMAIN_COL].unique(), key=str.lower),
            "apis": sorted(df["_api_label"].unique(), key=str.lower),
            "book_types": F.ordered_book_types(df[C.BOOK_TYPE_COL].unique()),
            "extra": {label: sorted(df[col].unique(), key=str.lower) for label, col in data.filter_cols.items()},
            "date_min": dates.min().strftime("%Y-%m-%d") if len(dates) else "",
            "date_max": dates.max().strftime("%Y-%m-%d") if len(dates) else "",
            "date_label": data.date_col or "",
        },
        "columns": {"all": cols, "default": F.default_columns(cols), "api": data.api_col},
    }


def _csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Neutralise spreadsheet formula injection in exported text cells."""
    def fix(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t", "\r"):
            return "'" + v
        return v
    df = df.copy()
    for c in df.columns:
        if is_text(df[c]):
            df[c] = df[c].map(fix)
    return df


def create_blueprint(upload_dir: str | Path, output_dir: str | Path) -> Blueprint:
    bp = Blueprint("backlog", __name__)
    upload_dir, output_dir = Path(upload_dir), Path(output_dir)
    jobs: dict[str, dict] = {}
    reports: OrderedDict[str, _Report] = OrderedDict()
    lock = threading.Lock()

    def _get(rid: str) -> _Report | None:
        with lock:
            return reports.get(rid)

    def _run(job_id: str, ui_path: Path, pbi_path: Path) -> None:
        job = jobs[job_id]
        step = lambda m: job["steps"].append(m)  # noqa: E731
        try:
            data = build_dataset(str(ui_path), str(pbi_path), progress=step)
            rep = _Report(data=data)
            with lock:
                reports[job_id] = rep
                while len(reports) > MAX_REPORTS:
                    reports.popitem(last=False)
            step("Preparing dashboard...")
            job["report"] = _meta(data, job_id)
            job["state"] = "ready"
            step("Generating Excel report...")
            try:
                rep.excel_path = write_report(data, default_output_path(data, str(output_dir)).with_name(
                    f"{job_id}_{data.output_stem}_Report.xlsx"))
                rep.excel_state = "done"
                step("Report generated successfully")
            except BacklogError as e:
                rep.excel_state, rep.excel_error = "error", str(e)
            except Exception:
                log.exception("Excel generation failed")
                rep.excel_state, rep.excel_error = "error", "The Excel report could not be generated."
        except BacklogError as e:
            job["state"], job["error"] = "error", str(e)
            log.error("Backlog report failed: %s", e)
        except Exception:
            log.exception("Backlog report failed unexpectedly")
            job["state"], job["error"] = "error", "Something went wrong while processing the files. Please check them and try again."

    @bp.route("/backlog")
    def page():
        return render_template("backlog.html")

    @bp.route("/api/backlog/generate", methods=["POST"])
    def generate():
        ui, pbi = request.files.get("ui"), request.files.get("powerbi")
        if not ui or not ui.filename or not pbi or not pbi.filename:
            return jsonify({"error": "Please upload both the Reg Transform UI CSV and the Power BI Excel file."}), 400
        ui_name, pbi_name = secure_filename(ui.filename) or "ui.csv", secure_filename(pbi.filename) or "powerbi.xlsx"
        if not ui_name.lower().endswith(".csv"):
            return jsonify({"error": "The Reg Transform UI export must be a .csv file."}), 400
        if not pbi_name.lower().endswith((".xlsx", ".xls")):
            return jsonify({"error": "The Power BI export must be an .xlsx or .xls file."}), 400
        job_id = uuid.uuid4().hex[:10]
        folder = upload_dir / job_id
        folder.mkdir(parents=True, exist_ok=True)
        ui_path, pbi_path = folder / ui_name, folder / pbi_name
        ui.save(ui_path)
        pbi.save(pbi_path)
        jobs[job_id] = {"state": "running", "steps": [], "error": "", "report": None}
        threading.Thread(target=_run, args=(job_id, ui_path, pbi_path), daemon=True).start()
        return jsonify({"job_id": job_id})

    @bp.route("/api/backlog/status/<job_id>")
    def status(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        rep = _get(job_id)
        return jsonify({
            "state": job["state"], "steps": job["steps"], "error": job["error"], "report": job["report"],
            "excel": {"state": rep.excel_state if rep else "pending", "error": rep.excel_error if rep else ""},
        })

    @bp.route("/api/backlog/<rid>/meta")
    def meta(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "This report is no longer available. Please generate it again."}), 404
        return jsonify({"report": _meta(rep.data, rid),
                        "excel": {"state": rep.excel_state, "error": rep.excel_error}})

    @bp.route("/api/backlog/<rid>/ids")
    def ids(rid: str):
        """Row ids of every book in the current view + filters (for 'select all')."""
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "Report not available"}), 404
        data = rep.data
        filtered = F.apply_filters(data.export, _spec(request.args), data.filter_cols)
        view = request.args.get("view", "all")
        vdf = filtered[F.view_mask(filtered, view)] if view in C.VIEW_CATEGORIES else filtered
        rows = [int(x) for x in vdf["_row"].tolist()[:MAX_SELECT]]
        return jsonify({"rows": rows, "total": len(vdf), "truncated": len(vdf) > MAX_SELECT})

    @bp.route("/api/backlog/<rid>/send-to-url-tool", methods=["POST"])
    def send_to_url_tool(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "This report is no longer available. Please generate it again."}), 404
        body = request.get_json(silent=True) or {}
        wanted = {int(x) for x in (body.get("rows") or [])[:MAX_SELECT] if str(x).lstrip("-").isdigit()}
        if not wanted:
            return jsonify({"error": "Select at least one book first."}), 400
        data = rep.data
        link = data.link_col
        if not link:
            return jsonify({"error": "The Power BI file has no source-link column, so there are no URLs to send."}), 400
        sub = data.export[data.export["_row"].isin(wanted)]
        items, seen, blank, dups = [], set(), 0, 0
        for _, r in sub.iterrows():
            url = "" if is_blank(r[link]) else str(r[link]).strip()
            if not url:
                blank += 1
                continue
            key = url_key(url)
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            items.append({
                "document_id": "" if not data.id_col or is_blank(r[data.id_col]) else str(r[data.id_col]).strip(),
                "book_title": "" if not data.title_col or is_blank(r[data.title_col]) else str(r[data.title_col]).strip(),
                "url": url, "domain": r[C.DOMAIN_COL], "api_result": r["_api_label"], "book_type": r[C.BOOK_TYPE_COL],
            })
        if not items:
            return jsonify({"error": "None of the selected books has a source URL.", "skipped_blank": blank}), 400
        tid = transfer.put({"items": items, "skipped_blank": blank, "duplicates_removed": dups,
                            "source": {"tool": "backlog", "rid": rid, "report_date": format_report_date(data.report_date)}})
        log.info("Sent %d books to the URL Analysis Tool (%d blank, %d duplicate URLs skipped)", len(items), blank, dups)
        return jsonify({"transfer_id": tid, "count": len(items), "skipped_blank": blank, "duplicates_removed": dups})

    @bp.route("/api/backlog/<rid>/query")
    def query(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "This report is no longer available. Please generate it again."}), 404
        data, a = rep.data, request.args
        filtered = F.apply_filters(data.export, _spec(a), data.filter_cols)
        view = a.get("view", "overview")
        vdf = filtered[F.view_mask(filtered, view)] if view in C.VIEW_CATEGORIES else filtered
        public = set(data.public_columns)
        cols = [c for c in a.getlist("col") if c in public] or F.default_columns(data.public_columns)
        rows = [] if view == "overview" else F.table_page(
            vdf, cols, data.api_col, a.get("sort", ""), a.get("dir", "asc") != "desc",
            _int(a.get("page"), 1), _int(a.get("size"), 50))
        return jsonify({
            "total_all": len(data.export), "total_filtered": len(filtered), "view_total": len(vdf),
            "cards": F.cards(filtered), "view_counts": F.view_counts(filtered),
            "domains": F.domain_summary(filtered), "apis": F.api_summary(filtered),
            "book_types": F.book_type_summary(filtered), "rows": rows, "columns": cols,
        })

    @bp.route("/api/backlog/<rid>/download/filtered")
    def download_filtered(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "Report not available"}), 404
        data = rep.data
        filtered = F.apply_filters(data.export, _spec(request.args), data.filter_cols)
        view = request.args.get("view", "overview")
        vdf = filtered[F.view_mask(filtered, view)] if view in C.VIEW_CATEGORIES else filtered
        csv = _csv_safe(F.public_view(vdf)).to_csv(index=False)
        name = f"Backlog_{format_report_date(data.report_date)}_{view}_filtered.csv"
        return Response("﻿" + csv, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @bp.route("/api/backlog/<rid>/download/selected", methods=["POST"])
    def download_selected(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "Report not available"}), 404
        body = request.get_json(silent=True) or {}
        wanted = {int(x) for x in (body.get("rows") or [])[:MAX_SELECT] if str(x).lstrip("-").isdigit()}
        if not wanted:
            return jsonify({"error": "Select at least one book first."}), 400
        data = rep.data
        sub = data.export[data.export["_row"].isin(wanted)]
        csv = _csv_safe(F.public_view(sub)).to_csv(index=False)
        name = f"Backlog_{format_report_date(data.report_date)}_selected.csv"
        return Response("﻿" + csv, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @bp.route("/api/backlog/<rid>/download/report")
    def download_report(rid: str):
        rep = _get(rid)
        if not rep:
            return jsonify({"error": "Report not available"}), 404
        if rep.excel_state != "done" or not rep.excel_path or not rep.excel_path.exists():
            msg = rep.excel_error or "The Excel report is still being prepared."
            return jsonify({"error": msg}), 409
        return send_file(rep.excel_path, as_attachment=True,
                         download_name=f"{rep.data.output_stem}_Report.xlsx")

    return bp
