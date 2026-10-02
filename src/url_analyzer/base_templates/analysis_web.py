"""Flask blueprint for the Base Template Analysis page (dataset vs Base Templates, joined on domain)."""
from __future__ import annotations

import io
import time
import uuid
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from .. import transfer
from ..logger import log
from . import compare as cmp
from .compare_report import build_report
from .store import BaseTemplateStore

MAX_UPLOAD = 100 * 1024 * 1024
UPLOAD_TTL = 3600
RESULT_TTL = 7200
MAX_RESULTS = 5
ALLOWED = (".xlsx", ".xlsm", ".csv")


def create_analysis_blueprint(upload_dir: str | Path, store: BaseTemplateStore) -> Blueprint:
    bp = Blueprint("base_template_analysis", __name__)
    upload_dir = Path(upload_dir)
    uploads: dict[str, dict] = {}
    results: dict[str, dict] = {}

    def prune() -> None:
        now = time.time()
        for uid in [u for u, v in uploads.items() if now - v["ts"] > UPLOAD_TTL]:
            Path(uploads.pop(uid)["path"]).unlink(missing_ok=True)
        for rid in [r for r, v in results.items() if now - v["ts"] > RESULT_TTL]:
            results.pop(rid)
        while len(results) > MAX_RESULTS:
            results.pop(min(results, key=lambda r: results[r]["ts"]))

    def guarded():
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"error": "Missing X-Requested-With header"}), 400
        return None

    def describe(uid: str, sheet: str = ""):
        up = uploads[uid]
        ds = cmp.load_dataset(up["path"], up["name"], sheet)
        return {"upload_id": uid, "filename": up["name"], "sheets": ds.sheets, "sheet": ds.sheet, "columns": ds.columns,
                "mapping": ds.mapping, "rows": len(ds.frame), "header_row": ds.header_row,
                "preview": [[str(v) for v in row] for row in ds.frame.head(5).itertuples(index=False, name=None)]}

    @bp.route("/base-template-analysis")
    def page():
        return render_template("base_template_analysis.html", flags={k: {"label": v[0], "level": v[1], "help": v[2]} for k, v in cmp.FLAGS.items()},
                               base_flags={k: {"label": v[0], "level": v[1], "help": v[2]} for k, v in cmp.BASE_FLAGS.items()})

    @bp.route("/api/bta/upload", methods=["POST"])
    def upload():
        if (bad := guarded()):
            return bad
        prune()
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "Choose the URL analysis file (.xlsx or .csv)"}), 400
        name = secure_filename(f.filename) or "dataset"
        if not name.lower().endswith(ALLOWED):
            return jsonify({"error": "Only .xlsx and .csv files are supported"}), 400
        upload_dir.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex
        path = upload_dir / f"{uid}{Path(name).suffix.lower()}"
        f.save(path)
        uploads[uid] = {"path": str(path), "name": name, "ts": time.time()}
        try:
            return jsonify(describe(uid))
        except cmp.CompareError as e:
            Path(uploads.pop(uid)["path"]).unlink(missing_ok=True)
            return jsonify({"error": str(e)}), 400

    @bp.route("/api/bta/<uid>/sheet", methods=["POST"])
    def change_sheet(uid):
        if (bad := guarded()):
            return bad
        if uid not in uploads:
            return jsonify({"error": "The upload expired - choose the file again"}), 404
        try:
            return jsonify(describe(uid, (request.get_json(silent=True) or {}).get("sheet", "")))
        except cmp.CompareError as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/api/bta/run", methods=["POST"])
    def run():
        if (bad := guarded()):
            return bad
        body = request.get_json(silent=True) or {}
        uid = body.get("upload_id", "")
        if uid not in uploads:
            return jsonify({"error": "The upload expired - choose the file again"}), 404
        up = uploads[uid]
        try:
            ds = cmp.load_dataset(up["path"], up["name"], body.get("sheet", ""))
            base_rows = store.matching()
            comparison = cmp.compare(ds, body.get("mapping") or {}, base_rows)
        except cmp.CompareError as e:
            return jsonify({"error": str(e)}), 400
        prune()
        rid = uuid.uuid4().hex[:12]
        results[rid] = {"ts": time.time(), "cmp": comparison, "name": up["name"]}
        log.info("Base Template Analysis: %s rows vs %s base templates -> %s need fixing", comparison.summary["dataset_rows"],
                 comparison.summary["base_rows"], comparison.summary["rows_needs_fix"])
        return jsonify({"result_id": rid, "summary": comparison.summary})

    @bp.route("/api/bta/from-transfer", methods=["POST"])
    def from_transfer():
        """Compare rows sent over from the URL Analysis page with the current Base Templates (no file involved)."""
        if (bad := guarded()):
            return bad
        payload = transfer.get((request.get_json(silent=True) or {}).get("transfer_id", ""))
        if payload is None or not payload.get("records"):
            return jsonify({"error": "This hand-off has expired. Send the URLs again from URL Analysis."}), 404
        recs = payload["records"]
        frame = pd.DataFrame({"DocID": [r.get("document_id", "") for r in recs], "Domain": [r.get("domain", "") for r in recs],
                              "SpiderTemplate": [r.get("spidering_template", "") for r in recs], "URL": [r.get("url", "") for r in recs]}, dtype=object)
        mapping = {"document_id": "DocID", "domain": "Domain", "spidering_template": "SpiderTemplate", "url": "URL"}
        name = (payload.get("source") or {}).get("name") or "URL Analysis results"
        try:
            comparison = cmp.compare(frame, mapping, store.matching(), [int(r.get("row", i + 1)) for i, r in enumerate(recs)],
                                     {"filename": f"{name} (from URL Analysis)", "sheet": "URL Analysis results"})
        except cmp.CompareError as e:
            return jsonify({"error": str(e)}), 400
        prune()
        rid = uuid.uuid4().hex[:12]
        results[rid] = {"ts": time.time(), "cmp": comparison, "name": Path(name).stem or "URL_Analysis"}
        log.info("Base Template Analysis from URL Analysis: %d rows -> %d need fixing", len(recs), comparison.summary["rows_needs_fix"])
        return jsonify({"result_id": rid, "summary": comparison.summary, "received": len(recs), "source": payload.get("source", {})})

    def get_result(rid):
        r = results.get(rid)
        if r:
            r["ts"] = time.time()
        return r["cmp"] if r else None

    def paged(items: list, args) -> dict:
        try:
            size = max(1, min(int(args.get("size", 50)), 500))
            page = max(1, int(args.get("page", 1)))
        except ValueError:
            raise cmp.CompareError("page and size must be numbers")
        pages = max(1, -(-len(items) // size))
        page = min(page, pages)
        return {"items": items[(page - 1) * size: page * size], "matched": len(items), "page": page, "pages": pages}

    @bp.route("/api/bta/<rid>/rows")
    def rows(rid):
        c = get_result(rid)
        if c is None:
            return jsonify({"error": "This report is no longer available - run the analysis again"}), 404
        a = request.args
        view = a.get("view", "rows")
        try:
            if view == "domains":
                q = a.get("q", "").lower()
                items = [d for d in c.domains if (not q or q in d["domain"].lower() or any(q in t.lower() for t in d["base_templates"]))
                         and (not a.get("status") or d["status"] == a["status"])]
            elif view == "base":
                q = a.get("q", "").lower()
                items = [b for b in c.base if (not q or q in (b["cached_domain"] + " " + b["template"]).lower())
                         and (not a.get("flag") or a["flag"] in b["flags"])]
            else:
                items = cmp.filter_rows(c.rows, a.get("flag", ""), a.get("status", ""), a.get("q", ""), a.get("domain", ""))
            out = paged(items, a)
        except cmp.CompareError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({**out, "summary": c.summary})

    @bp.route("/api/bta/<rid>/download")
    def download(rid):
        c = get_result(rid)
        if c is None:
            return jsonify({"error": "This report is no longer available - run the analysis again"}), 404
        a = request.args
        subset = None
        if a.get("scope") == "filtered":
            subset = cmp.filter_rows(c.rows, a.get("flag", ""), a.get("status", ""), a.get("q", ""), a.get("domain", ""))
        elif a.get("scope") == "fix":
            subset = [r for r in c.rows if r["status"] == "Needs fix"]
        base = Path(results[rid]["name"]).stem[:60]
        return send_file(io.BytesIO(build_report(c, subset)), as_attachment=True,
                         download_name=f"Base_Template_Analysis_{base}_{time.strftime('%Y%m%d')}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    return bp
