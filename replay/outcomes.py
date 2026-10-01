"""The replay result contract: success, business_outcome (a declared legitimate result like 'no such
member'), or hard_failure (unanticipated, with step, expected and observed)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class FailureDetail(BaseModel):
    step_index: int
    action: str
    expected: str
    observed: str
    message: str
    observed_page: str = ""
    """What the page actually showed when the step failed: the last document response's HTTP
    status, the URL, and the first part of the visible text (redacted). "Element not found" alone
    hides the difference between a drifted UI and an app error page; this shows which it was."""


class StrategyLogEntry(BaseModel):
    step_index: int
    action: str
    strategy_used: Optional[str] = None
    recovered_condition: Optional[str] = None


class EscalationRecord(BaseModel):
    """Summary of one human escalation during a replay: reason, what they said, what they decided.
    Full detail stays in handoff_log.jsonl."""

    reason: str
    outcome: str
    human_note: str
    resume: bool


class ReplayResult(BaseModel):
    kind: Literal["success", "business_outcome", "hard_failure"]
    capability_id: str
    version: str
    run_id: str

    outputs: dict[str, Any] = {}
    business_outcome_name: Optional[str] = None
    business_outcome_description: Optional[str] = None
    business_outcome_source: Optional[Literal["capability", "app"]] = None
    """Whether the matched outcome was declared by the capability itself or came from the
    application's shared catalog (artifact/annotations.py), so a reader can tell."""
    failure: Optional[FailureDetail] = None
    recovered_via_commit_verification: bool = False
    """True only when a step failed at or after the risky confirm step, the
    artifact declared a CommitVerification, and that check (or a human
    escalated to when the check itself was inconclusive) found evidence the
    action actually committed. kind is still "success" in this case, the
    goal was achieved, but declared outputs from steps after the failure
    point (e.g. a confirmation number) are genuinely missing, not invented,
    so a caller that cares about that distinction should check this flag."""

    commit_attempts: int = 1
    """How many times the whole transaction was executed. 2 means the first attempt failed at or after its
    risky step AND the run-id check verified nothing had been registered, so replay re-ran the same recorded
    steps once. Never more than 2, and never after an unverified or inconclusive result."""
    session_reauths: int = 0
    """How many times replay signed back in after the app's session expired (before a step, or to check a commit).
    The credentials come from the replay process's environment and are never written anywhere."""
    unattended_risky_steps: list[int] = []
    """Indexes of risky/irreversible (risk="confirm") steps that executed with no
    per-run human approval, because the artifact's status is "approved". This
    is the "flag" half of the risky-step policy: approval happened once, at
    review time, so the run itself must still leave a record that it ran one.
    Approvals given live during this run appear in `escalations` instead."""

    steps_executed: int = 0
    strategy_log: list[StrategyLogEntry] = []
    escalations: list[EscalationRecord] = []
    evidence_dir: str = ""
