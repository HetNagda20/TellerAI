from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action, classify_click
import logging

from guardrails.redact import field_is_sensitive, redact_obj, redact_text, redact_value
from logging_config import _RedactFilter


def test_risk_classification_and_the_allowlist():
    # irreversible commit buttons need a human; the safe review step before one does not
    assert classify_click("Confirm & Open Account") == "confirm"
    assert classify_click("Confirm Transfer") == "confirm"
    assert classify_click("Delete Member") == "confirm"
    assert classify_click("Search") == "safe"
    assert classify_click("Back to Member Lookup") == "safe"
    # regression: "Review Transfer" was once flagged by a bare \btransfer\b and produced an unplanned prompt
    assert classify_click("Review Transfer") == "safe"
    assert classify_click("Review") == "safe"

    assert classify_action("navigate", None, url_allowed=False) == "blocked"
    assert classify_action("navigate", None, url_allowed=True) == "safe"

    al = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/member"], allowed_actions=["click"])
    assert al.url_allowed("http://127.0.0.1:8000/member/1001")
    assert not al.url_allowed("http://evil.example.com/member/1001")
    assert not al.url_allowed("http://127.0.0.1:8000/admin")
    assert al.action_allowed("click") and not al.action_allowed("delete")


def test_redaction_by_field_name_and_by_pattern():
    assert redact_value("Password", "hunter2") == "[REDACTED]"
    assert redact_value("SSN", "123-45-6789") == "[REDACTED]"
    assert redact_value("Member ID", "10001") == "10001"
    assert "[REDACTED]" in redact_text("SSN on file: 123-45-6789")
    assert redact_text("Member ID 10001") == "Member ID 10001"
    assert field_is_sensitive("Card Number") and not field_is_sensitive("Initial Deposit")

    # contact details are scrubbed by shape too; ordinary banking text is left alone
    for pii in ("312-555-0199", "(312) 555 0199", "a.b+c@example.com", "555 W Washington blvd", "77 Lake Shore Dr"):
        assert redact_text(f"reach me at {pii}") == "reach me at [REDACTED]", pii
    for plain in ("Member ID 10001", "Transfer $100 from 12345 savings", "balance $2300.77 at 2026-09-20 09:14:02.000", "6.25% interest on $12,500"):
        assert redact_text(plain) == plain, plain
    assert redact_obj({"a": ["call 312-555-0199", {"b": "ok"}], "n": 3}) == {"a": ["call [REDACTED]", {"b": "ok"}], "n": 3}

    # every log line passes the filter, so a goal logged anywhere is scrubbed
    record = logging.LogRecord("x", logging.INFO, "f", 1, "goal=%r", ("Update address to 555 W Washington blvd",), None)
    assert _RedactFilter().filter(record) and "555 W Washington" not in record.getMessage()
