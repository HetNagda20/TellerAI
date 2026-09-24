from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action, classify_click
from guardrails.redact import field_is_sensitive, redact_text, redact_value


def test_classify_click_flags_irreversible_verbs():
    assert classify_click("Confirm & Open Account") == "confirm"
    assert classify_click("Confirm Transfer") == "confirm"
    assert classify_click("Delete Member") == "confirm"
    assert classify_click("Search") == "safe"
    assert classify_click("Back to Member Lookup") == "safe"


def test_classify_click_does_not_flag_a_review_step_that_merely_mentions_the_domain_verb():
    # Regression: "Review Transfer" (a safe, reversible step before "Confirm Transfer")
    # was previously flagged as risky by a bare `\btransfer\b` pattern — found during
    # a real discovery run, where it produced an unplanned confirmation prompt.
    assert classify_click("Review Transfer") == "safe"
    assert classify_click("Review") == "safe"


def test_classify_action_blocks_disallowed_navigation():
    assert classify_action("navigate", None, url_allowed=False) == "blocked"
    assert classify_action("navigate", None, url_allowed=True) == "safe"


def test_allowlist_domain_and_path():
    al = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/member"], allowed_actions=["click"])
    assert al.url_allowed("http://127.0.0.1:8000/member/1001")
    assert not al.url_allowed("http://evil.example.com/member/1001")
    assert not al.url_allowed("http://127.0.0.1:8000/admin")
    assert al.action_allowed("click")
    assert not al.action_allowed("delete")


def test_redact_value_by_field_name():
    assert redact_value("Password", "hunter2") == "[REDACTED]"
    assert redact_value("SSN", "123-45-6789") == "[REDACTED]"
    assert redact_value("Member ID", "10001") == "10001"


def test_redact_text_pattern_based():
    assert "[REDACTED]" in redact_text("SSN on file: 123-45-6789")
    assert redact_text("Member ID 10001") == "Member ID 10001"


def test_field_is_sensitive():
    assert field_is_sensitive("Card Number")
    assert not field_is_sensitive("Initial Deposit")
