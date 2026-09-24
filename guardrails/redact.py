"""Redaction for anything that might touch a log or an artifact.

Two layers, deliberately redundant:
  1. Field-name based: if the target's accessible name looks like a
     sensitive field (password, SSN, card number, ...), redact the value
     outright, regardless of content.
  2. Pattern based: scrub common PII/secret shapes (SSNs, card numbers,
     bearer tokens) out of free text even when we don't know the field name
     — e.g. out of an LLM's rationale string or a page's error text.

Note artifacts are parameterized by construction (steps reference
`{param_name}`, never literal values, see artifact/schema.py), so this
module's main job is the *run log*, which does capture concrete values.
"""

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
