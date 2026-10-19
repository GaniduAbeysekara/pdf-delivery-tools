"""Flask blueprint for the URL Analysis tool (one tool, three input methods, one service)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, redirect, render_template, request, send_file
from werkzeug.utils import secure_filename

from .. import transfer
from ..analysis import (SOURCE_BACKLOG, SOURCE_EXCEL, SOURCE_MANUAL, SOURCE_PAGE, BookRecord, InputError, inspect_excel,
                        parse_pasted, read_excel_records)
from ..analysis.engine import MAX_SUPPLIED_HTML, default_analyzer
from ..analysis.inputs import MAX_ITEMS, ParseOutcome, dedupe
from ..logger import log
from ..multipdf.finder import FindError, PdfFinder
from ..recipes import registry
from ..multipdf.service import FindService
from ..services.url_analysis_service import UrlAnalysisService

MAX_TEXT = 2_000_000
UPLOAD_TTL = 3600


def create_url_analysis_blueprint(output_dir: str | Path, upload_dir: str | Path,
                                  service: UrlAnalysisService | None = None, finder: PdfFinder | None = None) -> Blueprint:
    bp = Blueprint("url_analysis", __name__)
    upload_dir = Path(upload_dir)
    svc = service or UrlAnalysisService(output_dir)
    finder = finder or PdfFinder(svc.analyzer or default_analyzer())
    finds = FindService(finder, svc)
    uploads: dict[str, dict] = {}

    def prune_uploads() -> None:
        now = time.time()
        for uid in [u for u, v in uploads.items() if now - v["ts"] > UPLOAD_TTL]:
            Path(uploads.pop(uid)["path"]).unlink(missing_ok=True)

    @bp.route("/url-analysis")
    def page():
        return render_template("url_tool.html")

    @bp.route("/url-analysis/excel")
    def legacy_excel_page():
        return redirect("/url-analysis?tab=excel")

    @bp.route("/api/transfer/<tid>")
    def get_transfer(tid: str):
        payload = transfer.get(tid)
        if payload is None:
            return jsonify({"error": "This hand-off has expired. Please send the books again."}), 404
        return jsonify(payload)

    # ── input 1 + 3: pasted text, and records handed over from the Daily Backlog ──────────────
    @bp.route("/api/url-analysis/analyze", methods=["POST"])
    def analyze():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "auto")
        if mode == "items":                       # records transferred from the Daily Backlog
            raw = body.get("items") or []
            if not isinstance(raw, list):
                return jsonify({"error": "Invalid request."}), 400
            outcome = dedupe([BookRecord.from_dict(d) for d in raw[:MAX_ITEMS] if isinstance(d, dict)], ParseOutcome())
            source, source_name = SOURCE_BACKLOG, ""
        elif mode == "page_source":                   # HTML copied from the user's own browser
            url, html = str(body.get("url") or "").strip(), str(body.get("html") or "")
            if not url:
                return jsonify({"error": "Enter the address of the page the HTML came from."}), 400
            if "<" not in html or len(html.strip()) < 20:
                return jsonify({"error": "Paste the page's HTML source (see the steps above the box)."}), 400
            if len(html) > MAX_SUPPLIED_HTML:
                return jsonify({"error": "The page source is too large (limit 5 MB)."}), 413
            rec = BookRecord.from_dict({**body, "url": url})
            rec.page_html = html
            outcome = ParseOutcome(items=[rec])
            source, source_name = SOURCE_PAGE, ""
        elif mode in ("auto", "urls", "book"):
            text = str(body.get("text") or "")
            if len(text) > MAX_TEXT:
                return jsonify({"error": "The pasted text is too large."}), 413
            outcome = parse_pasted(text, mode)
            source, source_name = SOURCE_MANUAL, ""
        else:
            return jsonify({"error": "Unknown input mode."}), 400
        if not outcome.items:
            msg = ("No books with URLs were received." if mode == "items"
                   else "No URLs were found. Paste at least one URL (one per line).")
            return jsonify({"error": msg, "notes": outcome.notes}), 400
        info = svc.start(outcome.items, source=source, source_name=source_name, notes=outcome.notes,
                         duplicates_removed=outcome.duplicates_removed, source_columns=outcome.source_columns,
                         use_browser=bool(body.get("browser")))
        info["invalid"] = outcome.invalid_lines
        return jsonify(info)

    # ── input 2: Excel batch upload (inspect -> choose columns -> analyze) ───────────────────
    @bp.route("/api/url-analysis/excel/inspect", methods=["POST"])
    def excel_inspect():
        prune_uploads()
        f = request.files.get("file")
        name = secure_filename(f.filename) if f and f.filename else ""
        if not name or not name.lower().endswith((".xlsx", ".xls")):
            return jsonify({"error": "Please choose an .xlsx or .xls file."}), 400
        uid = uuid.uuid4().hex[:12]
        folder = upload_dir / uid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        f.save(path)
        try:
            ins = inspect_excel(str(path))
        except InputError as e:
            path.unlink(missing_ok=True)
            return jsonify({"error": str(e)}), 400
        uploads[uid] = {"path": str(path), "name": name, "ts": time.time()}
        return jsonify({"upload_id": uid, "file_name": name, "sheet": ins.sheet, "columns": ins.columns,
                        "rows": ins.row_count, "url_column": ins.url_column, "confident": ins.confident,
                        "candidates": ins.candidates, "id_column": ins.id_column,
                        "title_column": ins.title_column, "preview": ins.preview,
                        "code_column": ins.code_column, "template_column": ins.template_column})

    @bp.route("/api/url-analysis/excel/analyze", methods=["POST"])
    def excel_analyze():
        body = request.get_json(silent=True) or {}
        up = uploads.get(str(body.get("upload_id") or ""))
        if not up or not Path(up["path"]).exists():
            return jsonify({"error": "The uploaded file has expired. Please choose the file again."}), 410
        if not body.get("url_column"):
            return jsonify({"error": "Select the URL column first."}), 400
        try:
            data = read_excel_records(up["path"], body.get("url_column"), body.get("id_column") or None,
                                      body.get("title_column") or None, body.get("code_column") or None,
                                      body.get("template_column") or None)
        except InputError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            pass
        Path(up["path"]).unlink(missing_ok=True)       # the rows now live in the job
        uploads.pop(str(body["upload_id"]), None)
        info = svc.start(data.records, source=SOURCE_EXCEL, source_name=up["name"],
                         source_columns=data.source_columns, notes=data.notes, use_browser=bool(body.get("browser")))
        return jsonify(info)

    # ── common job endpoints ────────────────────────────────────────────────────────────────
    @bp.route("/api/url-analysis/<job_id>/status")
    def status(job_id: str):
        try:
            start = max(0, int(request.args.get("from", 0)))
        except ValueError:
            start = 0
        st = svc.status(job_id, start)
        return (jsonify(st), 200) if st else (jsonify({"error": "Unknown analysis."}), 404)

    @bp.route("/api/url-analysis/<job_id>/page-source", methods=["POST"])
    def page_source(job_id: str):
        """Replace one row's analysis with HTML the user copied from their browser (e.g. for BLOCKED pages)."""
        if not svc.get(job_id):
            return jsonify({"error": "Unknown analysis."}), 404
        body = request.get_json(silent=True) or {}
        html = str(body.get("html") or "")
        if len(html) > MAX_SUPPLIED_HTML:
            return jsonify({"error": "The page source is too large (limit 5 MB)."}), 413
        err = svc.apply_page_source(job_id, body.get("index"), html)
        return (jsonify({"error": err}), 409 if "Wait" in err else 400) if err else jsonify({"started": True})

    @bp.route("/api/url-analysis/<job_id>/reanalyze", methods=["POST"])
    def reanalyze(job_id: str):
        body = request.get_json(silent=True) or {}
        indices = body.get("indices")
        if indices is not None and not isinstance(indices, list):
            return jsonify({"error": "Invalid request."}), 400
        if not svc.get(job_id):
            return jsonify({"error": "Unknown analysis."}), 404
        browser = body.get("browser")
        ok = svc.reanalyze(job_id, [i for i in indices if isinstance(i, int)] if indices is not None else None,
                           use_browser=None if browser is None else bool(browser))
        if not ok:
            return jsonify({"error": "Nothing to re-analyze (is an analysis still running?)."}), 409
        return jsonify({"started": True})

    # ── input 5: a page that lists many PDFs (collapsed sections are opened in a browser when needed) ──────────
    def need_fetch_header():
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"error": "Missing X-Requested-With header"}), 400
        return None

    @bp.route("/api/url-analysis/find-pdfs/capabilities")
    def find_capabilities():
        return jsonify({"browser": finder.browser_ok()})

    @bp.route("/api/url-analysis/find-pdfs/recipe")
    def find_recipe():
        """Does this page have a dedicated crawler? The page shows its options when it does."""
        r = registry.find_recipe(request.args.get("url", ""))
        return jsonify({"recipe": r.describe() if r else None})

    @bp.route("/api/url-analysis/find-pdfs", methods=["POST"])
    def find_pdfs():
        if (bad := need_fetch_header()):
            return bad
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(finds.start(body))
        except FindError as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/api/url-analysis/find-pdfs/<fid>")
    def find_status(fid: str):
        st = finds.status(fid)
        return (jsonify(st), 200) if st else (jsonify({"error": "This search is no longer available."}), 404)

    @bp.route("/api/url-analysis/find-pdfs/<fid>/cancel", methods=["POST"])
    def find_cancel(fid: str):
        if (bad := need_fetch_header()):
            return bad
        return (jsonify({"cancelled": True}), 200) if finds.cancel(fid) else (jsonify({"error": "This search is no longer available."}), 404)

    @bp.route("/api/url-analysis/<job_id>/cancel", methods=["POST"])
    def cancel_analysis(job_id: str):
        """Stop a running analysis. URLs already checked keep their results (and can be downloaded)."""
        if (bad := need_fetch_header()):
            return bad
        state = svc.cancel(job_id)
        if state is None:
            return jsonify({"error": "Unknown analysis."}), 404
        return jsonify({"stopping": state == "running", "state": state})

    @bp.route("/api/url-analysis/<job_id>/send-to-base-templates", methods=["POST"])
    def send_to_base_templates(job_id: str):
        """Hand the (selected) results to Base Template Analysis. The data is read from the job, never from the browser."""
        job = svc.get(job_id)
        if not job:
            return jsonify({"error": "Unknown analysis."}), 404
        indices = (request.get_json(silent=True) or {}).get("indices")
        if indices is not None and (not isinstance(indices, list) or not all(isinstance(i, int) for i in indices)):
            return jsonify({"error": "indices must be a list of numbers."}), 400
        if indices is not None and not indices:
            return jsonify({"error": "Select at least one URL first."}), 400
        rows = svc.dataset_rows(job_id, indices)
        if not rows:
            return jsonify({"error": "There are no finished results to send yet."}), 409
        tid = transfer.put({"records": rows, "source": {"tool": "url_analysis", "job_id": job_id, "name": job["source_name"] or "URL Analysis results"}})
        log.info("Sent %d URL analysis rows to Base Template Analysis", len(rows))
        return jsonify({"transfer_id": tid, "count": len(rows)})

    # ── Download PDFs: save the documents into a NEW folder, named by Document ID ───────────────────────
    def is_local() -> bool:
        return request.remote_addr in ("127.0.0.1", "::1", None)

    @bp.route("/api/url-analysis/downloads/defaults")
    def download_defaults():
        return jsonify({"base": str(svc.download_base.resolve()), "local": is_local()})

    @bp.route("/api/url-analysis/<job_id>/downloads", methods=["POST"])
    def start_download(job_id: str):
        if (bad := need_fetch_header()):
            return bad
        if not svc.get(job_id):
            return jsonify({"error": "Unknown analysis."}), 404
        body = request.get_json(silent=True) or {}
        indices = body.get("indices")
        if indices is not None and (not isinstance(indices, list) or not indices or not all(isinstance(i, int) for i in indices)):
            return jsonify({"error": "indices must be a list of numbers."}), 400
        base = str(body.get("folder") or "").strip().strip('"')
        if base:
            if not is_local():
                return jsonify({"error": "Choosing a folder is only possible on the computer that runs the tool."}), 403
            p = Path(os.path.expandvars(base)).expanduser()
            if not p.is_absolute():
                return jsonify({"error": "Type the full path of the folder, for example C:\\Users\\you\\Documents\\Backlog PDFs (a new folder is made inside it)."}), 400
            base = str(p)
        info = svc.start_download(job_id, indices, use_browser=bool(body.get("browser")), base=base or None)
        return (jsonify(info), 400) if "error" in info else jsonify(info)

    @bp.route("/api/url-analysis/downloads/<did>")
    def download_status(did: str):
        try:
            start = max(0, int(request.args.get("from", 0)))
        except ValueError:
            start = 0
        st = svc.download_status(did, start)
        return (jsonify(st), 200) if st else (jsonify({"error": "This download is no longer available."}), 404)

    @bp.route("/api/url-analysis/downloads/<did>/cancel", methods=["POST"])
    def download_cancel(did: str):
        if (bad := need_fetch_header()):
            return bad
        return (jsonify({"stopping": True}), 200) if svc.cancel_download(did) else (jsonify({"error": "This download is no longer available."}), 404)

    @bp.route("/api/url-analysis/downloads/<did>/skip", methods=["POST"])
    def download_skip(did: str):
        """Skip one file that is being fetched; the saving goes on with the others."""
        if (bad := need_fetch_header()):
            return bad
        row = (request.get_json(silent=True) or {}).get("row")
        if not isinstance(row, int):
            return jsonify({"error": "row must be a number."}), 400
        return (jsonify({"skipped": True}), 200) if svc.skip_download_item(did, row) else (jsonify({"error": "That file is not being fetched any more."}), 404)

    @bp.route("/api/url-analysis/<job_id>/skip-browser", methods=["POST"])
    def skip_browser(job_id: str):
        """During an analysis: skip the URL the browser window is waiting on."""
        if (bad := need_fetch_header()):
            return bad
        if not svc.get(job_id):
            return jsonify({"error": "Unknown analysis."}), 404
        return (jsonify({"skipped": True}), 200) if svc.skip_browser_wait(job_id) else (jsonify({"error": "Nothing is waiting in the browser window."}), 404)

    @bp.route("/api/url-analysis/downloads/<did>/open-folder", methods=["POST"])
    def download_open_folder(did: str):
        """Show the new folder in the file explorer (only for a folder this tool made, and only on this computer)."""
        if (bad := need_fetch_header()):
            return bad
        folder = svc.download_folder(did)
        if not folder or not Path(folder).is_dir():
            return jsonify({"error": "This folder is no longer available."}), 404
        if not is_local():
            return jsonify({"error": "The folder can only be opened on the computer that runs the tool."}), 403
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)                                  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            return jsonify({"error": f"Could not open the folder: {str(e)[:100]}"}), 500
        return jsonify({"opened": True})

    @bp.route("/api/url-analysis/<job_id>/download", methods=["GET", "POST"])
    def download(job_id: str):
        """GET: every result. POST {"indices": [...]}: only the selected rows (same exporter)."""
        if not svc.get(job_id):
            return jsonify({"error": "Unknown analysis."}), 404
        indices = None
        if request.method == "POST":
            indices = (request.get_json(silent=True) or {}).get("indices")
            if not isinstance(indices, list) or not indices or not all(isinstance(i, int) for i in indices):
                return jsonify({"error": "Select at least one URL first."}), 400
        path = svc.export(job_id, indices)
        if not path:
            return jsonify({"error": "There are no results to download yet."}), 409
        log.info("Exported URL analysis %s", path)
        return send_file(path, as_attachment=True,
                         download_name="URL_Analysis_Selected.xlsx" if indices else "URL_Analysis_Results.xlsx")

    return bp
