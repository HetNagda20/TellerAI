"""Reviewer-declared business outcomes and recoverable conditions. App-wide outcomes are declared
once per app; only workflow-specific ones live on a capability. Stands in for human review."""

from __future__ import annotations

import re
from typing import Optional

from artifact.schema import (
    ActionType,
    BusinessOutcomeSignature,
    Checkpoint,
    CheckpointKind,
    CommitVerification,
    LocatorCandidate,
    LocatorStrategy,
    RecoverableCondition,
    RecoveryAction,
    Target,
)

_CONTINUE_BUTTON = Target(
    candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Continue"})]
)


_MEMBER_NOT_FOUND = BusinessOutcomeSignature(
    name="member_not_found",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="No member found"),
    description="The given member ID does not exist in the core system.",
)

_ACCOUNT_LOCKED = BusinessOutcomeSignature(
    name="account_locked",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Account Locked."),
    description="Member has a compliance hold; write actions are blocked until cleared by a supervisor.",
)

_SESSION_INTERSTITIAL = RecoverableCondition(
    name="session_renewal_interstitial",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Your session is being renewed."),
    recovery_action=ActionType.CLICK,
    recovery_target=_CONTINUE_BUTTON,
    description="A known interstitial that appears for some sessions before a write form loads; dismiss and continue.",
)

_INSUFFICIENT_FUNDS = BusinessOutcomeSignature(
    name="insufficient_funds",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Insufficient funds"),
    description="The funding/source account does not have enough balance to cover the requested amount.",
)

# Session expiry is a property of the application, so it is declared once per app and covers every capability. The
# credentials are {env:...} placeholders filled from the replay process's environment: nothing secret is stored here.
_SESSION_EXPIRED = RecoverableCondition(
    name="session_expired",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Your session has expired."),
    recovery_sequence=[
        RecoveryAction(
            action=ActionType.FILL,
            target=Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=username]"})]),
            value="{env:CONSOLE_USERNAME}",
        ),
        RecoveryAction(
            action=ActionType.FILL,
            target=Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[name=password]"})]),
            value="{env:CONSOLE_PASSWORD}",
        ),
        RecoveryAction(
            action=ActionType.CLICK,
            target=Target(candidates=[
                LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Sign in"}),
                LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "input[type=submit]"}),
            ]),
        ),
    ],
    restart_after_recovery=True,
    description="The session expired and the app asks to sign in again. Sign in, then re-run the recorded steps, since the page the run was on is gone.",
)

# Commit verification: after an ambiguous failure at a risky step, ask the app whether this
# attempt's write registered, by finding its X-Run-Id on the listing page.
_TRANSFER_COMMIT_VERIFICATION = CommitVerification(
    navigate_template="{base_url}/member/{primary_id}/transactions",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="{run_id}"),
    description="Checks the sending member's transaction history for this attempt's run id in the Source column.",
)

_SUBACCOUNT_COMMIT_VERIFICATION = CommitVerification(
    navigate_template="{base_url}/member/{primary_id}",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="{run_id}"),
    description="Checks the member's sub-account list for this attempt's run id in the Source column.",
)

_LOAN_COMMIT_VERIFICATION = CommitVerification(
    navigate_template="{base_url}/member/{primary_id}/loans",
    detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="{run_id}"),
    description="Checks the member's loan list for this attempt's run id in the Source column.",
)

_ANNOTATIONS: dict[str, tuple[list[BusinessOutcomeSignature], list[RecoverableCondition], Optional[CommitVerification]]] = {
    "open-member-subaccount": (
        [
            BusinessOutcomeSignature(
                name="invalid_deposit_amount",
                detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Initial deposit must be a number of at least $25.00."),
                description="The requested initial deposit violates the minimum-deposit business rule.",
            ),
        ],
        [_SESSION_INTERSTITIAL],
        _SUBACCOUNT_COMMIT_VERIFICATION,
    ),
    "create-auto-loan-for": ([], [], _LOAN_COMMIT_VERIFICATION),
    "transfer-funds": (
        [
            BusinessOutcomeSignature(
                name="recipient_not_found",
                detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Recipient not found"),
                description="The destination member ID does not exist in the core system.",
            ),
        ],
        [],
        _TRANSFER_COMMIT_VERIFICATION,
    ),
}


# Business outcomes that belong to the app itself, applied to every capability recorded against it
# (keyed by app_id). Unknown member, compliance hold, insufficient funds.
_APP_BUSINESS_OUTCOMES: dict[str, list[BusinessOutcomeSignature]] = {
    "cu-servicing-console": [_MEMBER_NOT_FOUND, _ACCOUNT_LOCKED, _INSUFFICIENT_FUNDS],
}


_APP_RECOVERABLE_CONDITIONS: dict[str, list[RecoverableCondition]] = {
    "cu-servicing-console": [_SESSION_EXPIRED],
}


def app_recoverable_conditions(app_id: str) -> list[RecoverableCondition]:
    return list(_APP_RECOVERABLE_CONDITIONS.get(app_id, []))


def app_business_outcomes(app_id: str) -> list[BusinessOutcomeSignature]:
    return list(_APP_BUSINESS_OUTCOMES.get(app_id, []))


# Placeholders replay supplies itself (replay/executor.py::_verify_params); an artifact needn't declare them.
_RESERVED_PLACEHOLDERS = {"base_url", "run_id", "primary_id"}


def check_annotation_placeholders(artifact) -> list[str]:
    """Returns the {names} in an artifact's annotations that are neither inputs nor base_url. Empty
    means everything resolves on replay."""
    known = {i.name for i in artifact.inputs} | _RESERVED_PLACEHOLDERS
    texts: list[str] = []
    if artifact.commit_verification:
        texts += [artifact.commit_verification.navigate_template, artifact.commit_verification.detect.value]
    for item in [*artifact.business_outcomes, *artifact.recoverable_conditions]:
        texts.append(item.detect.value)
    used = {name for text in texts for name in re.findall(r"\{(\w+)\}", text)}
    return sorted(used - known)


def annotations_for(
    capability_id: str,
) -> tuple[list[BusinessOutcomeSignature], list[RecoverableCondition], Optional[CommitVerification]]:
    return _ANNOTATIONS.get(capability_id, ([], [], None))
