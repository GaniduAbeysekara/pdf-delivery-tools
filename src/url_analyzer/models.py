"""Small shared data models."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DocumentLink:
    """A document (PDF/DOC/DOCX) link discovered on an HTML page."""
    url: str
    link_text: str = ""
    document_type: str = ""  # PDF, DOC, DOCX
