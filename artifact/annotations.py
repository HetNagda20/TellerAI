"""Reviewer-declared business outcomes and recoverable conditions for known
capabilities.

The discovery run only ever walks one path (the happy path a human operator
would take). It cannot discover "what does a locked account look like" on
its own. A reviewer who knows the target app adds that domain knowledge to
the artifact before approving it for unattended replay — this module is a
stand-in for that review step (see the `status: draft -> approved` field on
Artifact, and REPORT.md section 8 on cuts).
"""

from __future__ import annotations

from artifact.schema import (
    ActionType,
    BusinessOutcomeSignature,
    Checkpoint,
    CheckpointKind,
    LocatorCandidate,
    LocatorStrategy,
    RecoverableCondition,
    Target,
)

_CONTINUE_BUTTON = Target(
    candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": "Continue"})]
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

_ANNOTATIONS: dict[str, tuple[list[BusinessOutcomeSignature], list[RecoverableCondition]]] = {
    "open-member-subaccount": (
        [
            BusinessOutcomeSignature(
                name="member_not_found",
                detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="No member found"),
                description="The given member ID does not exist in the core system.",
            ),
            _ACCOUNT_LOCKED,
            BusinessOutcomeSignature(
                name="invalid_deposit_amount",
                detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Initial deposit must be a number of at least $25.00."),
                description="The requested initial deposit violates the minimum-deposit business rule.",
            ),
            _INSUFFICIENT_FUNDS,
        ],
        [_SESSION_INTERSTITIAL],
    ),
    "transfer-funds": (
        [
            BusinessOutcomeSignature(
                name="recipient_not_found",
                detect=Checkpoint(kind=CheckpointKind.TEXT_CONTAINS, value="Recipient not found"),
                description="The destination member ID does not exist in the core system.",
            ),
            _INSUFFICIENT_FUNDS,
            _ACCOUNT_LOCKED,
        ],
        [],
    ),
}


def annotations_for(capability_id: str) -> tuple[list[BusinessOutcomeSignature], list[RecoverableCondition]]:
    return _ANNOTATIONS.get(capability_id, ([], []))
