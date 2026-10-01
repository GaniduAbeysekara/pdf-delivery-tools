"""Input validation and loading."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..logger import log


class BacklogError(Exception):
    """An error whose message is safe to show to end users."""


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the actual column matching any candidate (case/whitespace-insensitive)."""
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        hit = lookup.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def validate_path(path: str, label: str, extensions: tuple[str, ...]) -> Path:
    p = Path(path) if path else None
    if not p or not p.is_file():
        raise BacklogError(f"{label} file was not found. Please select it again.")
    if p.suffix.lower() not in extensions:
        raise BacklogError(
            f"{label} must be a {' / '.join(extensions)} file (got '{p.suffix or 'no extension'}')."
        )
    return p


def read_ui_csv(path: str) -> tuple[pd.DataFrame, int, list[str]]:
    """Read the Reg Transform CSV. Returns (df, skipped_malformed_rows, warnings)."""
    skipped = 0

    def _skip(_line):
        nonlocal skipped
        skipped += 1
        return None

    attempts = [("utf-8-sig", "strict"), ("cp1252", "strict"), ("utf-8", "replace")]
    for enc, errors in attempts:
        skipped = 0
        try:
            df = pd.read_csv(path, encoding=enc, encoding_errors=errors, engine="python",
                             on_bad_lines=_skip, dtype=object)
        except UnicodeDecodeError:
            continue
        except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as e:
            raise BacklogError(f"The Reg Transform CSV could not be read: {e}") from e
        warnings = []
        if errors == "replace":
            warnings.append("The CSV contains mixed text encodings; some characters were replaced.")
        if skipped:
            warnings.append(f"{skipped} malformed CSV row(s) were skipped.")
        log.info("Loaded %d Reg Transform records (%d malformed skipped, encoding=%s)",
                 len(df), skipped, enc)
        if df.empty:
            raise BacklogError("The Reg Transform CSV contains no rows.")
        return df, skipped, warnings
    raise BacklogError("The Reg Transform CSV could not be decoded.")


def read_powerbi(path: str) -> pd.DataFrame:
    try:
        with pd.ExcelFile(path) as xls:      # closing matters: an open handle blocks deleting the file on Windows
            sheet = "Export" if "Export" in xls.sheet_names else xls.sheet_names[0]
            df = xls.parse(sheet)
    except Exception as e:  # corrupt / wrong format
        raise BacklogError(f"The Power BI Excel file could not be read: {e}") from e
    log.info("Loaded %d Power BI records (sheet '%s')", len(df), sheet)
    if df.empty:
        raise BacklogError("The Power BI Excel file contains no rows.")
    return df


def require_column(df: pd.DataFrame, candidates: list[str], source: str, purpose: str) -> str:
    col = find_col(df, candidates)
    if col is None:
        raise BacklogError(
            f"Required column '{candidates[0]}' ({purpose}) was not found in the {source}. "
            f"Columns found: {', '.join(map(str, list(df.columns)[:12]))}"
            f"{'…' if len(df.columns) > 12 else ''}"
        )
    return col
