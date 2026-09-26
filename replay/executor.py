"""Deterministic replay: the production execution path. No LLM in the loop —
every action comes straight from the artifact's recorded steps and the
caller's input params, and nothing here is allowed to change that.

Before each step, the engine checks the artifact's declared
recoverable_conditions and business_outcomes against the *current* page
state (see artifact/schema.py for why these are first-class, reviewable
parts of the artifact rather than ad hoc code):

  1. A matching recoverable condition gets exactly one bounded recovery
     action (e.g. dismiss a known interstitial), then execution continues
     into this same step as normal.
  2. A matching business outcome ends the run immediately as `business_outcome`
     — a legitimate result, not a crash.
  3. Otherwise the step's own target is resolved via the ranked locator
     fallback chain and executed, with one further bounded distinction inside
     _run_step: a Playwright *timeout* (the page/app being transiently slow —
     "environment failure", requirement 4) gets up to _TRANSIENT_RETRY_ATTEMPTS
     attempts with a short wait between; a locator that never resolves at all
     (requirement 5 — the artifact's recorded targets don't match this page)
     does not retry, because trying the same exhausted candidates again isn't
     going to find a different answer. Either way, whatever survives becomes a
     structured `hard_failure` naming the step, what was expected, and what
     was actually observed — never a reason to call the LLM.

Replay HITL boundary (requirement 6), stated plainly: `escalate_on_failure`
notifies a human and, if they report having fixed something operational (a
stuck dialog, a slow backend), allows exactly one bounded retry of the
*same* recorded step — never a different one, never open-ended. It is not
discovery. No GestureController is ever constructed here, so there is no
structured action capture on this path at all (see handoff/session.py's
DISCOVERY_HITL_REASONS — 'replay_hard_failure' is deliberately excluded, and
HandoffSession enforces that by dropping any captured_actions regardless).
A replay run can end in success, business_outcome, or hard_failure; it can
never come out the other side with a changed artifact. Teaching a capability
a new procedure is what discovery is for.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from artifact.schema import (
    ActionType,
    Artifact,
    BusinessOutcomeSignature,
    Checkpoint,
    CheckpointKind,
    RecoverableCondition,
)
from guardrails.allowlist import Allowlist
from guardrails.policy import classify_action
from guardrails.redact import redact_value
from handoff.session import HandoffSession, InterventionRequest
from replay.locators import ResolutionError, resolve_frame_chain, resolve_target
from replay.outcomes import FailureDetail, ReplayResult, StrategyLogEntry

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"
_TRANSIENT_RETRY_ATTEMPTS = 2  # total attempts on a Playwright timeout, i.e. one retry
_TRANSIENT_RETRY_WAIT_MS = 500
_ACTION_TIMEOUT_MS = 5000  # per-attempt Playwright actionability timeout; module-level so
                           # tests can shorten it rather than waiting out a real 5s timeout


def _fmt(value: Optional[str], params: dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.format(**params)
    except (KeyError, IndexError):
        return value


def _run_id() -> str:
    """Second-granularity timestamp plus a short random suffix. The timestamp
    alone is not enough: two concurrent replay invocations of the same
    capability starting within the same wall-clock second would otherwise get
    the identical run_id, hence the identical evidence_dir path, and race
    writing replay_result.json/replay_input.json/screenshots into the same
    files. This is the one actual shared-mutable-state hazard in this module
    (everything else -- run_id, evidence_dir, browser, page, outputs,
    strategy_log -- is already a per-call local, verified by inspection);
    fixing it needs no locks or infrastructure, just a collision-resistant id.
    """
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
            scope = resolve_frame_chain(page, checkpoint.target.frame_chain)
            resolved = resolve_target(scope, checkpoint.target)
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
    scope = resolve_frame_chain(page, cond.recovery_target.frame_chain)
    resolved = resolve_target(scope, cond.recovery_target)
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


def replay_artifact(
    artifact: Artifact,
    params: dict[str, str],
    headless: bool = True,
    escalate_on_failure: bool = False,
) -> ReplayResult:
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

    outputs: dict[str, str] = {}
    strategy_log: list[StrategyLogEntry] = []
    steps_executed = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        # See agent/loop.py's matching header on the discovery side, and
        # mock_app/app.py for how the server surfaces/logs/records this.
        page.set_extra_http_headers({"X-Automation-Source": "replay", "X-Run-Id": run_id})
        handoff = HandoffSession(page=page, run_id=run_id, evidence_dir=evidence_dir) if escalate_on_failure else None

        try:
            for step in artifact.steps:
                recovered_name = None
                rc = _match_recoverable(page, artifact.recoverable_conditions, params)
                if rc is not None:
                    _execute_recovery(page, rc, params)
                    recovered_name = rc.name

                bo = _match_business_outcome(page, artifact.business_outcomes, params)
                if bo is not None:
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
                        evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name)
                steps_executed += 1

                if not outcome.ok:
                    if escalate_on_failure and handoff is not None:
                        record = handoff.escalate(
                            InterventionRequest(
                                reason="replay_hard_failure",
                                goal_or_capability=artifact.capability_id,
                                message=f"Step {step.index} ({step.action.value}) failed: {outcome.message}",
                                step_index=step.index,
                            )
                        )
                        # Bounded operational recovery ONLY (requirement 6) — the human
                        # reported having fixed something in the environment (an
                        # unexpected system dialog, a slow backend), so the *same*
                        # recorded step gets exactly one more attempt. This is never a
                        # do-over of the whole artifact and never a second retry if this
                        # also fails: record.resume was previously read and discarded
                        # entirely, silently turning every escalation into a notification
                        # with no actual recovery effect — fixed here, not by adding a
                        # new mechanism. Whatever the human did (if anything) is not
                        # captured as a step: HandoffSession.escalate() already drops
                        # captured_actions for 'replay_hard_failure' unconditionally
                        # (see DISCOVERY_HITL_REASONS) — replay cannot teach the artifact,
                        # by construction, not by this call site remembering to behave.
                        if record.resume:
                            outcome = _run_step(page, step, params, allowlist, strategy_log, recovered_name)

                if not outcome.ok:
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
                        evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

                if step.extract_as and outcome.observed:
                    outputs[step.extract_as] = outcome.observed

                if step.checkpoint and not _check_checkpoint(page, step.checkpoint, params):
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
                        strategy_log=strategy_log, evidence_dir=str(evidence_dir),
                    )
                    page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                    _write_evidence(evidence_dir, artifact, params, result)
                    return result

            final_bo = _match_business_outcome(page, artifact.business_outcomes, params)
            if final_bo is not None:
                result = ReplayResult(
                    kind="business_outcome", capability_id=artifact.capability_id, version=artifact.version,
                    run_id=run_id, outputs=outputs, business_outcome_name=final_bo.name,
                    business_outcome_description=final_bo.description, steps_executed=steps_executed,
                    strategy_log=strategy_log, evidence_dir=str(evidence_dir),
                )
                page.screenshot(path=str(evidence_dir / "business_outcome.png"))
                _write_evidence(evidence_dir, artifact, params, result)
                return result

            if not _check_checkpoint(page, artifact.final_checkpoint, params):
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
                    strategy_log=strategy_log, evidence_dir=str(evidence_dir),
                )
                page.screenshot(path=str(evidence_dir / "hard_failure.png"))
                _write_evidence(evidence_dir, artifact, params, result)
                return result

            page.screenshot(path=str(evidence_dir / "success.png"))
            result = ReplayResult(
                kind="success", capability_id=artifact.capability_id, version=artifact.version, run_id=run_id,
                outputs=outputs, steps_executed=steps_executed, strategy_log=strategy_log,
                evidence_dir=str(evidence_dir),
            )
            _write_evidence(evidence_dir, artifact, params, result)
            return result
        finally:
            browser.close()


def _run_step(page, step, params, allowlist: Allowlist, strategy_log: list[StrategyLogEntry], recovered_name) -> _StepOutcome:
    """Bounded retry for transient environment failures (requirement 4) —
    a Playwright timeout (page/app slow, not permanently broken) gets up to
    _TRANSIENT_RETRY_ATTEMPTS total tries with a short wait between. A
    ResolutionError (requirement 5 — the artifact's target genuinely doesn't
    match this page) is NOT retried here: _run_step_once already exhausts
    every ranked locator candidate in one call, so calling it again with the
    same artifact and the same page would just fail the same way. Retrying
    that would look like recovery but isn't — it'd just be burning time
    before reporting the same structural mismatch.
    """
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
                return _StepOutcome(False, expected="navigate within allowlist", observed=url, message="Blocked by allowlist policy.")
            page.goto(url, wait_until="load")
            strategy_log.append(StrategyLogEntry(step_index=step.index, action=step.action.value, recovered_condition=recovered_name))
            return _StepOutcome(True)

        scope = resolve_frame_chain(page, step.target.frame_chain)
        resolved = resolve_target(scope, step.target)
        strategy_log.append(
            StrategyLogEntry(step_index=step.index, action=step.action.value, strategy_used=resolved.strategy, recovered_condition=recovered_name)
        )

        if not allowlist.action_allowed(step.action.value):
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

        if step.action == ActionType.READ_TEXT:
            text = resolved.locator.inner_text(timeout=_ACTION_TIMEOUT_MS) if resolved.locator is not None else ""
            out = _StepOutcome(True)
            out.observed = text
            return out

        return _StepOutcome(False, expected="known action type", observed=step.action.value, message="Unhandled action type.")

    except ResolutionError as e:
        return _StepOutcome(False, expected=f"one of the recorded locator candidates for step {step.index}", observed=str(e), message="No locator candidate resolved.")
    except PlaywrightTimeoutError:
        # Deliberately NOT caught-and-converted here — let it propagate to _run_step,
        # which decides whether a bounded retry is warranted (requirement 4). Catching
        # it here would turn every timeout into an immediate hard_failure with no
        # chance for the "just slow, not broken" case to recover.
        raise
    except Exception as e:  # noqa: BLE001 - any other Playwright/navigation error becomes a structured hard failure
        return _StepOutcome(False, expected=f"{step.action.value} to succeed", observed=str(e), message="Unhandled runtime error during step execution.")


def _write_evidence(evidence_dir: Path, artifact: Artifact, params: dict[str, str], result: ReplayResult) -> None:
    (evidence_dir / "replay_result.json").write_text(result.model_dump_json(indent=2))
    # Redact by field name for the *written copy* only — the real `params` dict
    # (unredacted) is what already drove Playwright above; evidence is audit-only
    # and never re-resolved, so a plain [REDACTED] substitution here is safe
    # (contrast with artifact/recorder.py's _scrub_target, which must preserve
    # replayability and so drops rather than substitutes).
    safe_params = {name: redact_value(name, value) for name, value in params.items()}
    (evidence_dir / "replay_input.json").write_text(
        json.dumps({"capability_id": artifact.capability_id, "version": artifact.version, "params": safe_params}, indent=2)
    )
