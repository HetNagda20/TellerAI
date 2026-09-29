"""The replay result contract: the three-way split the brief asks for.

- success: the artifact's flow completed and the final checkpoint held.
- business_outcome: a declared, legitimate non-error result (e.g. "no such
  member"), the caller needs this, it is not a crash.
- hard_failure: something the artifact did not anticipate; stop and report
  exactly what step, what was expected, and what was actually observed.

Recoverable conditions never appear in the final result on their own; they
are handled inline during a step and either fold back into success or, if
recovery itself fails, escalate into hard_failure.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class FailureDetail(BaseModel):
    step_index: int
    action: str
    expected: str
    observed: str
    message: str


class StrategyLogEntry(BaseModel):
    step_index: int
    action: str
    strategy_used: Optional[str] = None
    recovered_condition: Optional[str] = None


class EscalationRecord(BaseModel):
    """A summary of one human escalation that happened during this replay run
    -- reason, what the human said, and what they decided. The full detail
    (screenshot, exact message, timestamps) still lives in the evidence
    directory's handoff_log.jsonl; this exists so that ReplayResult itself
    is self-contained -- a caller reading just the primary result object
    should never have to separately discover and cross-reference a different
    file to find out a human was involved in producing this result at all.
    """

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
    failure: Optional[FailureDetail] = None
    recovered_via_commit_verification: bool = False
    """True only when a step failed at or after the risky confirm step, the
    artifact declared a CommitVerification, and that check (or a human
    escalated to when the check itself was inconclusive) found evidence the
    action actually committed. kind is still "success" in this case, the
    goal was achieved, but declared outputs from steps after the failure
    point (e.g. a confirmation number) are genuinely missing, not invented,
    so a caller that cares about that distinction should check this flag."""

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
