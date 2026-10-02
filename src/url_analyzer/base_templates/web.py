"""Flask blueprint for the Base Templates page (list, search, add/edit/delete, Excel import/export)."""
from __future__ import annotations

import io
import time
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from ..logger import log
from . import excel
from .store import FIELDS, LABELS, BaseTemplateStore, ValidationError

MAX_UPLOAD = 20 * 1024 * 1024


def create_base_templates_blueprint(data_file: str | Path, store: BaseTemplateStore | None = None) -> Blueprint:
    bp = Blueprint("base_templates", __name__)
    st = store or BaseTemplateStore(data_file)

    def guarded():
        """Changes must come from this app's own page (blocks cross-site form posts to localhost)."""
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"error": "Missing X-Requested-With header"}), 400
        return None

    def query_rows():
        a = request.args
        return st.matching(a.get("q", ""), a.get("replit", ""), a.get("dev", ""), a.get("sort", "created_date"),
                           a.get("dir", "desc") != "asc")

    @bp.route("/base-templates")
    def page():
        return render_template("base_templates.html", labels=LABELS, fields=FIELDS)

    @bp.route("/api/base-templates")
    def list_rows():
        rows = query_rows()
        try:
            page = st.page(rows, request.args.get("page", 1), request.args.get("size", 50))
        except ValueError:
            return jsonify({"error": "page and size must be numbers"}), 400
        return jsonify({**page, "summary": st.summary()})

    @bp.route("/api/base-templates", methods=["POST"])
    def add():
        if (bad := guarded()):
            return bad
        try:
            row, warnings = st.add(request.get_json(silent=True) or {})
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"row": row, "warnings": warnings}), 201

    @bp.route("/api/base-templates/<row_id>", methods=["PUT"])
    def update(row_id):
        if (bad := guarded()):
            return bad
        try:
            row, warnings = st.update(row_id, request.get_json(silent=True) or {})
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400
        except KeyError:
            return jsonify({"error": "That row no longer exists"}), 404
        return jsonify({"row": row, "warnings": warnings})

    @bp.route("/api/base-templates/<row_id>", methods=["DELETE"])
    def delete(row_id):
        if (bad := guarded()):
            return bad
        try:
            st.delete(row_id)
        except KeyError:
            return jsonify({"error": "That row no longer exists"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/base-templates/import", methods=["POST"])
    def import_xlsx():
        if (bad := guarded()):
            return bad
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "Choose an Excel file (.xlsx) to import"}), 400
        if not f.filename.lower().endswith((".xlsx", ".xlsm")):
            return jsonify({"error": "Only .xlsx files can be imported (open the file and Save As .xlsx)"}), 400
        mode = request.form.get("mode", "add_new")
        if mode not in ("add_new", "replace"):
            return jsonify({"error": "Unknown import mode"}), 400
        data = f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return jsonify({"error": "The file is larger than 20 MB"}), 400
        try:
            sheet = excel.read_rows(io.BytesIO(data), request.form.get("sheet", "").strip())
        except excel.ImportError_ as e:
            return jsonify({"error": str(e)}), 400
        if mode == "replace" and not sheet["rows"]:
            return jsonify({"error": "The sheet has no data rows, so the table was left unchanged"}), 400
        result = st.import_rows(sheet["rows"], mode)
        result.update(sheet=sheet["sheet"], skipped_blank=sheet["skipped_blank"], rows_in_sheet=len(sheet["rows"]))
        log.info("Base Templates import: %s", {k: v for k, v in result.items() if k != "rejected"})
        return jsonify(result)

    @bp.route("/api/base-templates/export")
    def export():
        rows = query_rows()
        return send_file(io.BytesIO(excel.build_workbook(rows)), as_attachment=True,
                         download_name=f"Base_Templates_{time.strftime('%Y%m%d')}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    return bp
