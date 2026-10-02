"""Runs URL-analysis jobs. Every input path (paste, Excel, Daily Backlog) starts its job here, so they all
share the same engine call, the same result events, the same re-analysis and the same export."""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from ..analysis.engine import MAX_SUPPLIED_HTML
from ..analysis import (BookRecord, URLAnalysisResult, URLAnalyzer, analyze_urls, export_analysis_results)
from ..logger import log

MAX_JOBS = 20


class UrlAnalysisService:
    def __init__(self, output_dir: str | Path, analyzer: URLAnalyzer | None = None):
        self.output_dir = Path(output_dir)
        self.analyzer = analyzer            # None -> the shared default analyzer (shared cache)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── start ───────────────────────────────────────────────────────────────
    def start(self, records: list[BookRecord], *, source: str, source_name: str = "",
              source_columns: list[str] | None = None, notes: list[str] | None = None,
              duplicates_removed: int = 0, force_refresh: bool = False) -> dict:
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "state": "running", "records": records, "total": len(records), "done": 0,
               "batch_total": len(records), "batch_done": 0, "events": [], "results": [None] * len(records),
               "error": "", "source": source, "source_name": source_name, "source_columns": source_columns or [],
               "notes": notes or [], "duplicates_removed": duplicates_removed}
        with self._lock:
            while len(self._jobs) >= MAX_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[job_id] = job
        self._launch(job, list(range(len(records))), force_refresh)
        return {"job_id": job_id, "total": len(records), "source": source, "notes": job["notes"],
                "duplicates_removed": duplicates_removed}

    def _launch(self, job: dict, indices: list[int], force: bool) -> None:
        def on_result(local_idx: int, res: URLAnalysisResult) -> None:
            idx = indices[local_idx]
            with self._lock:
                if job["results"][idx] is None:
                    job["done"] += 1
                job["results"][idx] = res
                job["events"].append({"idx": idx, **res.to_dict()})
                job["batch_done"] += 1

        def run() -> None:
            try:
                analyze_urls([job["records"][i] for i in indices], analyzer=self.analyzer,
                             force_refresh=force, on_result=on_result)
                job["state"] = "done"
            except Exception:
                log.exception("URL analysis job failed")
                job["state"], job["error"] = "error", "The analysis stopped unexpectedly. Please try again."

        job["batch_total"], job["batch_done"], job["state"] = len(indices), 0, "running"
        threading.Thread(target=run, daemon=True).start()

    # ── query / control ─────────────────────────────────────────────────────
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return self._jobs.get(job_id)

    def status(self, job_id: str, start: int = 0) -> dict | None:
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            events = job["events"][start:]
            return {"state": job["state"], "total": job["total"], "done": job["done"],
                    "batch_total": job["batch_total"], "batch_done": job["batch_done"], "events": events,
                    "next": start + len(events), "error": job["error"], "source": job["source"],
                    "source_name": job["source_name"], "notes": job["notes"],
                    "duplicates_removed": job["duplicates_removed"]}

    def reanalyze(self, job_id: str, indices: list[int] | None = None) -> bool:
        """Fresh (non-cached) re-check of the given result rows, or of every row."""
        job = self.get(job_id)
        if not job or job["state"] == "running":
            return False
        valid = sorted({i for i in (indices if indices is not None else range(job["total"]))
                        if isinstance(i, int) and 0 <= i < job["total"]})
        if not valid:
            return False
        self._launch(job, valid, True)
        return True

    def apply_page_source(self, job_id: str, index: int, html: str) -> str:
        """Re-analyse one row from HTML the user copied out of their browser. Returns '' or a user-facing error."""
        job = self.get(job_id)
        if not job:
            return "Unknown analysis."
        if job["state"] == "running":
            return "Wait for the running analysis to finish first."
        if not isinstance(index, int) or not 0 <= index < job["total"]:
            return "That row does not exist."
        if not html or "<" not in html:
            return "Paste the page's HTML source."
        job["records"][index].page_html = html[:MAX_SUPPLIED_HTML]
        self._launch(job, [index], True)
        return ""

    def dataset_rows(self, job_id: str, indices: list[int] | None = None) -> list[dict] | None:
        """Document ID / Domain / Spider Template / URL of finished results (all, or the rows at `indices`)
        for the Base Template Analysis hand-off. `row` is the 1-based position in the results table."""
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            wanted = range(len(job["results"])) if indices is None else sorted({i for i in indices if isinstance(i, int)})
            return [{"row": i + 1, "document_id": r.document_id, "domain": r.domain, "spidering_template": r.spidering_template, "url": r.url}
                    for i in wanted if 0 <= i < len(job["results"]) and (r := job["results"][i]) is not None]

    def export(self, job_id: str, indices: list[int] | None = None) -> Path | None:
        """Excel export of every result, or only the rows at `indices` (the user's selection)."""
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            if indices is None:
                results = [r for r in job["results"] if r is not None]
            else:
                results = [job["results"][i] for i in sorted(set(indices))
                           if isinstance(i, int) and 0 <= i < len(job["results"]) and job["results"][i] is not None]
        if not results:
            return None
        suffix = "" if indices is None else "_selected"
        return export_analysis_results(results, self.output_dir / f"{job_id}_URL_Analysis{suffix}.xlsx",
                                       source=job["source"], source_name=job["source_name"],
                                       source_columns=job["source_columns"])
