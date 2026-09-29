"""The capability artifact schema.

An artifact is a versioned, typed, reviewable description of a flow that an
AI agent can invoke as a capability. It is produced once by a successful
LLM-driven discovery run and then replayed deterministically, with no model
in the decision loop.

Design choices (see REPORT.md section 2 for the full rationale):

- Locator targeting is a ranked list of independent strategies
  (LocatorCandidate), not a single selector. Legacy/no-test-ID surfaces mean
  any single strategy can fail; replay tries them in order and records
  which one hit, itself a useful drift signal.
- frame_chain lets a target live inside nested iframes, because "how you
  get to the right document" is a real part of legacy web apps, not just
  "how you find the element".
- inputs/outputs are typed and named independently of the step list, so an
  agent invoking this capability sees a function-like contract, not a
  transcript.
- target carries app_id/tenant_id even though this project only implements
  a single tenant, so the schema does not need to change shape later to
  support the same app across many tenants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"       # ARIA role plus accessible name, most stable, no test IDs needed
    TEXT = "text"                 # exact/substring visible text match
    CSS_PATH = "css_path"         # structural CSS selector path, brittle, last resort before coords
    COORDINATES = "coordinates"   # absolute x/y, only when nothing else resolved


class LocatorCandidate(BaseModel):
    strategy: LocatorStrategy
    value: dict = Field(
        description=(
            "Strategy-specific payload, e.g. "
            '{"role": "button", "name": "Search"} for ROLE_NAME, '
            '{"text": "Open a New Sub-Account"} for TEXT, '
            '{"css": "form input[type=submit]"} for CSS_PATH, '
            '{"x": 120, "y": 240} for COORDINATES.'
        )
    )
    observed_at_record_time: bool = Field(
        default=False,
        description="True if this exact candidate is the one that actually resolved during discovery.",
    )


class Target(BaseModel):
    """What to act on: a ranked locator, optionally nested inside iframes."""

    candidates: list[LocatorCandidate]
    frame_chain: list[list[LocatorCandidate]] = Field(
        default_factory=list,
        description=(
            "Ordered list of frames to descend into before resolving `candidates`, "
            "outermost first. Each entry is itself a ranked candidate list for that "
            "frame's <iframe> element. Empty means the target is in the top-level document."
        ),
    )


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    READ_TEXT = "read_text"
    WAIT_FOR = "wait_for"


class CheckpointKind(str, Enum):
    URL_CONTAINS = "url_contains"
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_NOT_VISIBLE = "element_not_visible"
    TEXT_CONTAINS = "text_contains"


class Checkpoint(BaseModel):
    kind: CheckpointKind
    value: Optional[str] = Field(
        default=None, description="URL substring or text substring, depending on `kind`."
    )
    target: Optional[Target] = Field(
        default=None, description="Required for ELEMENT_VISIBLE / ELEMENT_NOT_VISIBLE."
    )


class Step(BaseModel):
    index: int
    action: ActionType
    description: str = Field(description="Human-readable rationale, carried over from discovery.")
    target: Optional[Target] = Field(default=None, description="Unused for NAVIGATE.")
    value_template: Optional[str] = Field(
        default=None,
        description=(
            "For FILL/SELECT: the text to enter, as a Python str.format template "
            'referencing input params, e.g. "{member_id}". For NAVIGATE: the URL template.'
        ),
    )
    extract_as: Optional[str] = Field(
        default=None,
        description="For READ_TEXT: the output field name this step's extracted text fills.",
    )
    checkpoint: Optional[Checkpoint] = Field(
        default=None, description="Optional inline assertion evaluated right after this step."
    )
    risk: Literal["safe", "confirm", "blocked"] = Field(
        default="safe",
        description="Guardrail classification for this individual action (see guardrails/policy.py).",
    )
    source: Literal["llm", "human_intervention"] = Field(
        default="llm",
        description=(
            "Who discovered this step. 'llm' for a normal tool call the discovery agent made "
            "itself; 'human_intervention' for a step captured while a human had control of the "
            "live session. Both are represented and replayed identically, this is provenance for "
            "review, not a different execution path. A human-taught step still carries the same "
            "ranked Target candidates a discovered step does, built the same way."
        ),
    )


class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step: Optional[int] = Field(
        default=None, description="Index of the Step whose extract_as produced this field, if any."
    )


class TargetApp(BaseModel):
    """Identifies the surface this capability was recorded against.

    app_id names the underlying vendor product/app (stable across tenants);
    tenant_id/base_url are the specific deployment this recording came from.
    Kept separate on purpose, see REPORT.md section 4 on multi-tenant reuse.
    """

    app_id: str
    tenant_id: Optional[str] = None
    base_url: str
    entry_path: str


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class BusinessOutcomeSignature(BaseModel):
    """A legitimate, non-error result the replay can detect and report as
    data, not as a crash. Checked after every step; the first match
    short-circuits the remaining steps and becomes the replay result's
    business_outcome.
    """

    name: str = Field(description="e.g. 'member_not_found', 'account_locked'.")
    detect: Checkpoint
    description: str


class RecoverableCondition(BaseModel):
    """A known transient/interstitial state: detect it, take one bounded
    recovery action, then re-check the current step's own target instead of
    failing. Not a retry loop, one recovery attempt per occurrence.
    """

    name: str
    detect: Checkpoint
    recovery_action: ActionType
    recovery_target: Optional[Target] = None
    recovery_value: Optional[str] = None
    description: str


class CommitVerification(BaseModel):
    """Reviewer-declared: how to check, after a step fails at or beyond this
    artifact's own risky confirm step, whether the underlying action
    actually committed server-side before the failure (e.g. the backend
    applied a transfer but the confirmation page never rendered). A
    discovery run only demonstrates the happy path, so like
    business_outcomes and recoverable_conditions, this is domain knowledge
    a reviewer supplies, never something discovery infers on its own.

    Deliberately just a navigate plus a Checkpoint, reusing the same
    primitives as everything else in this schema, not a new resolution
    framework. Deliberately fallible-safe: if this check itself cannot
    complete, replay must treat that as genuinely unknown, never silently
    fold it into either a success or a clean failure.
    """

    navigate_template: str = Field(
        description="URL template (may reference this capability's input params) to check for evidence the action committed, e.g. a transaction-history page."
    )
    detect: Checkpoint = Field(
        description="Checked on the page reached by navigate_template. If it holds, the action is judged to have committed."
    )
    description: str


class Artifact(BaseModel):
    capability_id: str = Field(description="Stable slug, e.g. 'open-member-subaccount'.")
    version: str = Field(default="1.0.0", description="Semver. Bump on any step/schema change.")
    status: ArtifactStatus = Field(default=ArtifactStatus.DRAFT)

    description: str
    goal_template: str = Field(
        description="The natural-language goal this was discovered from, kept for traceability."
    )
    target_app: TargetApp

    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    final_checkpoint: Checkpoint

    business_outcomes: list[BusinessOutcomeSignature] = Field(default_factory=list)
    recoverable_conditions: list[RecoverableCondition] = Field(default_factory=list)
    commit_verification: Optional[CommitVerification] = Field(
        default=None,
        description="Optional reviewer-declared ambiguous-commit check (see CommitVerification). None means a post-confirm failure is reported as an ordinary hard_failure, exactly as before this field existed.",
    )

    overall_risk: Literal["safe", "confirm", "blocked"] = "safe"
    created_from_run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
