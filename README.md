# URL Document Analyzer

A production-quality Python tool that reads an Excel file containing URLs, determines whether each URL is working, whether it directly points to a PDF/document, or is a landing page with document links — and extracts metadata (page count, last modified date, language) when a single document is found.

---

## What It Does

```
Excel (URLs)
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  For each URL:                                       │
│  1. Check accessibility (WORKING / NOT_WORKING / …)  │
│  2. Detect if it's a direct PDF / DOC / DOCX         │
│  3. If landing page → find document links            │
│     • 0 links → NONE                                 │
│     • 1 link  → SINGLE → extract metadata            │
│     • >1 link → MULTIPLE                             │
│  4. Extract: page count, last modified, language     │
└─────────────────────────────────────────────────────┘
     │
     ▼
Excel (Results) — 3 sheets: Results, SUMMARY, DOCUMENT_LINKS
```

---

## Requirements

- Python 3.11+
- See `requirements.txt`

---

## Installation

```bash
cd url-document-analyzer
pip install -r requirements.txt
```

---

## How to Prepare the Excel File

Your Excel file needs at least one column with URLs. The tool **auto-detects** the URL column by name (looks for `url`, `link`, `uri`, `href` in the column header, case-insensitive).

Example:

| ID  | URL                              | Title      |
|-----|----------------------------------|------------|
| 001 | https://example.com/document.pdf | Document A |
| 002 | https://example.com/page         | Document B |

---

## How to Run

### Basic usage
```bash
python -m url_analyzer --input input/myfile.xlsx
```

### Specify output path
```bash
python -m url_analyzer --input input/myfile.xlsx --output output/results.xlsx
```

### Specify URL column name manually
```bash
python -m url_analyzer --input input/myfile.xlsx --url-column "Link to the Issuance"
```

### Control parallel workers and config
```bash
python -m url_analyzer --input input/myfile.xlsx --workers 10 --config config.yaml
```

---

## Configuration (`config.yaml`)

```yaml
request:
  timeout: 30        # seconds per request
  retries: 3         # retry count on failure
  delay: 0.5         # seconds between requests (per worker)

processing:
  workers: 5         # parallel threads
  url_column: ""     # leave empty for auto-detect

documents:
  extensions:
    - ".pdf"
    - ".doc"
    - ".docx"

browser:
  enabled: false     # set true to enable Playwright fallback
  timeout: 30
```

---

## Output Structure

The tool creates an Excel file with **3 sheets**:

### Sheet 1: Results

All original columns plus:

| Column | Description |
|--------|-------------|
| `ORIGINAL_URL` | The URL from the input file |
| `FINAL_URL` | URL after following redirects |
| `STATUS` | `WORKING` / `NOT_WORKING` / `TIMEOUT` / `BLOCKED` / `ERROR` |
| `HTTP_STATUS` | HTTP status code (200, 404, etc.) |
| `REDIRECT_COUNT` | Number of redirects followed |
| `CONTENT_TYPE` | Server-reported content type |
| `URL_TYPE` | `DIRECT_PDF` / `DIRECT_DOCUMENT` / `LANDING_PAGE` / `NOT_WORKING` / `UNKNOWN` |
| `DOCUMENT_URL` | The resolved document URL (if found) |
| `DOCUMENT_LINK_TYPE` | `DIRECT` / `SINGLE` / `MULTIPLE` / `NONE` |
| `DOCUMENT_LINK_COUNT` | Number of document links found on landing page |
| `LAST_MODIFIED` | Last modified date (YYYY-MM-DD) or `N/A` |
| `PAGE_COUNT` | Number of PDF pages or `N/A` |
| `LANGUAGE` | Detected language or `Unknown` |
| `TEXT_EXTRACTION_STATUS` | `OK` / `NO_TEXT` / `ERROR` |
| `ERROR` | Error message if applicable |

### Sheet 2: SUMMARY

Counts by category (total, working, not working, direct PDFs, landing pages, single/multiple/none document links).

### Sheet 3: DOCUMENT_LINKS

All discovered document links across all landing pages:

| Column | Description |
|--------|-------------|
| `SOURCE_URL` | The landing page URL |
| `DOCUMENT_URL` | The discovered document link |
| `LINK_TEXT` | Anchor text of the link |
| `DOCUMENT_TYPE` | `PDF` / `DOC` / `DOCX` |

---

## Example Output

| URL | STATUS | URL_TYPE | DOCUMENT_URL | DOCUMENT_LINK_TYPE | PAGE_COUNT | LANGUAGE |
|-----|--------|----------|--------------|--------------------|------------|----------|
| example.com/file.pdf | WORKING | DIRECT_PDF | example.com/file.pdf | DIRECT | 25 | English |
| example.com/page | WORKING | LANDING_PAGE | example.com/doc.pdf | SINGLE | 12 | French |
| example.com/books | WORKING | LANDING_PAGE | | MULTIPLE | | |
| example.com/about | WORKING | LANDING_PAGE | | NONE | | |
| example.com/broken | NOT_WORKING | NOT_WORKING | | | | |

---

## Troubleshooting

**Auto-detection fails for URL column**
→ Use `--url-column "Exact Column Name"`

**Many BLOCKED results**
→ Some sites block automated requests. Results are correct — the PDF exists but requires a real browser. Enable browser fallback (see below).

**Language shows Unknown**
→ The PDF has no extractable text (scanned/image PDF) or too little text.

**Slow processing**
→ Increase `--workers` (e.g. `--workers 10`). Be careful not to overload servers.

---

## Browser Fallback (Playwright)

For JavaScript-heavy sites where normal requests miss document links:

1. Install Playwright:
   ```bash
   pip install playwright
   playwright install chromium
   ```

2. Enable in `config.yaml`:
   ```yaml
   browser:
     enabled: true
   ```

The tool will use Playwright only for pages where static HTML analysis finds no document links.

---

## Limitations

- Scanned PDFs (image-only) → page count works, language = Unknown
- Password-protected PDFs → page count may fail
- Sites requiring login/CAPTCHA → will appear BLOCKED or NOT_WORKING
- Very large PDFs are capped at 50 MB download by default
- Language detection requires at least 50 characters of extracted text

---

## Project Structure

```
url-document-analyzer/
├── config.yaml              ← Runtime configuration
├── requirements.txt
├── pyproject.toml
├── README.md
├── src/url_analyzer/
│   ├── __init__.py
│   ├── main.py              ← CLI + orchestration
│   ├── config.py            ← Config loading
│   ├── models.py            ← Data models
│   ├── logger.py            ← Logging setup
│   ├── http_client.py       ← HTTP with retry/backoff
│   ├── url_checker.py       ← URL accessibility
│   ├── html_analyzer.py     ← Find document links in HTML
│   ├── document_detector.py ← Main pipeline logic
│   ├── pdf_analyzer.py      ← PDF metadata extraction
│   ├── language_detector.py ← Language detection
│   ├── excel_handler.py     ← Excel read/write
│   └── utils.py             ← URL normalization, helpers
├── tests/                   ← Unit tests (pytest)
├── input/                   ← Put your Excel files here
├── output/                  ← Generated results go here
└── logs/                    ← analyzer.log
```

## PDF Delivery Tools (web app)

```bash
.
un_dashboard.bat        # then open http://localhost:8080  (binds to 127.0.0.1; set HOST to change)
```

| Page | What it does |
|---|---|
| **Home** (`/`) | Choose a tool. The sidebar is on every page. |
| **Daily Backlog Dashboard** (`/backlog`) | Upload the Reg Transform UI CSV + Power BI Excel, explore the backlog, select books, download the filtered CSV or the 7-tab Excel report. |
| **URL Analysis** (`/url-analysis`) | One tool, three inputs: **Paste URLs**, **Excel Batch Upload**, **From Daily Backlog**. |
| **Base Templates** (`/base-templates`) | The team's *Base Template* sheet: search / filter / sort, add, edit, delete, import from and export to Excel. Stored in `data/base_templates.json` (git-ignored). Import "add only new rows" is repeatable, so rows added to the SharePoint sheet can be pulled in. |
| **Base Template Analysis** (`/base-template-analysis`) | Upload a URL analysis file (Document ID, Domain, Spider Template, URL) and compare it with the Base Templates, joined on domain. Flags missing URLs, missing Spider Templates, missing domains and domains with no Base Template; Excel report with Review / Needs Fix / Domains / Base Templates Review / SUMMARY sheets. The URL Analysis page has a **Send to Base Template Analysis** button (selected rows, or all) that hands its results over, already compared. |

### One URL analysis engine

```
 Paste URLs ──┐
 Excel upload ┼─► analyze_urls(records) ─► URLAnalysisResult ─► one dashboard ─► export_analysis_results()
 Backlog      ┤        (analysis/engine.py)
 CLI (--input)┘
```

Paste URLs, Excel Batch Upload and From Daily Backlog are **the three input tabs of the one URL Analysis tool**
(not separate tools). Whatever the input, every URL gets the same analysis:

1. **Does it work?** `Status` = `WORKING` / `NOT_WORKING` (404, 410, 5xx) / `BLOCKED` (401, 403, 429) /
   `TIMEOUT` / `ERROR` (DNS, connection, SSL, invalid URL). Redirects are followed and the final URL analysed.
2. **`URL Type`**: `DIRECT_PDF` / `DIRECT_DOCUMENT` (by Content-Type or file signature, not just `.pdf` in the URL),
   `LANDING_PAGE`, `NOT_WORKING`, `UNKNOWN`.
3. **Landing pages**: links in `<a>`, `<iframe>`, `<embed>`, `<object>` are normalised and de-duplicated. Links
   without a document extension (e.g. `/download?id=7`) count only if their real Content-Type is a document -
   anchor text alone never does. Then `Document Link Type`:
   `NONE`, `SINGLE` (the one document is followed and its page count / language / last modified / size are
   reported, with `Document URL`) or `MULTIPLE` (count + all URLs on the *Document Links* sheet; none is chosen).
4. **Document details**: page count, language (from extracted text; `Unknown` if too little text),
   `Text Extraction Status` (`NO_TEXT` for scanned PDFs), last modified (HTTP header, else PDF metadata), size.
5. **Politeness**: configurable timeout, retries with exponential back-off, minimum delay per host, a per-host
   concurrency limit, `Retry-After` honoured on HTTP 429. One failed URL never stops the batch.
6. **Optional browser fallback** for JavaScript-generated links: `browser: enabled: true` in `config.yaml`
   (needs `pip install playwright && playwright install chromium`); only used when static HTML shows no links.

Dashboard (identical for every input): summary cards, status pills, status / URL type / document-link bars,
**Overview by domain** (click a row to filter), nine filters, search, sortable paged table with row selection,
**Download Selected**, **Select all filtered**, Re-analyze, and **Download Analysis Results**
(Excel: `URL Analysis`, `Document Links`, `SUMMARY`). The Daily Backlog table has **Download Selected** too.

Everything that checks a URL lives in `src/url_analyzer/analysis/`; there is no other implementation
(a test fails if `requests`/`pypdf` appear anywhere else).

| Module | Role |
|---|---|
| `analysis/engine.py` | `analyze_urls()`, status classification, landing pages, batch runner, cache |
| `analysis/http_analyzer.py` | URL validation (http/https only, private addresses blocked incl. redirects), redirects, SSL fallback, throttle |
| `analysis/document_analyzer.py` + `pdf_analyzer.py` | file-type detection, page count, language, last modified |
| `analysis/browser_fallback.py` | optional Playwright renderer |
| `analysis/inputs.py` | pasted-text parsing, Excel reading with URL-column detection |
| `analysis/export.py` | `export_analysis_results()` - the only Excel exporter for URL analysis |
| `services/url_analysis_service.py` | jobs, progress, re-analysis, export (shared by every input) |
| `ui/url_analysis.py`, `backlog/web.py` | Flask blueprints |

**Excel upload:** the URL column is detected from the header (URL, Source URL, Source Link, Link, Document URL...)
or from the cell contents; if it can't be identified confidently you pick it from a **Select URL Column** list.
Every row is kept; the same URL on several rows is fetched once.

**Caching:** results are reused for an hour across all inputs and are clearly marked *cached* (with the time of
the live check). **Re-analyze Selected / All** forces a fresh check.

**Book Type** follows `Power BI BookSourceId -> UI DocumentId -> UI BookCategory` (key set in
`backlog/config.py::MATCH_KEY_PAIRS`): the Power BI file only provides the link, the Book Type is always the
Reg Transform `BookCategory` of the matched record. A record with no UI match is `Unmatched` (no Book Type is
assigned); a matched record whose `BookCategory` is blank is `Unknown`. Both stay in the data but are not in the
PDF/Other action lists. Duplicate UI identifiers are reported (the first occurrence in the file is used).
The filter values come from the data, so new BookCategory values appear automatically.

**CLI:**

```bash
.
un.bat --input books.xlsx [--url-column "Source Link"] [--fresh]      # URL analysis of an Excel file
.
un.bat daily-workflow --ui "StartPointStatus-01-Oct-2026.csv" --powerbi "Details Table.xlsx"
```
