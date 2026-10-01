"""Constants for the PDF Daily Backlog feature (column names, categories, sheet names)."""
from __future__ import annotations

# ── Source columns (matched case-insensitively; first match wins) ────────────
UI_BOOK_TYPE_COLS = ["BookCategory"]
UI_KEY_COLS = ["Code", "CubeBookId", "DocumentId"]
UI_URL_COLS = ["Url"]

PBI_API_COLS = ["API Result"]
PBI_KEY_COLS = ["BookSourceId", "SourceFileName"]

# The identifier that links a Power BI record to its Reg Transform UI record
# (confirmed against real exports: UI DocumentId == Power BI BookSourceId).
# Auto-detection over UI_KEY_COLS x PBI_KEY_COLS is only a fallback if these columns are absent.
MATCH_KEY_PAIRS = [("DocumentId", "BookSourceId")]

PBI_LINK_COLS = ["Link to the Issuance", "Source Link", "Link"]
PBI_ID_COLS = ["BookSourceId", "SourceFileName"]
PBI_TITLE_COLS = ["BookTitle", "Book Title", "Title"]

# ── Derived columns added to the Export tab ─────────────────────────────────
BOOK_TYPE_COL = "Book Type"
DOMAIN_COL = "Domain"

# ── Book types ───────────────────────────────────────────────────────────────
BT_PDF, BT_HTML, BT_HTML_PDF, BT_OTHER = "PDF", "HTML", "HTML + PDF", "Other"
BT_UNKNOWN = "Unknown"       # matched UI record whose BookCategory is blank
BT_UNMATCHED = "Unmatched"   # no Reg Transform record for the BookSourceId: no Book Type is assigned
# Only used to ORDER known values; the filter/summary lists come from the actual data.
BOOK_TYPE_ORDER = [BT_PDF, BT_OTHER, BT_HTML, BT_HTML_PDF, BT_UNKNOWN, BT_UNMATCHED]
ACTION_BOOK_TYPES = {BT_PDF, BT_OTHER}
UNKNOWN_DOMAIN = "Unknown"
MIN_MATCH_RATIO = 0.8   # below this share of matched Power BI records the UI file is probably partial/filtered

# ── API Result categories (values compared after normalisation) ──────────────
CAT_CHANGE, CAT_POTENTIAL = "change", "potential"
CAT_EVC_OPEN, CAT_EVC, CAT_DEV, CAT_OTHER = "evc_open", "evc", "dev", "other"

API_CHANGE = "change"
API_POTENTIAL = "stream 3 - potential changes bau skim"
API_EVC_OPEN = "stream 5 - externally verified changed - open task"
API_EVC = "stream 5 - externally verified changed"
# "Stream 2 - " prefix optional; "Spidering - Template Error" intentionally not matched.
DEV_ISSUE_PATTERN = (
    r"^(stream 2\s*-\s*)?(monitoring failure|source modified|template error|image base64 issue)"
)

CATEGORY_LABELS = {
    CAT_CHANGE: "BAU review",
    CAT_POTENTIAL: "BAU review",
    CAT_EVC_OPEN: "Verification",
    CAT_EVC: "Verification",
    CAT_DEV: "Dev attention",
    CAT_OTHER: "",
}

# ── Views / action lists ─────────────────────────────────────────────────────
VIEW_ALL, VIEW_CHANGE, VIEW_EVC_OPEN, VIEW_EVC, VIEW_DEV = "all", "change", "evc_open", "evc", "dev"
VIEW_CATEGORIES = {
    VIEW_CHANGE: {CAT_CHANGE, CAT_POTENTIAL},
    VIEW_EVC_OPEN: {CAT_EVC_OPEN},
    VIEW_EVC: {CAT_EVC},
    VIEW_DEV: {CAT_DEV},
}
ACTION_SHEETS = {  # sheet name -> view
    "Change and Potential Change": VIEW_CHANGE,
    "EVC Open Task": VIEW_EVC_OPEN,
    "External Verified Change": VIEW_EVC,
    "Stream 2 - Dev Attn Needed": VIEW_DEV,
}

# ── Optional filters: shown only when the column exists and has values ───────
OPTIONAL_FILTERS = [
    ("Status", ["URL Status", "Status"]),
    ("Owner", ["Assigned To", "Assignee", "Owner", "ReleaseReview.UserName"]),
    ("Language", ["Language"]),
    ("Jurisdiction", ["CUBE Jurisdiction", "Jurisdiction"]),
    ("Issuing Body", ["CUBE Issuing Body", "Issuing Body"]),
]
DATE_COLS = ["APIResult_ChangeDate", "API Last Checked Date"]
MAX_FILTER_OPTIONS = 2000

# ── Dashboard table ──────────────────────────────────────────────────────────
DEFAULT_TABLE_COLS = [
    "BookSourceId", "BookTitle", DOMAIN_COL, "API Result", BOOK_TYPE_COL,
    "Link to the Issuance", "URL Status", "Language", "Assigned To",
    "CUBE Jurisdiction", "CUBE Issuing Body", "APIResult_ChangeDate",
]
MAX_PAGE_SIZE = 500

# ── Workbook ─────────────────────────────────────────────────────────────────
SHEET_EXPORT, SHEET_PIVOT = "Export", "Pivot"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
