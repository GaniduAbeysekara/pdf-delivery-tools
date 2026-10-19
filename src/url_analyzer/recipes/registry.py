"""Site recipes: pages the generic 'Find PDFs on a Page' search cannot handle get a dedicated crawler, chosen by the page address.

A recipe turns its findings into ready-made analysis results (the dashboard and Excel export then work as for any other input),
because the files it finds cannot always be fetched by the normal analysis (for example they need the site's own session)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from ..analysis.models import URLAnalysisResult
from ..backlog.normalize import extract_domain
from ..multipdf.extract import FoundPdf
from ..multipdf.finder import Discovery, FindError
from . import cbj


@dataclass
class Recipe:
    key: str
    name: str
    description: str
    options: list[dict]
    matches: Callable[[str], bool]
    run: Callable[..., Discovery]

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "description": self.description, "options": self.options}


def _human_size(n: int | None) -> str:
    from ..analysis.document_analyzer import human_size
    return human_size(n)


def attachments_to_results(atts: list[cbj.Attachment], page_url: str) -> list[URLAnalysisResult]:
    """One analysis result per attachment, in the shape every other input produces."""
    out = []
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    kinds = {"pdf": "PDF", "doc": "DOC", "docx": "DOCX"}
    for a in atts:
        read = a.size_bytes is not None
        r = URLAnalysisResult(url=a.url or "", document_id=a.attachment_id, book_title=a.name, domain=extract_domain(a.url) if a.url else "",
                              extra={"Section Path": a.hierarchy, "Source Page": page_url, "Site IDs": a.app_ids,
                                     "Found In": a.found_in, "Level": a.level}, status="WORKING", url_type="DIRECT_DOCUMENT",
                              mime_type=a.content_type, content_type=kinds.get(a.ext, "Other"), direct_document=True, document_url=a.url or "",
                              document_link_type="DIRECT", analyzed_at=now)
        if read:
            r.http_status = r.final_status = 200
            r.file_size_bytes, r.file_size = a.size_bytes, _human_size(a.size_bytes)
            r.page_count = a.pages or "Unknown"
            r.language = a.language or ("Unknown" if a.ext in ("pdf", "docx") else "N/A")
            r.last_modified = a.last_modified or "Unknown"
            r.analysis_status = "Failed" if (a.read_error and a.size_bytes == 0) else ("Unknown" if a.read_error else "Success")
            r.error = a.read_error
        else:
            r.page_count, r.file_size, r.language, r.last_modified = "Unknown", "Unknown", "N/A", "Unknown"
            r.analysis_status = "Unknown"
            r.error = ("This address only works inside the site's own session, so the file was not fetched. Tick 'Read the files' "
                       "(Find PDFs on a Page) to get its size, pages and date.")
        if not a.url:
            r.status, r.analysis_status, r.error = "ERROR", "Failed", "The attachment was listed but clicking it made no download request."
        out.append(r)
    return out


# ── Superintendencia Financiera de Colombia: Circular Basica Juridica ────────
def _matches_cbj(url: str) -> bool:
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    path = u.path.lower()
    return host.endswith("superfinanciera.gov.co") and ("circular-basica-juridica" in path or "visualizadorcbj" in path)


def _run_cbj(url: str, options: dict | None = None, progress=None, cancelled=None) -> Discovery:
    options = options or {}
    parte = options.get("parte")
    try:
        parte = int(parte) if parte not in (None, "", "all") else None
    except (TypeError, ValueError):
        raise FindError("The Parte must be a number (1, 2 or 3) or empty.")
    if not url.startswith("http"):
        url = "https://" + url
    try:
        res = cbj.crawl(url, only_parte=parte, read_files=bool(options.get("read_files")), progress=progress, cancelled=cancelled)
    except RuntimeError as e:
        raise FindError(str(e)) from e
    except Exception as e:
        if type(e).__name__ in ("Error", "TimeoutError"):                      # Playwright's own errors
            raise FindError(f"The page could not be processed in the browser: {str(e).splitlines()[0][:160]}") from e
        raise
    d = Discovery(mode="recipe", final_url=url)
    d.found = [FoundPdf(url=a.url, title=a.name, path=a.hierarchy, attr=a.found_in, node_id=a.attachment_id, source=a.page_url) for a in res.attachments]
    d.prepared = attachments_to_results(res.attachments, url)
    ok = sum(1 for c in res.chapters if c.status == "ok")
    dup = sum(1 for c in res.chapters if c.status == "duplicate")
    d.notes = [f"Superfinanciera recipe: read {ok} Capítulo(s)" + (f", skipped {dup} repeated menu row(s)" if dup else "") + f"; {len(res.attachments)} attachment(s)."]
    d.notes += res.notes
    if res.errors:
        d.notes.append(f"{len(res.errors)} Capítulo(s) could not be read: " + "; ".join(f"{c.capitulo[:40]} ({c.error[:60]})" for c in res.errors[:5]))
    d.stats = {"pages_opened": ok, "pages_failed": len(res.errors), "duplicates": dup, "seconds": res.seconds, "levels": []}
    return d


CBJ = Recipe(
    key="superfinanciera-cbj", name="Superfinanciera - Circular Básica Jurídica",
    description=("This page is recognised: its content is loaded on demand inside an iframe, so a dedicated crawler opens every Parte, Título and "
                 "Capítulo and lists the attachments of each (PDF, DOC/DOCX, XLS/XLSX ...). The scope and the other options below are not used."),
    options=[{"key": "parte", "type": "select", "label": "Only this Parte", "choices": [["", "All Partes"], ["1", "Parte I"], ["2", "Parte II"], ["3", "Parte III"]]},
             {"key": "read_files", "type": "checkbox",
              "label": "Read the files (downloads each one into memory inside the site's session, to get size, pages and date; nothing is saved)"}],
    matches=_matches_cbj, run=_run_cbj)

RECIPES = [CBJ]


def find_recipe(url: str) -> Recipe | None:
    return next((r for r in RECIPES if r.matches(url or "")), None)
