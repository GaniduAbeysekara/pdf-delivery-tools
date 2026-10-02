"""Common input record and result model for every URL-analysis input path."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Source labels shown in the dashboard and written to the export.
SOURCE_MANUAL = "Manual URL Input"
SOURCE_EXCEL = "Excel Batch Upload"
SOURCE_BACKLOG = "Daily Backlog"
SOURCE_PAGE = "Pasted Page Source"


@dataclass
class BookRecord:
    """One URL to analyse plus whatever is known about the book/document it belongs to."""
    url: str
    document_id: str = ""
    book_title: str = ""
    domain: str = ""
    api_result: str = ""
    book_type: str = ""
    code: str = ""                  # Reg Transform Code of the book
    spidering_template: str = ""    # Reg Transform SpideringTemplate of the book
    extra: dict = field(default_factory=dict)   # preserved source columns (e.g. from an uploaded Excel)
    page_html: str = ""             # HTML of the page supplied by the user (page is then NOT requested); never exported

    @classmethod
    def from_dict(cls, d: dict) -> "BookRecord":
        title = d.get("book_title") or d.get("title") or ""
        return cls(
            url=str(d.get("url") or "").strip(), document_id=str(d.get("document_id") or "").strip(),
            book_title=str(title).strip(), domain=str(d.get("domain") or "").strip(),
            api_result=str(d.get("api_result") or "").strip(), book_type=str(d.get("book_type") or "").strip(),
            code=str(d.get("code") or "").strip(),
            spidering_template=str(d.get("spidering_template") or d.get("SpideringTemplate") or "").strip(),
            extra={str(k): str(v) for k, v in (d.get("extra") or {}).items()},
        )


@dataclass
class URLAnalysisResult:
    """The single standardised result produced by the engine, whatever the input source."""
    url: str
    document_id: str = ""
    book_title: str = ""
    domain: str = ""
    api_result: str = ""
    book_type: str = ""
    code: str = ""
    spidering_template: str = ""
    extra: dict = field(default_factory=dict)
    http_status: int | None = None        # status of the URL itself (first response, e.g. 301)
    final_status: int | None = None       # status after following redirects
    final_url: str = ""
    redirects: int = 0
    status: str = "ERROR"                 # WORKING / NOT_WORKING / TIMEOUT / BLOCKED / ERROR
    url_type: str = "UNKNOWN"             # DIRECT_PDF / DIRECT_DOCUMENT / LANDING_PAGE / NOT_WORKING / UNKNOWN
    mime_type: str = ""                   # raw Content-Type header (mime only)
    content_type: str = ""                # PDF / DOC / DOCX / HTML / Other
    direct_document: bool = False
    document_url: str = ""                # the document the metadata below belongs to (itself, or the followed link)
    document_link_type: str = ""          # DIRECT / SINGLE / MULTIPLE / NONE ("" when not applicable)
    document_link_count: int | None = None
    document_urls: list[str] = field(default_factory=list)   # every unique document link found on a landing page
    page_count: str = "N/A"               # number, "Unknown" (document unreadable) or "N/A" (not applicable)
    file_size_bytes: int | None = None
    file_size: str = "Unknown"
    language: str = "N/A"
    last_modified: str = "Unknown"
    text_extraction_status: str = ""      # OK / NO_TEXT / ERROR (scanned PDFs have NO_TEXT)
    analysis_status: str = "Failed"       # Success | Failed | Unknown
    error: str = ""
    notes: list[str] = field(default_factory=list)
    cached: bool = False                  # True when reused from the cache instead of a fresh check
    analyzed_at: str = ""                 # UTC time of the live check ("YYYY-MM-DD HH:MM:SS")

    def with_record(self, rec: BookRecord) -> "URLAnalysisResult":
        """Copy of this result carrying another record's identity and source columns."""
        copy = URLAnalysisResult(**{**asdict(self), "notes": list(self.notes)})
        copy.url = rec.url or self.url
        copy.document_id, copy.book_title, copy.api_result = rec.document_id, rec.book_title, rec.api_result
        copy.book_type, copy.extra = rec.book_type, dict(rec.extra)
        copy.code, copy.spidering_template = rec.code, rec.spidering_template
        if rec.domain:
            copy.domain = rec.domain
        return copy

    def to_dict(self) -> dict:
        d = asdict(self)
        d["direct_document"] = "Yes" if self.direct_document else "No"
        d["http_status"] = "" if self.http_status is None else str(self.http_status)
        d["final_status"] = "" if self.final_status is None else str(self.final_status)
        d["document_link_count"] = "" if self.document_link_count is None else str(self.document_link_count)
        return d
