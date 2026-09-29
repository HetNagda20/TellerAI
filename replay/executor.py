"""Deterministic replay: the production execution path. No LLM in the loop.
Every action comes from the artifact's recorded steps and the caller's
input params.

Before each step, the engine checks the artifact's declared
recoverable_conditions and business_outcomes against the current page state:

  1. A matching recoverable condition gets exactly one bounded recovery
     action, then execution continues into this same step.
  2. A matching business outcome ends the run immediately as
     "business_outcome" (a legitimate result, not a crash).
  3. Otherwise the step's target is resolved via the ranked locator
     fallback chain and executed. A Playwright timeout (transiently slow
     environment) gets a bounded retry. A locator that never resolves does
     not retry, since the same exhausted candidates will not find a
     different answer. Either way, an unresolved failure becomes a
     structured hard_failure naming the step, what was expected, and what
     was observed. Never a reason to call the LLM.

Risky steps: a step recorded as risk="confirm" runs unattended only if the
artifact's status is "approved" (flagged in ReplayResult.unattended_risky_steps).
Otherwise it goes through the same risky_action_confirm human gate discovery
uses, and fails closed (never executes) if denied or if there is no approver.

Replay HITL boundary: escalate_on_failure notifies a human and, if they
report having fixed something operational, allows exactly one bounded
retry of the same recorded step. It is not discovery. No GestureController
capture is ever armed on this path (see handoff/session.py's
DISCOVERY_HITL_REASONS). A replay run ends in success, business_outcome,
or hard_failure; it never comes out with a changed artifact.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from artifact.schema import (
    ActionType,
    Artifact,
    ArtifactStatus,
    BusinessOutcomeSignature,
    Checkpoint,
    CheckpointKind,
    CommitVerification,
    RecoverableCondition,
)
from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action
from guardrails.redact import redact_value
from handoff.gesture import GestureController
from handoff.session import HandoffSession, InterventionRequest, _CliOperator
from replay.locators import ResolutionError, resolve_frame_chain, resolve_target, resolve_text
from replay.outcomes import EscalationRecord, FailureDetail, ReplayResult, StrategyLogEntry

logger = logging.getLogger(__name__)

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"
_TRANSIENT_RETRY_ATTEMPTS = 2  # total attempts on a timeout, i.e. one retry
_TRANSIENT_RETRY_WAIT_MS = 500
_ACTION_TIMEOUT_MS = 5000  # per-attempt actionability timeout; module-level so tests can shorten it


def _fmt(value: Optional[str], params: dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.format(**params)
    except (KeyError, IndexError):
        return value


def _human_summary(artifact: Artifact, step, params: dict[str, str]) -> str:
    """Plain-language summary built from Artifact.description and
    Step.description only, never internal detail like a step index or a raw
    exception message. Callers append that technical detail separately."""
    param_summary = ", ".join(f"{name}: {value}" for name, value in params.items())
    return f'{artifact.description} The step that didn\'t complete: "{step.description}" (for {param_summary}).'


def _run_id() -> str:
    """Timestamp plus a random suffix, since two concurrent replays starting
    in the same second would otherwise collide on the same evidence_dir."""
    stamp = datetime.now(timezone.utc).strftime("replay_%Y%m%dT%H%M%SZ")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _check_checkpoint(page: Page, checkpoint: Checkpoint, params: dict[str, str]) -> bool:
    value = _fmt(checkpoint.value, params)
    if checkpoint.kind == CheckpointKind.URL_CONTAINS:
        return value in urlparse(page.url).path
    if checkpoint.kind == CheckpointKind.TEXT_CONTAINS:
        try:
            return value in page.inner_text("body")
        except Exception:
            return False
    if checkpoint.kind in (CheckpointKind.ELEMENT_VISIBLE, CheckpointKind.ELEMENT_NOT_VISIBLE):
        assert checkpoint.target is not None
        try:
            scope = resolve_frame_chain(page, checkpoint.target.frame_chain, params)
            resolved = resolve_target(scope, checkpoint.target, params)
            visible = resolved.locator is not None and resolved.locator.is_visible()
        except ResolutionError:
            visible = False
        return visible if checkpoint.kind == CheckpointKind.ELEMENT_VISIBLE else not visible
    return False


def _match_business_outcome(
    page: Page, signatures: list[BusinessOutcomeSignature], params: dict[str, str]
) -> Optional[BusinessOutcomeSignature]:
    for sig in signatures:
        if _check_checkpoint(page, sig.detect, params):
            return sig
    return None


def _match_recoverable(
    page: Page, conditions: list[RecoverableCondition], params: dict[str, str]
) -> Optional[RecoverableCondition]:
    for cond in conditions:
        if _check_checkpoint(page, cond.detect, params):
            return cond
    return None


def _execute_recovery(page: Page, cond: RecoverableCondition, params: dict[str, str]) -> None:
    if cond.recovery_action == ActionType.NAVIGATE:
        page.goto(_fmt(cond.recovery_value, params), wait_until="load")
        return
    assert cond.recovery_target is not None
    scope = resolve_frame_chain(page, cond.recovery_target.frame_chain, params)
    resolved = resolve_target(scope, cond.recovery_target, params)
    if cond.recovery_action == ActionType.CLICK:
        if resolved.locator is not None:
            resolved.locator.click(timeout=5000)
        else:
            page.mouse.click(resolved.coordinates["x"], resolved.coordinates["y"])
    elif cond.recovery_action == ActionType.FILL and resolved.locator is not None:
        resolved.locator.fill(_fmt(cond.recovery_value, params) or "", timeout=5000)
    page.wait_for_load_state("domcontentloaded", timeout=5000)


@dataclass
class _StepOutcome:
    ok: bool
    expected: str = ""
    observed: str = ""
    message: str = ""


def _verify_commit(page: Page, verification: CommitVerification, params: dict[str, str]) -> Optional[bool]:
    """Runs a reviewer-declared check for whether an ambiguous step failure
    actually committed server-side. Returns True (evidence it committed),
    False (checked, no evidence), or None if the check itself could not
    complete. None is deliberately distinct from False: "we don't know"
    must never be treated the same as "we checked and it's clear."""
    try:
        url = _fmt(verification.navigate_template, params)
        page.goto(url, wait_until="load")
        return _check_checkpoint(page, verification.detect, params)
    except Exception:
        return None


def _resolve_ambiguous_commit(
    page: Page,
    artifact: Artifact,
    step,
    params: dict[str, str],
    outcome: _StepOutcome,
    handoff: Optional[HandoffSession],
    escalate_on_failure: bool,
) -> tuple[bool, _StepOutcome, Optional[EscalationRecord]]:
    """Called only when a step fails at or after this artifact's risky
    confirm step and a CommitVerification is declared. Returns
    (committed_and_recovered, possibly-amended outcome, an EscalationRecord
    if a human was actually asked, else None).

    A check that finds a clear answer is trusted outright. A check that
    cannot complete is where the real decision lives: if escalate_on_failure
    is set, a human is asked, with full context, to make the call. If no
    human is available, this never falls back to retrying; it reports
    hard_failure with an explicit warning that the caller must not treat
    the failure as safe to retry with the same params.
    """
    # navigate_template may reference {base_url}, but real artifacts rarely declare
    # it as a runtime input (it is baked into a NAVIGATE step as a literal at record
    # time). Fall back to the artifact's own target_app.base_url so the check can
    # still run; an explicit runtime params["base_url"] wins if present.
    verify_params = {"base_url": artifact.target_app.base_url, **params}
    committed = _verify_commit(page, artifact.commit_verification, verify_params)
    if committed is True:
        logger.info("commit_verification found evidence of commit capability=%s run_id=%s", artifact.capability_id, step.index)
        return True, outcome, None
    if committed is False:
        return False, outcome, None

    if escalate_on_failure and handoff is not None:
        record = handoff.escalate(
            InterventionRequest(
                reason="commit_verification_inconclusive",
                goal_or_capability=artifact.capability_id,
                message=(
                    f"{_human_summary(artifact, step, params)} We also couldn't automatically confirm "
                    f"whether this actually went through (checked: {artifact.commit_verification.description}). "
                    "Please check manually, e.g. the account's real transaction history, before answering. "
                    "Answering yes below means \"I've confirmed it went through\"; no means \"I've confirmed "
                    "it did not.\" Do not guess. "
                    f"(Technical detail for support: step {step.index} [{step.action.value}]: {outcome.message})"
                ),
                step_index=step.index,
            )
        )
        logger.warning(
            "escalation raised reason=commit_verification_inconclusive capability=%s outcome=%s",
            artifact.capability_id, record.outcome,
        )
        escalation = EscalationRecord(
            reason="commit_verification_inconclusive", outcome=record.outcome, human_note=record.human_note, resume=record.resume
        )
        if record.resume:
            return True, outcome, escalation
        outcome.message = f"{outcome.message} A human confirmed this action did not commit."
        return False, outcome, escalation

    outcome.message = (
        f"{outcome.message} Commit status could not be verified (the declared commit-verification "
        "check itself failed to complete). Do NOT retry this capability with the same params without "
        "manually confirming whether it already committed."
    )
    return False, outcome, None


def _risky_steps(artifact: Artifact) -> list:
    return [s for s in artifact.steps if s.risk == "confirm"]


def _needs_per_run_approval(artifact: Artifact) -> bool:
    """Per-artifact approval model: a reviewer approving an artifact
    (status=approved) is what authorizes its risky steps to run unattended.
    Any other artifact that contains a risky step falls back to the same
    live human gate discovery uses, once per run, before that step."""
    return artifact.status != ArtifactStatus.APPROVED and bool(_risky_steps(artifact))


def _gate_risky_step(
    handoff: Optional[HandoffSession],
    artifact: Artifact,
    step,
    params: dict[str, str],
    escalations: list[EscalationRecord],
) -> Optional[FailureDetail]:
    """The replay-side twin of discovery's Executor._gate(): same
    HandoffSession.escalate(reason="risky_action_confirm") call, run before
    the risky step executes. Returns None if a human approved it, else the
    FailureDetail to end the run with. A missing handoff fails closed."""
    if handoff is None:
        return FailureDetail(
            step_index=step.index, action=step.action.value,
            expected="human approval of a risky step in a draft artifact",
            observed="no approver available", message="Risky step refused: no approver available.",
        )
    record = handoff.escalate(
        InterventionRequest(
            reason="risky_action_confirm",
            goal_or_capability=artifact.capability_id,
            message=(
                f"{_human_summary(artifact, step, params)} This artifact is a draft (not approved for "
                "unattended replay) and this step looks irreversible, approve?"
            ),
            step_index=step.index,
        )
    )
    escalations.append(
        EscalationRecord(reason="risky_action_confirm", outcome=record.outcome, human_note=record.human_note, resume=record.resume)
    )
    if record.outcome == "approved":
        return None
    return FailureDetail(
        step_index=step.index, action=step.action.value,
        expected="human approval of a risky step in a draft artifact",
        observed=record.outcome, message="Risky step was not approved; it was not executed.",
    )


def replay_artifact(
    artifact: Artifact,
    params: dict[str, str],
    headless: bool = True,
    escalate_on_failure: bool = False,
    operator_factory: Optional[Callable[[Page], object]] = None,
    slow_mo_ms: int = 0,
) -> ReplayResult:
    """operator_factory, if given, is called with the live Page this call
    creates, and its return value is used as HandoffSession's operator. This
    is the seam a caller (a test, or a future handoff mode) needs to hand
    the operator a reference to the same page replay is driving. None
    reproduces prior behavior: HandoffSession falls back to _CliOperator().
    Used whenever a handoff is needed: escalate_on_failure, or the per-run
    approval gate a draft artifact's risky step requires (see
    _needs_per_run_approval). With neither, it is ignored.

    slow_mo_ms delays every browser operation by that many milliseconds so a
    human can follow a headed run. Pacing only: no step, locator, or outcome
    changes, and 0 (the default) is exactly the previous behavior.
    """
    missing = [p.name for p in artifact.inputs if p.required and p.name not in params]
    if missing:
        raise ValueError(f"Missing required params: {missing}")

    declared_names = {p.name for p in artifact.inputs}
    unknown = [name for name in params if name not in declared_names]
    if unknown:
        raise ValueError(
            f"Unknown params not declared by this artifact's inputs: {unknown}. "
            f"Declared inputs: {sorted(declared_names)}."
        )

    run_id = _run_id()
    evidence_dir = EVIDENCE_ROOT / f"{run_id}_{artifact.capability_id}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    allowlist = Allowlist.load()
    logger.info("replay start capability=%s version=%s run_id=%s", artifact.capability_id, artifact.version, run_id)

    outputs: dict[str, str] = {}
    strategy_log: list[StrategyLogEntry] = []
    escalations: list[EscalationRecord] = []
    unattended_risky: list[int] = []
    steps_executed = 0

    needs_gate = _needs_per_run_approval(artifact)
    if needs_gate and headless and operator_factory is None:
        # Fail closed before touching the app: a draft artifact's risky step needs a
        # live human, and a headless run with no supplied operator has none.
        first = _risky_steps(artifact)[0]
        logger.error("replay refused capability=%s run_id=%s reason=draft_artifact_risky_step_no_approver", artifact.capability_id, run_id)
        result = ReplayResult(
            kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version, run_id=run_id,
            failure=FailureDetail(
                step_index=first.index, action=first.action.value,
                expected="an approved artifact, or a live approver for its risky step",
                observed=f"artifact status={artifact.status.value}, headless run with no operator",
                message=(
                    "Refused before any step ran: this artifact is a draft containing a risky/irreversible "
                    "step. Approve the artifact for unattended replay, or replay headed / with an operator."
                ),
            ),
            evidence_dir=str(evidence_dir),
        )
        _write_evidence(evidence_dir, artifact, params, result)
        return result

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        page = browser.new_page()
        # See agent/loop.py's matching header on the discovery side, and
        # mock_app/app.py for how the server surfaces/records this.
        page.set_extra_http_headers({"X-Automation-Source": "replay", "X-Run-Id": run_id})
        handoff = None
        if escalate_on_failure or needs_gate:
            if operator_factory is not None:
                operator = operator_factory(page)
            else:
                # No custom operator: default to an on-page banner when there is a real
                # browser for an end user to see it in. capture=False keeps replay
                # structurally incapable of recording actions; the banner is
                # notification only.
                gesture = GestureController(page) if not headless else None
                operator = _CliOperator(gesture=gesture, capture=False) if gesture is not None else None
            handoff = HandoffSession(page=page, run_id=run_id, evidence_dir=evidence_dir, operator=operator)

        reached_confirm_step = False
        try:
            for step in artifact.steps:
                recovered_name = None
                rc = _match_recoverable(page, artifact.recoverable_conditions, params)
                if rc is not None:
                    _execute_recovery(page, rc, params)
                    recovered_name = rc.name

                bo = _match_business_outcome(page, artifact.business_outcomes, params)
                if bo is not None:
                    logger.info("replay business_outcome capability=%s run_id=%s outcome=%s", artifact.capability_id, run_id, bo.name)
                    result = ReplayResult(
                        kind="business_outcome",
                        capability_id=artifact.capability_id,
                        version=artifact.version,
                        run_id=run_id,
                        outputs=outputs,
                        business_outcome_name=bo.name,
                        business_outcome_description=bo.description,
                        steps_executed=steps_executed,
                        strategy_log=strategy_log,
                        escalations=escalations, unattended_risky_steps=unattended_risky,
                        evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                if step.risk == "confirm":
                    if artifact.status == ArtifactStatus.APPROVED:
                        unattended_risky.append(step.index)
                        logger.warning(
                            "risky step executing unattended under an approved artifact capability=%s run_id=%s step=%s",
                            artifact.capability_id, run_id, step.index,
                        )
                    else:
                        denial = _gate_risky_step(handoff, artifact, step, params, escalations)
                        if denial is not None:
                            logger.error(
                                "replay hard_failure (risky step not approved) capability=%s run_id=%s step=%s",
                                artifact.capability_id, run_id, step.index,
                            )
                            result = ReplayResult(
                                kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version,
                                run_id=run_id, outputs=outputs, failure=denial, steps_executed=steps_executed,
                                strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky,
                                evidence_dir=str(evidence_dir),
                            )
                            page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                            _write_evidence(evidence_dir, artifact, params, result)
                            return result
                    # Set before executing: if the confirm click times out, the
                    # underlying request may still have registered server side.
                    reached_confirm_step = True

                outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name)
                steps_executed += 1

                if not outcome.ok:
                    if escalate_on_failure and handoff is not None:
                        record = handoff.escalate(
                            InterventionRequest(
                                reason="replay_hard_failure",
                                goal_or_capability=artifact.capability_id,
                                message=(
                                    f"{_human_summary(artifact, step, params)} "
                                    f"(Technical detail for support: step {step.index} [{step.action.value}]: {outcome.message})"
                                ),
                                step_index=step.index,
                            )
                        )
                        logger.warning(
                            "escalation raised reason=replay_hard_failure capability=%s run_id=%s outcome=%s",
                            artifact.capability_id, run_id, record.outcome,
                        )
                        escalations.append(
                            EscalationRecord(
                                reason="replay_hard_failure",
                                outcome=record.outcome,
                                human_note=record.human_note,
                                resume=record.resume,
                            )
                        )
                        # Bounded operational recovery only: the human reported having
                        # fixed something in the environment, so the same recorded step
                        # gets exactly one more attempt, never a second retry if this
                        # also fails. Whatever the human did is not captured as a step;
                        # HandoffSession.escalate() drops captured_actions for
                        # replay_hard_failure unconditionally, so replay cannot teach
                        # the artifact by construction.
                        if record.resume:
                            outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name)

                if not outcome.ok and reached_confirm_step and artifact.commit_verification is not None:
                    committed, outcome, escalation = _resolve_ambiguous_commit(
                        page, artifact, step, params, outcome, handoff, escalate_on_failure
                    )
                    if escalation is not None:
                        escalations.append(escalation)
                    if committed:
                        logger.info("replay success (recovered via commit_verification) capability=%s run_id=%s", artifact.capability_id, run_id)
                        page.screenshot(path=str(evidence_dir / "success.png"))
                        result = ReplayResult(
                            kind="success",
                            capability_id=artifact.capability_id,
                            version=artifact.version,
                            run_id=run_id,
                            outputs=outputs,
                            steps_executed=steps_executed,
                            strategy_log=strategy_log,
                            escalations=escalations, unattended_risky_steps=unattended_risky,
                            evidence_dir=str(evidence_dir),
                            recovered_via_commit_verification=True,
                        )
                        _write_evidence(evidence_dir, artifact, params, result)
                        return result

                if not outcome.ok:
                    logger.error(
                        "replay hard_failure capability=%s run_id=%s step=%s action=%s message=%s",
                        artifact.capability_id, run_id, step.index, step.action.value, outcome.message,
                    )
                    failure = FailureDetail(
                        step_index=step.index,
                        action=step.action.value,
                        expected=outcome.expected,
                        observed=outcome.observed,
                        message=outcome.message,
                    )
                    result = ReplayResult(
                        kind="hard_failure",
                        capability_id=artifact.capability_id,
                        version=artifact.version,
                        run_id=run_id,
                        outputs=outputs,
                        failure=failure,
                        steps_executed=steps_executed,
                        strategy_log=strategy_log,
                        escalations=escalations, unattended_risky_steps=unattended_risky,
                        evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                if step.extract_as and outcome.observed:
                    outputs[step.extract_as] = outcome.observed

                if step.checkpoint and not _check_checkpoint(page, step.checkpoint, params):
                    logger.error(
                        "replay hard_failure (inline checkpoint) capability=%s run_id=%s step=%s",
                        artifact.capability_id, run_id, step.index,
                    )
                    failure = FailureDetail(
                        step_index=step.index,
                        action=step.action.value,
                        expected=f"inline checkpoint {step.checkpoint.kind.value}={step.checkpoint.value}",
                        observed=page.url,
                        message="Inline checkpoint failed after otherwise-successful action.",
                    )
                    result = ReplayResult(
                        kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version,
                        run_id=run_id, outputs=outputs, failure=failure, steps_executed=steps_executed,
                        strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

            final_bo = _match_business_outcome(page, artifact.business_outcomes, params)
            if final_bo is not None:
                logger.info("replay business_outcome capability=%s run_id=%s outcome=%s", artifact.capability_id, run_id, final_bo.name)
                result = ReplayResult(
                    kind="business_outcome", capability_id=artifact.capability_id, version=artifact.version,
                    run_id=run_id, outputs=outputs, business_outcome_name=final_bo.name,
                    business_outcome_description=final_bo.description, steps_executed=steps_executed,
                    strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                )
                page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                _write_evidence(evidence_dir, artifact, params, result)
                return result

            if not _check_checkpoint(page, artifact.final_checkpoint, params):
                logger.error("replay hard_failure (final checkpoint) capability=%s run_id=%s", artifact.capability_id, run_id)
                failure = FailureDetail(
                    step_index=len(artifact.steps) - 1,
                    action="final_checkpoint",
                    expected=f"{artifact.final_checkpoint.kind.value}={_fmt(artifact.final_checkpoint.value, params)}",
                    observed=page.url,
                    message="All steps executed but the final checkpoint did not hold.",
                )
                result = ReplayResult(
                    kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version,
                    run_id=run_id, outputs=outputs, failure=failure, steps_executed=steps_executed,
                    strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                )
                page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                _write_evidence(evidence_dir, artifact, params, result)
                return result

            logger.info("replay success capability=%s run_id=%s steps_executed=%s", artifact.capability_id, run_id, steps_executed)
            page.screenshot(path=str(evidence_dir / "success.png"))
            result = ReplayResult(
                kind="success", capability_id=artifact.capability_id, version=artifact.version, run_id=run_id,
                outputs=outputs, steps_executed=steps_executed, strategy_log=strategy_log,
                escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
            )
            _write_evidence(evidence_dir, artifact, params, result)
            return result
        finally:
            browser.close()


def _run_step(page, step, params, allowlist: Allowlist, strategy_log: list[StrategyLogEntry], recovered_name) -> _StepOutcome:
    """Bounded retry for transient timeouts (page/app slow, not broken):
    up to _TRANSIENT_RETRY_ATTEMPTS tries with a short wait between. A
    ResolutionError is not retried here, since _run_step_once already
    exhausts every ranked locator candidate in one call."""
    last: Optional[_StepOutcome] = None
    for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            return _run_step_once(page, step, params, allowlist, strategy_log, recovered_name)
        except PlaywrightTimeoutError as e:
            last = _StepOutcome(
                False,
                expected=f"{step.action.value} to complete",
                observed=str(e),
                message=f"Transient timeout on attempt {attempt}/{_TRANSIENT_RETRY_ATTEMPTS}.",
            )
            if attempt < _TRANSIENT_RETRY_ATTEMPTS:
                page.wait_for_timeout(_TRANSIENT_RETRY_WAIT_MS)
    assert last is not None
    last.message = f"Exhausted {_TRANSIENT_RETRY_ATTEMPTS} attempts after repeated transient timeouts. " + last.message
    return last


def _run_step_once(page, step, params, allowlist: Allowlist, strategy_log: list[StrategyLogEntry], recovered_name) -> _StepOutcome:
    try:
        if step.action == ActionType.NAVIGATE:
            url = _fmt(step.value_template, params)
            if not allowlist.url_allowed(url):
                logger.warning("blocked by allowlist: navigate url=%s", url)
                return _StepOutcome(False, expected="navigate within allowlist", observed=url, message="Blocked by allowlist policy.")
            page.goto(url, wait_until="load")
            strategy_log.append(StrategyLogEntry(step_index=step.index, action=step.action.value, recovered_condition=recovered_name))
            return _StepOutcome(True)

        scope = resolve_frame_chain(page, step.target.frame_chain, params)

        if step.action == ActionType.READ_TEXT:
            # A different success criterion than other actions: a candidate can
            # structurally resolve without that being a meaningful extraction. No
            # candidate producing non-empty text raises ResolutionError, caught
            # below like any other resolution failure.
            text, strategy_used = resolve_text(scope, step.target, params, timeout_ms=_ACTION_TIMEOUT_MS)
            strategy_log.append(
                StrategyLogEntry(step_index=step.index, action=step.action.value, strategy_used=strategy_used, recovered_condition=recovered_name)
            )
            if not allowlist.action_allowed(step.action.value):
                logger.warning("blocked by allowlist: action=%s", step.action.value)
                return _StepOutcome(False, expected="action within allowlist", observed=step.action.value, message="Blocked by allowlist policy.")
            out = _StepOutcome(True)
            out.observed = text
            return out

        resolved = resolve_target(scope, step.target, params)
        strategy_log.append(
            StrategyLogEntry(step_index=step.index, action=step.action.value, strategy_used=resolved.strategy, recovered_condition=recovered_name)
        )

        if not allowlist.action_allowed(step.action.value):
            logger.warning("blocked by allowlist: action=%s", step.action.value)
            return _StepOutcome(False, expected="action within allowlist", observed=step.action.value, message="Blocked by allowlist policy.")

        if step.action == ActionType.CLICK:
            if resolved.locator is not None:
                resolved.locator.click(timeout=_ACTION_TIMEOUT_MS)
            else:
                page.mouse.click(resolved.coordinates["x"], resolved.coordinates["y"])
            page.wait_for_load_state("domcontentloaded", timeout=_ACTION_TIMEOUT_MS)
            return _StepOutcome(True)

        if step.action == ActionType.FILL:
            value = _fmt(step.value_template, params) or ""
            if resolved.locator is None:
                return _StepOutcome(False, expected="fillable element", observed="coordinates-only match", message="Cannot fill via coordinates.")
            resolved.locator.fill(value, timeout=_ACTION_TIMEOUT_MS)
            return _StepOutcome(True)

        if step.action == ActionType.SELECT:
            value = _fmt(step.value_template, params) or ""
            if resolved.locator is None:
                return _StepOutcome(False, expected="selectable element", observed="coordinates-only match", message="Cannot select via coordinates.")
            resolved.locator.select_option(value, timeout=_ACTION_TIMEOUT_MS)
            return _StepOutcome(True)

        return _StepOutcome(False, expected="known action type", observed=step.action.value, message="Unhandled action type.")

    except ResolutionError as e:
        return _StepOutcome(False, expected=f"one of the recorded locator candidates for step {step.index}", observed=str(e), message="No locator candidate resolved.")
    except PlaywrightTimeoutError:
        # Not caught here: let it propagate to _run_step, which decides whether a
        # bounded retry is warranted.
        raise
    except Exception as e:  # noqa: BLE001 - any other Playwright/navigation error becomes a structured hard failure
        return _StepOutcome(False, expected=f"{step.action.value} to succeed", observed=str(e), message="Unhandled runtime error during step execution.")


def _write_evidence(evidence_dir: Path, artifact: Artifact, params: dict[str, str], result: ReplayResult) -> None:
    (evidence_dir / "replay_result.json").write_text(result.model_dump_json(indent=2))
    # Redact by field name for the written copy only. The real params dict
    # (unredacted) already drove Playwright above; evidence is audit-only and
    # never re-resolved, so a plain [REDACTED] substitution here is safe.
    safe_params = {name: redact_value(name, value) for name, value in params.items()}
    (evidence_dir / "replay_input.json").write_text(
        json.dumps({"capability_id": artifact.capability_id, "version": artifact.version, "params": safe_params}, indent=2)
    )
