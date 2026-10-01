"""Deterministic replay, with no LLM. Each step checks recoverable conditions and business outcomes, then resolves its target through ranked locators. Failures are structured, never a reason to call a model."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from artifact.annotations import app_business_outcomes, app_recoverable_conditions
from artifact.pagetext import text_present
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
from guardrails.redact import redact_text, redact_value
from handoff.gesture import GestureController
from handoff.remote import free_loopback_port, launch_args, remote_debugging_enabled
from handoff.session import HandoffSession, InterventionRequest, _CliOperator
from replay.locators import ResolutionError, resolve_frame_chain, resolve_target, resolve_text
from replay.outcomes import EscalationRecord, FailureDetail, ReplayResult, StrategyLogEntry

logger = logging.getLogger(__name__)

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"
_TRANSIENT_RETRY_ATTEMPTS = 2  # total attempts on a timeout, i.e. one retry
_TRANSIENT_RETRY_WAIT_MS = 500
_ACTION_TIMEOUT_MS = 5000  # per-attempt actionability timeout; module-level so tests can shorten it
_CHECKPOINT_TIMEOUT_MS = 10000  # how long a checkpoint is polled before it counts as failed
_POLL_MS = 250
_SETTLE_MS = 3000  # cap on waiting for the network to go quiet after a click; never fails a step
_COMMIT_RETRY_ATTEMPTS = 1  # whole-transaction re-runs after a VERIFIED non-commit; never more than one
_COMMIT_SETTLE_MS = 500  # wait before trusting a "no evidence" answer from the history page
_RECOVERY_RESTARTS = 1  # after a recovery that loses the page (signing back in), the recorded steps re-run from the start once
_RETRY = object()  # sentinel: this attempt verified nothing was registered, run the transaction again


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
        return text_present(page, value)
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


def _effective_business_outcomes(artifact: Artifact) -> tuple[list[BusinessOutcomeSignature], dict[str, str]]:
    """The capability's own outcomes first, then the app's shared ones, without duplicates. Also
    returns a name to 'capability' or 'app' map for reporting."""
    own = list(artifact.business_outcomes)
    own_names = {b.name for b in own}
    shared = [b for b in app_business_outcomes(artifact.target_app.app_id) if b.name not in own_names]
    return own + shared, {**{b.name: "capability" for b in own}, **{b.name: "app" for b in shared}}


def _observed_page(page: Page, last_response: dict) -> str:
    """What the page showed at a failure: last document HTTP status, URL, and the start of the
    visible text, redacted."""
    try:
        text = re.sub(r"\s+", " ", page.inner_text("body", timeout=1500)).strip()
    except Exception:
        text = ""
    status = f"HTTP {last_response['status']} " if last_response.get("status") else ""
    return redact_text(f"{status}{page.url} | {text[:300]}")


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


class _RecoveryError(Exception):
    """A declared recovery could not run: a credential was not provided, or an action is not allowed."""


_ENV_PLACEHOLDER = re.compile(r"\{env:(\w+)\}")
_reauths = threading.local()  # how many times this replay signed back in (one replay per thread)


def _with_secrets(value: str) -> str:
    """Fills {env:NAME} from the environment. The result lives only in this call: it is never logged or stored."""

    def fill(match: re.Match) -> str:
        got = os.environ.get(match.group(1))
        if got is None:
            raise _RecoveryError(f"the environment variable {match.group(1)} is not set, so replay cannot sign back in")
        return got

    return _ENV_PLACEHOLDER.sub(fill, value)


def _effective_recoverables(artifact: Artifact) -> list[RecoverableCondition]:
    """The capability's own recoverable conditions, then the application's shared ones (session expiry), without
    duplicates."""
    own = list(artifact.recoverable_conditions)
    names = {c.name for c in own}
    return own + [c for c in app_recoverable_conditions(artifact.target_app.app_id) if c.name not in names]


def _run_recovery_action(page: Page, action: Optional[ActionType], target, value: Optional[str], params: dict[str, str], allowlist: Allowlist) -> None:
    if action is None or not allowlist.action_allowed(action.value):
        raise _RecoveryError(f"the recovery action {getattr(action, 'value', action)!r} is not allowed")
    text = _fmt(_with_secrets(value), params) if value is not None else None
    if action == ActionType.NAVIGATE:
        if not allowlist.url_allowed(text or ""):
            raise _RecoveryError("the recovery navigates outside the allowlist")
        page.goto(text, wait_until="load")
        return
    assert target is not None

    def find():
        return resolve_target(resolve_frame_chain(page, target.frame_chain, params), target, params)

    resolved = _until_resolved(page, find, 3000)
    if action == ActionType.CLICK:
        if resolved.locator is not None:
            resolved.locator.click(timeout=5000)
        else:
            page.mouse.click(resolved.coordinates["x"], resolved.coordinates["y"])
    elif action == ActionType.FILL and resolved.locator is not None:
        resolved.locator.fill(text or "", timeout=5000)
    page.wait_for_load_state("domcontentloaded", timeout=5000)


def _execute_recovery(page: Page, cond: RecoverableCondition, params: dict[str, str], allowlist: Allowlist) -> None:
    if cond.recovery_sequence:
        for act in cond.recovery_sequence:
            _run_recovery_action(page, act.action, act.target, act.value, params, allowlist)
    else:
        _run_recovery_action(page, cond.recovery_action, cond.recovery_target, cond.recovery_value, params, allowlist)
    if cond.restart_after_recovery:
        _reauths.n = getattr(_reauths, "n", 0) + 1


@dataclass
class _StepOutcome:
    ok: bool
    expected: str = ""
    observed: str = ""
    message: str = ""


def _primary_id(artifact: Artifact, params: dict[str, str]) -> str:
    """The input the flow opened with: the first fill step's {param}, normally the member lookup.
    Backs the reserved {primary_id} placeholder."""
    for step in artifact.steps:
        if step.action == ActionType.FILL and step.value_template:
            m = re.fullmatch(r"\{(\w+)\}", step.value_template)
            if m:
                return str(params.get(m.group(1), ""))
    return ""


def _verify_params(artifact: Artifact, params: dict[str, str], run_tag: str) -> dict[str, str]:
    """Placeholders a commit check may use besides the artifact's inputs: {base_url}, {primary_id}, and {run_id}, this attempt's id. An explicit param of the same name wins."""
    return {"base_url": artifact.target_app.base_url, "primary_id": _primary_id(artifact, params), "run_id": run_tag, **params}


def _verify_commit(page: Page, artifact: Artifact, params: dict[str, str]) -> Optional[bool]:
    """Checks whether a failed step actually committed. True means evidence it did, False means checked and nothing, None means the check could not complete. None is not False.
    A history page behind an expired session would read as "nothing found", so the check signs back in first."""
    verification = artifact.commit_verification
    try:
        url = _fmt(verification.navigate_template, params)
        page.goto(url, wait_until="load")
        signed_out = next((c for c in _effective_recoverables(artifact) if c.restart_after_recovery and _check_checkpoint(page, c.detect, params)), None)
        if signed_out is not None:
            _execute_recovery(page, signed_out, params, Allowlist.load())
            page.goto(url, wait_until="load")
        return _check_checkpoint(page, verification.detect, params)
    except Exception:
        return None


def _verify_commit_settled(page: Page, artifact: Artifact, params: dict[str, str]) -> Optional[bool]:
    """Trusts a 'no evidence' answer only after a short wait and a second look, so a lagging history page isn't mistaken for a transaction that never happened."""
    first = _verify_commit(page, artifact, params)
    if first is not False:
        return first
    page.wait_for_timeout(_COMMIT_SETTLE_MS)
    return _verify_commit(page, artifact, params)


def _earlier_attempt_committed(page: Page, artifact: Artifact, params: dict[str, str], run_id: str, attempt: int) -> bool:
    """True if an earlier attempt's run id is now in the history, meaning it registered after all and the retry made a duplicate."""
    for n in range(1, attempt):
        found = _verify_commit(page, artifact, _verify_params(artifact, params, f"{run_id}-a{n}"))
        if found is True:
            return True
    return False


def _resolve_ambiguous_commit(
    page: Page,
    artifact: Artifact,
    step,
    params: dict[str, str],
    outcome: _StepOutcome,
    handoff: Optional[HandoffSession],
    escalate_on_failure: bool,
    run_tag: str,
    attempt: int,
    max_attempts: int,
) -> tuple[str, _StepOutcome, Optional[EscalationRecord]]:
    """Runs when a step fails at or after the risky step and a check is declared. Returns a verdict (committed, retry, failed), the outcome, and any escalation record."""
    verify_params = _verify_params(artifact, params, run_tag)
    committed = _verify_commit_settled(page, artifact, verify_params)
    if committed is True:
        logger.info("commit_verification found this attempt's run id capability=%s attempt=%s", artifact.capability_id, attempt)
        return "committed", outcome, None

    if committed is False:
        if attempt < max_attempts:
            return "retry", outcome, None
        if escalate_on_failure and handoff is not None:
            record = handoff.escalate(
                InterventionRequest(
                    reason="commit_retry_exhausted",
                    goal_or_capability=artifact.capability_id,
                    message=(
                        f"{_human_summary(artifact, step, params)} The transaction was attempted {attempt} times and the "
                        f"history was checked after each: this run's id ({run_tag}) never appeared, so it was not "
                        "registered. Please handle it manually. Answering yes below means \"I've confirmed it went "
                        "through\"; no means \"it did not.\" "
                        f"(Technical detail for support: step {step.index} [{step.action.value}]: {outcome.message})"
                    ),
                    step_index=step.index,
                )
            )
            logger.warning(
                "escalation raised reason=commit_retry_exhausted capability=%s outcome=%s", artifact.capability_id, record.outcome
            )
            escalation = EscalationRecord(
                reason="commit_retry_exhausted", outcome=record.outcome, human_note=record.human_note, resume=record.resume
            )
            if record.resume:
                return "committed", outcome, escalation
            outcome.message = f"{outcome.message} A human confirmed this action did not commit."
            return "failed", outcome, escalation
        outcome.message = (
            f"{outcome.message} Verified after {attempt} attempt(s) that the transaction was not registered (this run's id "
            "never appeared in the history); it was retried once and did not go through."
        )
        return "failed", outcome, None

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
            return "committed", outcome, escalation
        outcome.message = f"{outcome.message} A human confirmed this action did not commit."
        return "failed", outcome, escalation

    outcome.message = (
        f"{outcome.message} Commit status could not be verified (the declared commit-verification "
        "check itself failed to complete). Do NOT retry this capability with the same params without "
        "manually confirming whether it already committed."
    )
    return "failed", outcome, None


def _risky_steps(artifact: Artifact) -> list:
    return [s for s in artifact.steps if s.risk == "confirm"]


def _needs_per_run_approval(artifact: Artifact) -> bool:
    """A reviewer approving an artifact authorizes its risky steps to run unattended. Any other
    artifact with a risky step gets the live human gate once per run."""
    return artifact.status != ArtifactStatus.APPROVED and bool(_risky_steps(artifact))


def _gate_risky_step(
    handoff: Optional[HandoffSession],
    artifact: Artifact,
    step,
    params: dict[str, str],
    escalations: list[EscalationRecord],
) -> Optional[FailureDetail]:
    """Replay's twin of discovery's gate: runs risky_action_confirm before the risky step. Returns
    None if approved, else the FailureDetail. No handoff fails closed."""
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
    """Replays an artifact with typed params. operator_factory supplies the human operator when a
    gate or escalation needs one. slow_mo_ms only slows a headed run."""
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

    outcome_signatures, outcome_source = _effective_business_outcomes(artifact)
    recoverables = _effective_recoverables(artifact)
    _reauths.n = 0
    step_timeout = artifact.step_timeout_ms or _ACTION_TIMEOUT_MS
    checkpoint_timeout = artifact.checkpoint_timeout_ms or _CHECKPOINT_TIMEOUT_MS
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

    max_attempts = 1 + _COMMIT_RETRY_ATTEMPTS

    def _attempt(attempt: int):
        """One run of the recorded steps in a fresh browser. Returns the result, or _RETRY when
        nothing was verified as registered and an attempt remains. Requests carry {run_id}-aN."""
        nonlocal steps_executed
        outputs.clear()
        run_tag = f"{run_id}-a{attempt}"
        with sync_playwright() as pw:
            # Only a run that can ask a person for something opens the loopback-only debugging port
            # (handoff/remote.py); a plain replay keeps the browser closed to everything but Playwright.
            may_ask_a_person = escalate_on_failure or needs_gate or operator_factory is not None
            remote_port = free_loopback_port() if (may_ask_a_person and remote_debugging_enabled()) else None
            browser = pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms, args=launch_args(remote_port) if remote_port else [])
            page = browser.new_page()
            last_response: dict = {}

            def _remember_document_response(response) -> None:
                try:
                    if response.request.resource_type == "document" and response.frame == page.main_frame:
                        last_response.update(status=response.status, url=response.url)
                except Exception:
                    pass

            page.on("response", _remember_document_response)
            # See agent/loop.py's matching header on the discovery side, and
            # mock_app/app.py for how the server surfaces/records this.
            page.set_extra_http_headers({"X-Automation-Source": "replay", "X-Run-Id": run_tag})
            handoff = None
            if escalate_on_failure or needs_gate:
                if operator_factory is not None:
                    operator = operator_factory(page)
                else:
                    # No custom operator: use an on-page banner if there's a real browser.
                    # capture=False keeps replay unable to record actions, so the banner only
                    # notifies.
                    gesture = GestureController(page) if (not headless or remote_port) else None
                    operator = (
                        _CliOperator(gesture=gesture, capture=False, headless=headless, remote_debug_port=remote_port)
                        if gesture is not None else None
                    )
                handoff = HandoffSession(page=page, run_id=run_id, evidence_dir=evidence_dir, operator=operator, remote_debug_port=remote_port)

            reached_confirm_step = False

            def _duplicate_result():
                """After a retried transaction succeeds, an earlier attempt's run id must not be in
                the history. If it is, the money moved twice, so never report success."""
                if attempt < 2 or artifact.commit_verification is None:
                    return None
                if not _earlier_attempt_committed(page, artifact, params, run_id, attempt):
                    return None
                risky = _risky_steps(artifact)
                detail = FailureDetail(
                    step_index=risky[0].index if risky else 0,
                    action="commit_verification",
                    expected="only the latest attempt's run id in the history",
                    observed=f"an earlier attempt's run id ({run_id}-a1) is also in the history",
                    message=(
                        "Possible duplicate: the transaction was retried after the check found nothing, but the earlier "
                        "attempt's write is now visible too. Do NOT treat this as a single successful transaction."
                    ),
                    observed_page=_observed_page(page, last_response),
                )
                logger.error("replay hard_failure (possible duplicate) capability=%s run_id=%s", artifact.capability_id, run_id)
                if escalate_on_failure and handoff is not None:
                    record = handoff.escalate(
                        InterventionRequest(
                            reason="commit_duplicate_suspected",
                            goal_or_capability=artifact.capability_id,
                            message=f"{detail.message} (attempts: {attempt}, run id {run_id})",
                            step_index=detail.step_index,
                        )
                    )
                    escalations.append(
                        EscalationRecord(reason="commit_duplicate_suspected", outcome=record.outcome, human_note=record.human_note, resume=record.resume)
                    )
                page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                dup = ReplayResult(
                    kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version, run_id=run_id,
                    outputs=outputs, failure=detail, steps_executed=steps_executed, strategy_log=strategy_log,
                    escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                )
                _write_evidence(evidence_dir, artifact, params, dup)
                return dup

            try:
                restarts = 0  # per attempt: a fresh browser starts with a fresh session
                step_index = 0
                while step_index < len(artifact.steps):
                    step = artifact.steps[step_index]
                    step_index += 1
                    recovered_name = None
                    pre_failure: Optional[_StepOutcome] = None  # set when this step cannot run at all
                    rc = _match_recoverable(page, recoverables, params)
                    if rc is not None:
                        if rc.restart_after_recovery and reached_confirm_step:
                            # The page is gone after a risky step ran: whether it committed is now the question, and the
                            # commit check below answers it (it signs in again first).
                            pre_failure = _StepOutcome(False, expected=f"the page for step {step.index}", observed=rc.name,
                                                       message=f"{rc.description} It happened after a risky step, so whether that step committed is not known yet.")
                        else:
                            try:
                                _execute_recovery(page, rc, params, allowlist)
                                recovered_name = rc.name
                                if rc.restart_after_recovery:
                                    if restarts >= _RECOVERY_RESTARTS:
                                        pre_failure = _StepOutcome(False, expected=f"the page for step {step.index}", observed=rc.name,
                                                                   message=f"{rc.name} happened again after recovering from it once; not trying again.")
                                    else:
                                        restarts += 1
                                        logger.warning("replay signed back in and restarts the recorded steps capability=%s run_id=%s condition=%s", artifact.capability_id, run_id, rc.name)
                                        outputs.clear()
                                        step_index = 0
                                        continue
                            except (_RecoveryError, ResolutionError, PlaywrightTimeoutError) as e:
                                pre_failure = _StepOutcome(False, expected=f"to recover from {rc.name}", observed=str(e)[:300],
                                                           message=f"Could not recover from {rc.name}: {e}")

                    bo = _match_business_outcome(page, outcome_signatures, params)
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
                            business_outcome_source=outcome_source[bo.name],
                            steps_executed=steps_executed,
                            strategy_log=strategy_log,
                            escalations=escalations, unattended_risky_steps=unattended_risky,
                            evidence_dir=str(evidence_dir),
                        )
                        page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                        _write_evidence(evidence_dir, artifact, params, result)
                        return result

                    if pre_failure is None and step.risk == "confirm":
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

                    if pre_failure is None:
                        outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name, step_timeout)
                        steps_executed += 1
                    else:
                        outcome = pre_failure

                    if not outcome.ok:
                        # If commit verification is declared, it owns failures at or after a risky
                        # step (retry once, then escalate). Asking a human first would skip the
                        # retry.
                        if escalate_on_failure and handoff is not None and not (
                            reached_confirm_step and artifact.commit_verification is not None
                        ):
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
                            # Bounded recovery: the human fixed something, so the same step gets
                            # exactly one more try. Their actions are never captured, so replay
                            # can't teach the artifact.
                            if record.resume:
                                outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name, step_timeout)

                    if not outcome.ok and reached_confirm_step and artifact.commit_verification is not None:
                        verdict, outcome, escalation = _resolve_ambiguous_commit(
                            page, artifact, step, params, outcome, handoff, escalate_on_failure,
                            run_tag=run_tag, attempt=attempt, max_attempts=max_attempts,
                        )
                        if escalation is not None:
                            escalations.append(escalation)
                        if verdict == "retry":
                            logger.warning(
                                "replay retrying the whole transaction (verified not registered) capability=%s run_id=%s attempt=%s",
                                artifact.capability_id, run_id, attempt,
                            )
                            return _RETRY
                        if verdict == "committed":
                            duplicate = _duplicate_result()
                            if duplicate is not None:
                                return duplicate
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
                            observed_page=_observed_page(page, last_response),
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

                    if step.checkpoint and not _poll_checkpoint(page, step.checkpoint, params, checkpoint_timeout):
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
                            observed_page=_observed_page(page, last_response),
                        )
                        result = ReplayResult(
                            kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version,
                            run_id=run_id, outputs=outputs, failure=failure, steps_executed=steps_executed,
                            strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                        )
                        page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                        _write_evidence(evidence_dir, artifact, params, result)
                        return result

                final_state, final_bo = _await_final_state(page, artifact, params, outcome_signatures, checkpoint_timeout)
                if final_state == "outcome":
                    logger.info("replay business_outcome capability=%s run_id=%s outcome=%s", artifact.capability_id, run_id, final_bo.name)
                    result = ReplayResult(
                        kind="business_outcome", capability_id=artifact.capability_id, version=artifact.version,
                        run_id=run_id, outputs=outputs, business_outcome_name=final_bo.name,
                        business_outcome_description=final_bo.description, business_outcome_source=outcome_source[final_bo.name], steps_executed=steps_executed,
                        strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                if final_state == "timeout":
                    logger.error("replay hard_failure (final checkpoint) capability=%s run_id=%s", artifact.capability_id, run_id)
                    failure = FailureDetail(
                        step_index=len(artifact.steps) - 1,
                        action="final_checkpoint",
                        expected=f"{artifact.final_checkpoint.kind.value}={_fmt(artifact.final_checkpoint.value, params)}",
                        observed=page.url,
                        message=f"All steps executed but the final checkpoint did not hold within {checkpoint_timeout} ms.",
                        observed_page=_observed_page(page, last_response),
                    )
                    result = ReplayResult(
                        kind="hard_failure", capability_id=artifact.capability_id, version=artifact.version,
                        run_id=run_id, outputs=outputs, failure=failure, steps_executed=steps_executed,
                        strategy_log=strategy_log, escalations=escalations, unattended_risky_steps=unattended_risky, evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                duplicate = _duplicate_result()
                if duplicate is not None:
                    return duplicate
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

    attempt = 1
    while True:
        result = _attempt(attempt)
        if result is not _RETRY:
            break
        attempt += 1
    reauths = getattr(_reauths, "n", 0)
    if attempt > 1 or reauths:
        result.commit_attempts = attempt
        result.session_reauths = reauths
        _write_evidence(evidence_dir, artifact, params, result)
    return result


def _run_step(page, step, params, allowlist: Allowlist, strategy_log: list[StrategyLogEntry], recovered_name, timeout_ms: Optional[int] = None) -> _StepOutcome:
    """Retries transient timeouts a few times with a short wait. A ResolutionError is not retried,
    since every candidate was already tried."""
    last: Optional[_StepOutcome] = None
    for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            return _run_step_once(page, step, params, allowlist, strategy_log, recovered_name, timeout_ms)
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


def _poll_checkpoint(page: Page, checkpoint: Checkpoint, params: dict[str, str], timeout_ms: int) -> bool:
    """A slow legacy app may render the success state a moment after the click returns, so look again until
    it holds or the time is up. One look is always made, even with a zero timeout."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if _check_checkpoint(page, checkpoint, params):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(_POLL_MS)


def _await_final_state(page: Page, artifact: Artifact, params: dict[str, str], signatures, timeout_ms: int):
    """Waits for whichever comes first: a declared business outcome, the final checkpoint, or the timeout.
    Returns ("outcome", signature), ("success", None) or ("timeout", None). An outcome wins a tie."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        signature = _match_business_outcome(page, signatures, params)
        if signature is not None:
            return "outcome", signature
        if _check_checkpoint(page, artifact.final_checkpoint, params):
            return "success", None
        if time.monotonic() >= deadline:
            return "timeout", None
        page.wait_for_timeout(_POLL_MS)


def _until_resolved(page: Page, resolve: Callable, timeout_ms: int):
    """Runs `resolve` until it stops raising ResolutionError or the time is up. A slow page may simply not
    have drawn the control yet; waiting is not a retry of a different action, and it still ends in failure."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            return resolve()
        except ResolutionError:
            if time.monotonic() >= deadline:
                raise
            page.wait_for_timeout(_POLL_MS)


def _settle(page: Page) -> None:
    """After a click, give in-flight requests a moment to finish. Legacy pages that poll forever never go
    quiet, so this is capped and its timeout is ignored."""
    try:
        page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
    except PlaywrightTimeoutError:
        pass


def _run_step_once(page, step, params, allowlist: Allowlist, strategy_log: list[StrategyLogEntry], recovered_name, timeout_ms: Optional[int] = None) -> _StepOutcome:
    timeout_ms = timeout_ms or _ACTION_TIMEOUT_MS
    try:
        if step.action == ActionType.NAVIGATE:
            url = _fmt(step.value_template, params)
            if not allowlist.url_allowed(url):
                logger.warning("blocked by allowlist: navigate url=%s", url)
                return _StepOutcome(False, expected="navigate within allowlist", observed=url, message="Blocked by allowlist policy.")
            page.goto(url, wait_until="load")
            strategy_log.append(StrategyLogEntry(step_index=step.index, action=step.action.value, recovered_condition=recovered_name))
            return _StepOutcome(True)

        if step.action == ActionType.READ_TEXT:
            # Different success test here: a locator can resolve without extracting anything useful.
            # If no candidate gives non-empty text, raise ResolutionError.
            def _read():
                scope = resolve_frame_chain(page, step.target.frame_chain, params)
                return resolve_text(scope, step.target, params, timeout_ms=min(timeout_ms, 1500))

            text, strategy_used = _until_resolved(page, _read, timeout_ms)
            strategy_log.append(
                StrategyLogEntry(step_index=step.index, action=step.action.value, strategy_used=strategy_used, recovered_condition=recovered_name)
            )
            if not allowlist.action_allowed(step.action.value):
                logger.warning("blocked by allowlist: action=%s", step.action.value)
                return _StepOutcome(False, expected="action within allowlist", observed=step.action.value, message="Blocked by allowlist policy.")
            out = _StepOutcome(True)
            out.observed = text
            return out

        def _find():
            scope = resolve_frame_chain(page, step.target.frame_chain, params)
            return resolve_target(scope, step.target, params)

        resolved = _until_resolved(page, _find, timeout_ms)
        strategy_log.append(
            StrategyLogEntry(step_index=step.index, action=step.action.value, strategy_used=resolved.strategy, recovered_condition=recovered_name)
        )

        if not allowlist.action_allowed(step.action.value):
            logger.warning("blocked by allowlist: action=%s", step.action.value)
            return _StepOutcome(False, expected="action within allowlist", observed=step.action.value, message="Blocked by allowlist policy.")

        if step.action == ActionType.CLICK:
            if resolved.locator is not None:
                resolved.locator.click(timeout=timeout_ms)
            else:
                page.mouse.click(resolved.coordinates["x"], resolved.coordinates["y"])
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            _settle(page)
            return _StepOutcome(True)

        if step.action == ActionType.FILL:
            value = _fmt(step.value_template, params) or ""
            if resolved.locator is None:
                return _StepOutcome(False, expected="fillable element", observed="coordinates-only match", message="The recorded field was not found on the current page (only the coordinates fallback matched, which cannot fill).")
            resolved.locator.fill(value, timeout=timeout_ms)
            return _StepOutcome(True)

        if step.action == ActionType.SELECT:
            value = _fmt(step.value_template, params) or ""
            if resolved.locator is None:
                return _StepOutcome(False, expected="selectable element", observed="coordinates-only match", message="The recorded dropdown was not found on the current page (only the coordinates fallback matched, which cannot select).")
            resolved.locator.select_option(value, timeout=timeout_ms)
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
    # Redact by field name in the saved copy only. The real params already drove the browser, and
    # evidence is audit-only, so a plain [REDACTED] is safe.
    safe_params = {name: redact_value(name, value) for name, value in params.items()}
    (evidence_dir / "replay_input.json").write_text(
        json.dumps({"capability_id": artifact.capability_id, "version": artifact.version, "params": safe_params}, indent=2)
    )
