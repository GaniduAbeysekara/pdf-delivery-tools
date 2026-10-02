"""Persistent, editable table of Base Templates (the team's "Base Template" sheet).

Rows live in one JSON file (atomic writes, thread-safe). The page, the API and the Excel import/export all go
through this class, so the rules (validation, ordering, duplicate warnings) exist exactly once.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from ..backlog import config as backlog_config
from ..backlog.normalize import extract_domain

FIELDS = ["created_date", "template", "domain", "added_to_replit", "dev_name", "comments"]
LABELS = {
    "created_date": "Created Date", "template": "Monitoring Templates (SpideringTemplate)", "domain": "Cached Domain",
    "added_to_replit": "Added to Replit", "dev_name": "Dev Name", "comments": "Comments",
}
LIMITS = {"template": 300, "domain": 500, "added_to_replit": 60, "dev_name": 100, "comments": 2000}
NOT_SET = "__blank__"                    # filter value meaning "the field is empty"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class ValidationError(ValueError):
    """A problem with the submitted values, safe to show to the user."""


def parse_date(value) -> str:
    """ISO date string from a date/datetime or common text formats; '' for blank. Raises ValidationError."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValidationError(f"'{text}' is not a valid date (use YYYY-MM-DD)")


_DOMAIN_SPLIT = re.compile(r"[,;\n]+")


def normalize_domains(text: str) -> tuple[str, int]:
    """Cached Domain in the same format as the Domain column everywhere else in the tool.

    Each entry becomes a bare lowercase hostname (scheme, path, port, trailing dot removed) via the shared
    `extract_domain`; several entries (separated by , ; or new lines) are de-duplicated and joined with ', '.
    Text that is not a web address is kept as typed. Returns (value, number of entries not recognised).
    """
    out, seen, unknown = [], set(), 0
    for part in _DOMAIN_SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        host = extract_domain(part)
        if host == backlog_config.UNKNOWN_DOMAIN:
            host, unknown = part, unknown + 1
        if host.lower() not in seen:
            seen.add(host.lower())
            out.append(host)
    return ", ".join(out), unknown


def clean(data: dict) -> dict:
    """Validate and normalise one row. Raises ValidationError with a user-facing message."""
    out = {"created_date": parse_date(data.get("created_date"))}
    for key in ("template", "domain", "added_to_replit", "dev_name", "comments"):
        value = _CONTROL.sub("", "" if data.get(key) is None else str(data.get(key))).strip()
        if key == "comments":
            value = value.replace("\r\n", "\n")
        if len(value) > LIMITS[key]:
            raise ValidationError(f"{LABELS[key]} is too long (maximum {LIMITS[key]} characters)")
        out[key] = value
    out["domain"] = normalize_domains(out["domain"])[0]
    if len(out["domain"]) > LIMITS["domain"]:
        raise ValidationError(f"{LABELS['domain']} is too long (maximum {LIMITS['domain']} characters)")
    if not out["template"]:
        raise ValidationError("Monitoring Template (SpideringTemplate) is required")
    if re.search(r"\s", out["template"]):
        raise ValidationError("A template name cannot contain spaces")
    return out


def signature(row: dict) -> tuple:
    """What makes two rows 'the same' when importing without replacing."""
    return (row["template"].lower(), row["created_date"], row["domain"].lower(), row["added_to_replit"].lower(),
            row["dev_name"].lower(), row["comments"].lower())


class BaseTemplateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rows: list[dict] = []
        self._dev_members: list[str] = []
        self._seq = 0
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._rows = [r for r in doc.get("rows", []) if isinstance(r, dict) and r.get("template")]
        for r in self._rows:
            r["domain"] = normalize_domains(str(r.get("domain") or ""))[0]
        self._dev_members = [str(n) for n in doc.get("dev_members", [])]
        self._seq = max([int(r.get("seq", 0)) for r in self._rows] + [0])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rows": self._rows, "dev_members": self._dev_members, "saved": time.strftime("%Y-%m-%d %H:%M:%S")},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)                      # a crash can never leave a half-written file

    def _new(self, row: dict) -> dict:
        self._seq += 1
        return {"id": uuid.uuid4().hex[:12], "seq": self._seq, **row}

    # ── queries ─────────────────────────────────────────────────────────────
    def count(self) -> int:
        with self._lock:
            return len(self._rows)

    def dev_members(self) -> list[str]:
        with self._lock:
            return sorted({*self._dev_members, *(r["dev_name"] for r in self._rows if r["dev_name"])}, key=str.lower)

    def matching(self, q: str = "", replit: str = "", dev: str = "", sort: str = "created_date", desc: bool = True) -> list[dict]:
        with self._lock:
            rows = list(self._rows)
        tokens = q.lower().split()
        if tokens:
            rows = [r for r in rows if all(t in " ".join(str(r[f]) for f in FIELDS).lower() for t in tokens)]
        if replit:
            rows = [r for r in rows if (r["added_to_replit"] == "" if replit == NOT_SET else r["added_to_replit"].lower() == replit.lower())]
        if dev:
            rows = [r for r in rows if (r["dev_name"] == "" if dev == NOT_SET else r["dev_name"].lower() == dev.lower())]
        key = sort if sort in FIELDS else "created_date"
        filled = [r for r in rows if r[key] != ""]
        blank = [r for r in rows if r[key] == ""]                 # empty values always sort last
        filled.sort(key=lambda r: (str(r[key]).lower(), r["seq"]), reverse=desc)
        blank.sort(key=lambda r: r["seq"], reverse=desc)
        return filled + blank

    def page(self, rows: list[dict], page: int = 1, size: int = 50) -> dict:
        size = max(1, min(int(size), 500))
        pages = max(1, -(-len(rows) // size))
        page = max(1, min(int(page), pages))
        return {"rows": rows[(page - 1) * size: page * size], "matched": len(rows), "page": page, "pages": pages, "size": size}

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._rows)
        replit = {}
        for r in rows:
            v = r["added_to_replit"] or "(not set)"
            replit[v] = replit.get(v, 0) + 1
        return {"total": len(rows), "replit": dict(sorted(replit.items(), key=lambda kv: (-kv[1], kv[0]))),
                "without_domain": sum(1 for r in rows if not r["domain"]),
                "devs": self.dev_members(), "latest": max((r["created_date"] for r in rows if r["created_date"]), default="")}

    # ── changes ─────────────────────────────────────────────────────────────
    def _duplicates(self, template: str, ignore_id: str = "") -> int:
        return sum(1 for r in self._rows if r["template"].lower() == template.lower() and r["id"] != ignore_id)

    def add(self, data: dict) -> tuple[dict, list[str]]:
        row = clean(data)
        with self._lock:
            warnings = []
            dup = self._duplicates(row["template"])
            if dup:
                warnings.append(f"A template named '{row['template']}' already exists ({dup} row{'s' if dup > 1 else ''}). The new row was added anyway.")
            new = self._new(row)
            self._rows.append(new)
            self._save()
        return new, warnings

    def update(self, row_id: str, data: dict) -> tuple[dict, list[str]]:
        row = clean(data)
        with self._lock:
            for existing in self._rows:
                if existing["id"] == row_id:
                    dup = self._duplicates(row["template"], row_id)
                    existing.update(row)
                    self._save()
                    return existing, ([f"Another row with this template name exists ({dup})."] if dup else [])
        raise KeyError(row_id)

    def delete(self, row_id: str) -> None:
        with self._lock:
            kept = [r for r in self._rows if r["id"] != row_id]
            if len(kept) == len(self._rows):
                raise KeyError(row_id)
            self._rows = kept
            self._save()

    def import_rows(self, rows: list[dict], mode: str = "add_new", dev_members: list[str] | None = None) -> dict:
        """mode 'replace': the table becomes exactly `rows`; 'add_new': only rows not already present are appended."""
        cleaned, rejected = [], []
        reformatted = unrecognised = 0
        for i, raw in enumerate(rows, 1):
            try:
                cleaned.append(clean(raw))
                before = str(raw.get("domain") or "").strip()
                reformatted += before != cleaned[-1]["domain"]
                unrecognised += normalize_domains(before)[1]
            except ValidationError as e:
                rejected.append(f"row {i}: {e}")
        with self._lock:
            if mode == "replace":
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_suffix(".bak"))     # one-step undo for a destructive import
                self._rows, self._seq = [], 0
            have = {signature(r) for r in self._rows}
            added = skipped = 0
            for row in cleaned:
                sig = signature(row)
                if sig in have:
                    skipped += 1
                    continue
                have.add(sig)
                self._rows.append(self._new(row))
                added += 1
            if dev_members:
                self._dev_members = sorted({*self._dev_members, *dev_members}, key=str.lower)
            self._save()
            return {"added": added, "skipped_existing": skipped, "rejected": rejected[:20], "rejected_count": len(rejected),
                    "total": len(self._rows), "mode": mode,
                    "domains_reformatted": reformatted, "domains_not_recognised": unrecognised}
