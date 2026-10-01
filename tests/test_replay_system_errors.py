"""System errors on replay, on the real saved capabilities: the app or procedure misbehaving. Always
a loud structured failure, no LLM, no guessing, and no unaccounted money."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import replay.executor as replay_executor
from artifact.schema import ActionType, Checkpoint, CheckpointKind, LocatorCandidate, LocatorStrategy, Step, Target
from guardrails.allowlist import Allowlist
from handoff.session import InterventionRequest
from replay.executor import replay_artifact
from mock_app import data as mock_data
from tests.conftest import ledger, load_capability, member

TRANSFER = {"from_member_id": "10001", "from_account_type": "checking", "to_member_id": "20001", "to_account_type": "checking", "amount": "5"}
SUBACCOUNT = {"member_id": "20001", "account_type": "Savings", "initial_deposit": "50"}  # 20001 hits the session interstitial

_INERT_ALLOWLIST = Allowlist(allowed_domains=["127.0.0.1"], allowed_path_prefixes=["/"], allowed_actions=["click", "fill", "select", "navigate", "read_text"])


def _css(css: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})])


def _arm(base_url: str, scenario: str) -> None:
    """Arms the mock app's one-shot fault for the next confirm POST: a 503 after the write commits,
    or a 503 before anything is written."""
    urllib.request.urlopen(
        urllib.request.Request(f"{base_url}/__test__/arm_failure", data=f"scenario={scenario}".encode(), method="POST"),
        timeout=2,
    )


def _arm_post_commit_failure(base_url: str) -> None:
    _arm(base_url, "post_commit_response_failure")


def _with_step_target(artifact, index: int, target: Target):
    steps = [s.model_copy(update={"target": target}) if s.index == index else s for s in artifact.steps]
    return artifact.model_copy(update={"steps": steps})


class _Operator:
    """A scripted human. `fix` runs against the live replay page when control is taken."""

    def __init__(self, page, resume: bool, fix=None):
        self.page, self.resume, self.fix = page, resume, fix
        self.requests: list[InterventionRequest] = []

    def confirm(self, request):
        return True, "n/a"

    def take_control(self, request):
        self.requests.append(request)
        if self.fix and self.resume:
            self.fix(self.page)
        return ("fixed it" if self.resume else "gave up"), self.resume, False, []


# -- transient runtime failure: bounded retry -----------------------------------------


def test_transient_timeout_retries_once_then_fails_structured(monkeypatch):
    monkeypatch.setattr(replay_executor, "_ACTION_TIMEOUT_MS", 400)
    monkeypatch.setattr(replay_executor, "_TRANSIENT_RETRY_WAIT_MS", 50)
    step = Step(index=0, action=ActionType.CLICK, description="click", target=_css("#btn"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        # visible only after attempt 1 has timed out: the retry must catch it
        page.set_content(
            '<button id="btn" style="display:none">Hi</button>'
            '<script>setTimeout(() => { document.getElementById("btn").style.display = "block"; }, 420);</script>'
        )
        recovered = replay_executor._run_step(page, step, {}, _INERT_ALLOWLIST, [], None)
        # never visible: the retry is bounded, then a structured failure
        page.set_content('<button id="btn" style="display:none">Hi</button>')
        exhausted = replay_executor._run_step(page, step, {}, _INERT_ALLOWLIST, [], None)
        browser.close()

    assert recovered.ok is True
    assert exhausted.ok is False and "Exhausted" in exhausted.message and "timeout" in exhausted.message.lower()


# -- an expired session: sign back in, and never move money twice ------------------------------------


def test_an_expired_session_is_signed_back_in_and_money_never_moves_twice(base_url, monkeypatch):
    monkeypatch.setenv("CONSOLE_USERNAME", "teller")
    monkeypatch.setenv("CONSOLE_PASSWORD", "teller-demo-pass")
    artifact = load_capability("transfer-funds", "1.0.0", base_url)

    # 1. it expires before the risky step: sign in, re-run the recorded steps from the start, one transfer
    _arm(base_url, "session_expires_at_review")
    first = replay_artifact(artifact, TRANSFER, headless=True)
    assert first.kind == "success" and first.session_reauths == 1 and first.commit_attempts == 1
    assert len(ledger("10001")) == 1

    # 2. it expires on the confirm click, so the write never happened: the check signs in, finds nothing, retries once
    _arm(base_url, "session_expires_at_confirm")
    second = replay_artifact(artifact, TRANSFER, headless=True)
    assert second.kind == "success" and second.commit_attempts == 2 and second.session_reauths >= 1
    assert len(ledger("10001")) == 2

    # 3. it expires after the write went through: the check signs in, finds this attempt's id, and does NOT retry
    _arm(base_url, "session_expires_after_confirm")
    third = replay_artifact(artifact, TRANSFER, headless=True)
    assert third.kind == "success" and third.recovered_via_commit_verification and third.commit_attempts == 1
    assert len(ledger("10001")) == 3

    # 4. no credentials in the environment: a clear failure, nothing moves, and the secret is nowhere in the evidence
    monkeypatch.delenv("CONSOLE_USERNAME")
    _arm(base_url, "session_expires_at_review")
    failed = replay_artifact(artifact, TRANSFER, headless=True)
    assert failed.kind == "hard_failure" and "CONSOLE_USERNAME is not set" in failed.failure.message
    assert len(ledger("10001")) == 3
    written = "".join(p.read_text() for r in (first, failed) for p in Path(r.evidence_dir).glob("*.json*"))
    assert "teller-demo-pass" not in written


# -- slow apps: wait for the state, don't assume it is there ------------------------------------


def _checkpoint(text: str) -> Checkpoint:
    return Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value=text)


def test_replay_waits_for_a_slow_app_and_for_content_that_arrives_without_a_url_change(base_url):
    late_text = (
        '<div id="out"></div><iframe id="f" srcdoc=""></iframe>'
        '<script>setTimeout(() => { document.getElementById("out").textContent = "Saved OK"; }, 700);'
        'setTimeout(() => { document.getElementById("f").srcdoc = "<p>Posted in frame</p>"; }, 900);</script>'
    )
    late_button = '<script>setTimeout(() => { document.body.innerHTML = "<button id=go>Go</button>"; }, 1200);</script>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # the success text appears on the same page, later, in the main document or in a frame
        page.set_content(late_text)
        assert replay_executor._poll_checkpoint(page, _checkpoint("Saved OK"), {}, 0) is False  # one look only
        assert replay_executor._poll_checkpoint(page, _checkpoint("Saved OK"), {}, 3000) is True
        assert replay_executor._poll_checkpoint(page, _checkpoint("Posted in frame"), {}, 3000) is True
        assert replay_executor._poll_checkpoint(page, _checkpoint("Never shown"), {}, 400) is False  # bounded

        # a target drawn late is found within the step's own time, and not if that time is too short
        step = Step(index=0, action=ActionType.CLICK, description="go", target=_css("#go"))
        page.set_content(late_button)
        assert replay_executor._run_step(page, step, {}, _INERT_ALLOWLIST, [], None, timeout_ms=4000).ok is True
        page.set_content(late_button)
        assert replay_executor._run_step(page, step, {}, _INERT_ALLOWLIST, [], None, timeout_ms=300).ok is False
        browser.close()

    # a backend that holds every response for a moment still replays, with the artifact's own timeouts
    urllib.request.urlopen(urllib.request.Request(f"{base_url}/__test__/set_delay", data=b"ms=600", method="POST"), timeout=2)
    slow = load_capability("fetch-account-balance", "1.0.0", base_url).model_copy(update={"step_timeout_ms": 8000, "checkpoint_timeout_ms": 8000})
    ok = replay_artifact(slow, {"member_id": "10001"}, headless=True)
    assert ok.kind == "success" and ok.outputs["checking_balance"] == "$812.44"

    # and a checkpoint that never holds is a structured failure that says how long it waited
    never = slow.model_copy(update={"final_checkpoint": _checkpoint("Never shown"), "checkpoint_timeout_ms": 900})
    failed = replay_artifact(never, {"member_id": "10001"}, headless=True)
    assert failed.kind == "hard_failure" and failed.failure.action == "final_checkpoint" and "900 ms" in failed.failure.message


# -- artifact/target drift and policy: fail immediately, never retried, never patched -------


def test_unresolvable_target_is_a_structured_failure_before_any_money_moves(base_url):
    """The recorded 'From Account' locator no longer matches the page (UI drift)."""
    artifact = _with_step_target(load_capability("transfer-funds", "1.0.0", base_url), 6, _css("#no-longer-on-the-page"))

    result = replay_artifact(artifact, TRANSFER, headless=True)

    assert result.kind == "hard_failure"
    assert (result.failure.step_index, result.failure.action) == (6, "select")
    assert "css_path" in result.failure.observed  # what was tried
    assert "Exhausted" not in result.failure.message  # took the no-retry path
    # an unrecognized failure says what the page actually showed: status, URL, visible text
    assert result.failure.observed_page.startswith("HTTP 200 ")
    assert "/transactions/new/transfer" in result.failure.observed_page and "Transfer Funds" in result.failure.observed_page
    assert result.recovered_via_commit_verification is False  # failed before the confirm step
    assert ledger("10001") == [] and member("10001")["checking_balance"] == pytest.approx(812.44)


def test_a_navigate_outside_the_allowlist_is_blocked(base_url):
    artifact = load_capability("transfer-funds", "1.0.0", base_url)
    steps = [s.model_copy(update={"value_template": "http://evil.example.com/"}) if s.index == 0 else s for s in artifact.steps]

    result = replay_artifact(artifact.model_copy(update={"steps": steps}), TRANSFER, headless=True)

    assert result.kind == "hard_failure"
    assert result.failure.step_index == 0 and "allowlist" in result.failure.message.lower()
    assert result.steps_executed == 1  # the blocked step is the only one attempted


# -- recoverable conditions, and operational recovery by a human ------------------------------


def test_a_known_interstitial_is_dismissed_automatically_and_an_unknown_one_earns_a_human_one_retry(base_url):
    """A declared condition is dismissed by replay. Undeclared, it is a failure, or with escalation
    a human's fix earns one retry. A human who declines ends the run."""
    declared = replay_artifact(load_capability("open-member-subaccount", "1.0.0", base_url), SUBACCOUNT, headless=True)
    assert declared.kind == "success"
    assert [e.recovered_condition for e in declared.strategy_log if e.recovered_condition] == ["session_renewal_interstitial"]

    artifact = load_capability("open-member-subaccount", "1.0.0", base_url).model_copy(update={"recoverable_conditions": []})
    before = artifact.model_dump_json()

    plain = replay_artifact(artifact, SUBACCOUNT, headless=True)
    assert plain.kind == "hard_failure" and plain.failure.step_index == 5  # the account-type select never appears

    operators: list[_Operator] = []

    def fixes(page):
        page.get_by_role("button", name="Continue").click()
        page.wait_for_load_state("domcontentloaded", timeout=5000)

    def make(resume):
        def factory(page):
            op = _Operator(page, resume=resume, fix=fixes)
            operators.append(op)
            return op

        return factory

    recovered = replay_artifact(artifact, SUBACCOUNT, headless=True, escalate_on_failure=True, operator_factory=make(True))
    declined = replay_artifact(artifact, SUBACCOUNT, headless=True, escalate_on_failure=True, operator_factory=make(False))

    assert recovered.kind == "success"
    assert [r.reason for r in operators[0].requests] == ["replay_hard_failure"]  # asked once
    # the same step ran twice: the failed first attempt (logged via the last-resort
    # coordinates candidate) and exactly one retry, which resolved normally
    assert [e.strategy_used for e in recovered.strategy_log if e.step_index == 5] == ["coordinates", "css_path"]
    assert declined.kind == "hard_failure" and declined.failure.step_index == 5
    assert len(operators[1].requests) == 1
    assert artifact.model_dump_json() == before  # operational recovery never edits the artifact


# -- ambiguous commit: did the write register? The app's own record of this attempt's run id answers ----


def test_post_commit_failure_is_recovered_through_the_run_id_for_every_write_capability(base_url, monkeypatch):
    """The write commits, then a 503. The app shows this attempt's run id, so replay finds it and
    recovers, not retries. Covers transfer, sub-account and loan."""
    cases = {
        "transfer-funds": TRANSFER,
        "open-member-subaccount": {"member_id": "10001", "account_type": "Savings", "initial_deposit": "50"},
        "create-auto-loan-for": {"member_id": "10001", "loan_amount": "8000", "loan_purpose": "Personal", "interest_rate": "5.5"},
    }
    for capability, params in cases.items():
        _arm_post_commit_failure(base_url)
        result = replay_artifact(load_capability(capability, "1.0.0", base_url), params, headless=True)
        assert result.kind == "success" and result.recovered_via_commit_verification is True, capability
        assert result.commit_attempts == 1 and "confirmation_number" not in result.outputs, capability  # never invent an output

    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 5)  # exactly one transfer
    assert [r["run_id"][-3:] for r in mock_data.SUBACCOUNTS["10001"]] == ["-a1"]  # one sub-account, stamped with attempt 1
    assert [l["run_id"][-3:] for l in mock_data.LOANS["10001"]] == ["-a1"]

    # The history page lags the write. The first look finds nothing, the second finds it. Recover,
    # never retry: a retry would be a second transfer.
    real, looks = replay_executor._verify_commit, []

    def a_beat_behind(page, verification, params):
        looks.append(params["run_id"])
        return False if len(looks) == 1 else real(page, verification, params)

    monkeypatch.setattr(replay_executor, "_verify_commit", a_beat_behind)
    monkeypatch.setattr(replay_executor, "_COMMIT_SETTLE_MS", 50)
    _arm_post_commit_failure(base_url)
    behind = replay_artifact(load_capability("transfer-funds", "1.0.0", base_url), TRANSFER, headless=True)
    assert behind.kind == "success" and behind.recovered_via_commit_verification is True and behind.commit_attempts == 1
    assert len(looks) == 2 and looks[0] == looks[1]  # the same attempt's id, looked at twice
    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 10)  # two transfers in this test, one per run


def test_a_verified_non_commit_is_retried_once_and_lands_exactly_once_and_a_second_failure_reaches_a_human(base_url, monkeypatch):
    monkeypatch.setattr(replay_executor, "_COMMIT_SETTLE_MS", 50)
    artifact = load_capability("transfer-funds", "1.0.0", base_url)

    # The first confirm answers 503 before writing. The check finds nothing twice, so the whole
    # transaction reruns and the ledger ends with one transfer.
    _arm(base_url, "pre_commit_response_failure")
    retried = replay_artifact(artifact, TRANSFER, headless=True)

    assert retried.kind == "success" and retried.commit_attempts == 2 and retried.recovered_via_commit_verification is False
    assert retried.unattended_risky_steps == [11, 11]  # the risky step ran on each attempt, and is flagged each time
    assert retried.outputs["confirmation_number"].startswith("TXF-")
    rows = ledger("10001") + ledger("20001")
    assert len(rows) == 2 and {r["run_id"] for r in rows} == {f"{retried.run_id}-a2"}
    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 5)

    # The confirm click never fires on either attempt. Retry once, then ask a human once after both
    # attempts. Never a third try.
    broken = _with_step_target(artifact, 11, _css("#no-such-confirm-button"))
    unattended = replay_artifact(broken, TRANSFER, headless=True)
    asked = replay_artifact(broken, TRANSFER, headless=True, escalate_on_failure=True, operator_factory=lambda page: _Operator(page, resume=False))

    assert unattended.kind == "hard_failure" and unattended.commit_attempts == 2 and "retried once" in unattended.failure.message
    assert asked.kind == "hard_failure" and asked.commit_attempts == 2
    assert [e.reason for e in asked.escalations] == ["commit_retry_exhausted"]
    assert len(ledger("10001") + ledger("20001")) == 2  # neither failed run wrote anything


def test_a_lagging_history_cannot_make_a_retry_report_a_double_transfer_as_success(base_url, monkeypatch):
    """The first attempt registered but the history lagged, so a retry made a second transfer.
    Replay must see the first run id and not call that success."""
    real = replay_executor._verify_commit_settled
    calls: list[str] = []

    def lagging(page, verification, params):
        calls.append(params["run_id"])
        return False if len(calls) == 1 else real(page, verification, params)  # attempt 1's check is wrong, once

    monkeypatch.setattr(replay_executor, "_verify_commit_settled", lagging)
    monkeypatch.setattr(replay_executor, "_COMMIT_SETTLE_MS", 50)
    _arm_post_commit_failure(base_url)  # attempt 1 commits, but the caller sees an error

    result = replay_artifact(
        load_capability("transfer-funds", "1.0.0", base_url), TRANSFER, headless=True,
        escalate_on_failure=True, operator_factory=lambda page: _Operator(page, resume=False),
    )

    assert result.kind == "hard_failure" and result.commit_attempts == 2
    assert result.failure.action == "commit_verification" and "duplicate" in result.failure.message.lower()
    assert [e.reason for e in result.escalations] == ["commit_duplicate_suspected"]
    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 10)  # the damage the detector exists to surface


def test_when_the_commit_check_itself_cannot_run_replay_never_guesses(base_url):
    """No approver: a loud do-not-retry failure. With one: the human's answer decides,
    and is recorded. In every case the write is never re-attempted."""
    artifact = load_capability("transfer-funds", "1.0.0", base_url)
    unreachable = artifact.commit_verification.model_copy(update={"navigate_template": "http://127.0.0.1:1/"})
    artifact = artifact.model_copy(update={"commit_verification": unreachable})

    _arm_post_commit_failure(base_url)
    unattended = replay_artifact(artifact, TRANSFER, headless=True)

    def run_with_human(resume: bool):
        _arm_post_commit_failure(base_url)
        return replay_artifact(
            artifact, TRANSFER, headless=True, escalate_on_failure=True, operator_factory=lambda page: _Operator(page, resume=resume)
        )

    says_committed = run_with_human(True)
    says_not_committed = run_with_human(False)

    assert unattended.kind == "hard_failure" and "do not retry" in unattended.failure.message.lower()
    assert unattended.commit_attempts == says_committed.commit_attempts == says_not_committed.commit_attempts == 1  # unknown is never "not committed"
    assert says_committed.kind == "success" and says_committed.recovered_via_commit_verification is True
    assert says_not_committed.kind == "hard_failure" and "human confirmed" in says_not_committed.failure.message.lower()
    assert member("10001")["checking_balance"] == pytest.approx(812.44 - 15)  # three commits happened, none repeated
