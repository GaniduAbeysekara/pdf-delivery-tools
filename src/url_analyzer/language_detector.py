"""Language detection from extracted text."""
from __future__ import annotations

from .logger import log

MIN_TEXT_LENGTH = 50   # characters; below this → Unknown

# Map langdetect codes to human-readable names
LANG_MAP = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ar": "Arabic",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "it": "Italian",
    "nl": "Dutch",
    "ru": "Russian",
    "pl": "Polish",
    "tr": "Turkish",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "id": "Indonesian",
    "ms": "Malay",
    "th": "Thai",
    "vi": "Vietnamese",
    "hi": "Hindi",
    "bn": "Bengali",
    "ur": "Urdu",
    "fa": "Persian",
    "he": "Hebrew",
    "el": "Greek",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "uk": "Ukrainian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "sl": "Slovenian",
    "ca": "Catalan",
    "mk": "Macedonian",
    "sq": "Albanian",
}


def detect_language(text: str) -> str:
    """
    Detect the primary language of the given text.
    Returns human-readable language name or 'Unknown'.
    """
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        return "Unknown"

    try:
        from langdetect import detect, LangDetectException
        code = detect(text)
        return LANG_MAP.get(code, code.capitalize())
    except ImportError:
        log.warning("langdetect not installed; language detection unavailable")
        return "Unknown"
    except Exception as e:
        log.debug("Language detection failed: %s", e)
        return "Unknown"
