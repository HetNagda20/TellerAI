"""The capability artifact schema: a versioned, typed, reviewable description of a flow. Discovery
records it once, replay runs it with no model. Targets use ranked locator candidates."""

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
        description="Reserved. The current recorder does not set it (always false); replay reports the strategy that resolved in its strategy_log instead.",
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
    allowed_values: Optional[list[str]] = Field(
        default=None,
        description="The choices of the dropdown this input fills, as the page shows them. None for free-text inputs.",
    )


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step: Optional[int] = Field(
        default=None, description="Index of the Step whose extract_as produced this field, if any."
    )


class TargetApp(BaseModel):
    """The surface a capability was recorded against. app_id is stable across tenants; tenant_id and
    base_url identify the deployment."""

    app_id: str
    tenant_id: Optional[str] = None
    base_url: str
    entry_path: str


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class BusinessOutcomeSignature(BaseModel):
    """A legitimate non-error result that replay reports as data, not a crash. The first match ends
    the run as the business_outcome."""

    name: str = Field(description="e.g. 'member_not_found', 'account_locked'.")
    detect: Checkpoint
    description: str


class RecoveryAction(BaseModel):
    """One action of a multi-action recovery, such as signing back in. `value` may contain {env:NAME}, which replay
    fills from its environment when it runs. Secrets therefore never appear in an artifact, in a log, or in evidence."""

    action: ActionType
    target: Optional[Target] = None
    value: Optional[str] = None


class RecoverableCondition(BaseModel):
    """A known transient/interstitial state: detect it, take the declared recovery
    (one action, or a short sequence), then re-check the current step's own target
    instead of failing. Not a retry loop, one recovery attempt per occurrence.
    """

    name: str
    detect: Checkpoint
    recovery_action: Optional[ActionType] = None
    recovery_target: Optional[Target] = None
    recovery_value: Optional[str] = None
    recovery_sequence: list[RecoveryAction] = Field(
        default_factory=list, description="If present, run these in order instead of the single action above."
    )
    restart_after_recovery: bool = Field(
        default=False,
        description=(
            "True when the recovery loses the page the run was on (signing back in lands on a fresh page, "
            "and a half-filled form is gone). Replay then re-runs the recorded steps from the start, once, as long as "
            "no risky step has run; if one has, whether it committed is checked first."
        ),
    )
    description: str


class CommitVerification(BaseModel):
    """Reviewer-declared check for whether an action committed before a failure: a navigate plus a
    checkpoint. If the check itself can't finish, the answer is unknown."""

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
    step_timeout_ms: Optional[int] = Field(
        default=None, description="How long replay waits for each step's target to appear and act. None uses the engine default; slow legacy apps raise it."
    )
    checkpoint_timeout_ms: Optional[int] = Field(
        default=None, description="How long replay polls for the final (and any inline) checkpoint before failing. None uses the engine default."
    )

    business_outcomes: list[BusinessOutcomeSignature] = Field(default_factory=list)
    recoverable_conditions: list[RecoverableCondition] = Field(default_factory=list)
    commit_verification: Optional[CommitVerification] = Field(
        default=None,
        description="Optional reviewer-declared ambiguous-commit check (see CommitVerification). None means a post-confirm failure is reported as an ordinary hard_failure, exactly as before this field existed.",
    )

    overall_risk: Literal["safe", "confirm", "blocked"] = "safe"
    created_from_run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
