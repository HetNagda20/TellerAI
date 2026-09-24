"""The replay result contract: the three-way split the brief asks for.

- success: the artifact's flow completed and the final checkpoint held.
- business_outcome: a declared, legitimate non-error result (e.g. "no such
  member") — the caller needs this, it is not a crash.
- hard_failure: something the artifact didn't anticipate; stop and report
  exactly what step, what was expected, and what was actually observed.

Recoverable conditions never appear in the final result on their own — they
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


class ReplayResult(BaseModel):
    kind: Literal["success", "business_outcome", "hard_failure"]
    capability_id: str
    version: str
    run_id: str

    outputs: dict[str, Any] = {}
    business_outcome_name: Optional[str] = None
    business_outcome_description: Optional[str] = None
    failure: Optional[FailureDetail] = None

    steps_executed: int = 0
    strategy_log: list[StrategyLogEntry] = []
    evidence_dir: str = ""
