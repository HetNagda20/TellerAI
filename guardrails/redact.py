"""Redaction for logs and artifacts. Values are redacted by field name (password, SSN, card) and by
pattern (SSNs, card numbers, tokens) in free text."""

from __future__ import annotations

import re

_SENSITIVE_NAME_PATTERNS = [
    "password", "passwd", "ssn", "social security", "pin", "cvv",
    "card number", "card num", "account number", "routing number", "secret", "token", "api key",
]

_PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # SSN
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),         # card-number-shaped digit runs
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b"),  # JWT-shaped
    re.compile(r"(?<![\d-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\d-])"),  # US phone number
    re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),  # email address
    re.compile(  # street address: number, up to a few words, then a street suffix
        r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,3}"
        r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Pkwy|Parkway|Hwy|Highway)\b\.?",
        re.IGNORECASE,
    ),
]

REDACTED = "[REDACTED]"


def field_is_sensitive(field_name: str) -> bool:
    lowered = (field_name or "").lower()
    return any(p in lowered for p in _SENSITIVE_NAME_PATTERNS)


def redact_value(field_name: str, value: str) -> str:
    if field_is_sensitive(field_name):
        return REDACTED
    return redact_text(value)


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern in _PII_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def redact_obj(obj):
    """Redacts every string inside nested dicts and lists. For anything written to evidence or a log,
    where the field names are not known."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
