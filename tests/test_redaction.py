"""Sensitive-data handling across the two places raw text can otherwise leak:
into a *reusable, persisted artifact* (artifact/recorder.py) and into
*evidence/logs* (agent/loop.py, replay/executor.py). These are deliberately
different remedies for a reason stated in each fix's own comment: an artifact
locator candidate that gets blindly replaced with "[REDACTED]" would silently
break replay forever, so recorder.py *drops* a sensitive candidate instead
(keeping the always-present css_path/coordinates fallback); evidence is
audit-only and never re-resolved, so a plain text substitution there is safe
and sufficient. No banking-specific logic anywhere in this file or in the
code it tests — the fixture is a generic "field that looks like a secret",
reusing guardrails/redact.py's existing, generic patterns.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from agent.executor import StepLog
from agent.loop import DiscoveryResult, _redact_for_evidence
from artifact.recorder import record_artifact
from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    ParamType,
    Step,
    Target,
    TargetApp,
)
from guardrails.redact import REDACTED
from replay.executor import replay_artifact

SSN = "123-45-6789"
BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


def _sensitive_read_target(text: str) -> Target:
    return Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "text", "name": text}),
            LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": text}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table td b"}),
            LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 1, "y": 2}),
        ]
    )


# -- artifact-side: drop, never fake-redact a locator ------------------------


def test_record_artifact_drops_sensitive_locator_candidates_but_keeps_css_fallback():
    """A read_text step whose observed value is PII-shaped must not embed that
    raw value as a ROLE_NAME/TEXT locator candidate in the persisted artifact
    -- but must still be resolvable on replay via css_path/coordinates.
    """
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(
            index=1, action="read_text", rationale="read the SSN on file",
            ref="f0e0", element_name=SSN, target=_sensitive_read_target(SSN), ok=True,
        ),
    ]
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done",
        outputs={"ssn_on_file": SSN},
        steps=steps, goal="read ssn", target_url="http://127.0.0.1:8000/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(result, "some-capability", "desc", {}, target_app)

    read_step = next(s for s in artifact.steps if s.action == ActionType.READ_TEXT)
    strategies = {c.strategy for c in read_step.target.candidates}
    assert strategies == {LocatorStrategy.CSS_PATH, LocatorStrategy.COORDINATES}, (
        "sensitive role_name/text candidates must be dropped, not replaced with a fake locator"
    )
    assert any(c.strategy == LocatorStrategy.CSS_PATH for c in read_step.target.candidates), "a resolvable fallback must remain"
    assert SSN not in artifact.model_dump_json(), "the raw SSN must not appear anywhere in the persisted artifact"


def test_record_artifact_redacts_rationale_text():
    """The LLM's own free-text rationale can echo an observed value back
    verbatim; that free text (Step.description) is display-only, so a plain
    substitution is fine here (unlike a locator candidate).
    """
    steps = [
        StepLog(
            index=0, action="navigate", rationale=f"Reading the member's SSN {SSN} to verify identity",
            url_before="", url_after="http://127.0.0.1:8000/", ok=True,
        ),
    ]
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs={},
        steps=steps, goal="g", target_url="http://127.0.0.1:8000/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(result, "some-capability", "desc", {}, target_app)

    assert SSN not in artifact.steps[0].description
    assert REDACTED in artifact.steps[0].description


def _named_target(name: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "textbox", "name": name})])


def test_record_artifact_still_declares_typed_inputs_normally():
    """Redaction must not touch legitimate, declared, typed contract values --
    only incidental page text. member_id is business data, not PII, and must
    keep flowing through exactly as before.
    """
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_named_target("e.g. 10001"), ok=True),
    ]
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs={},
        steps=steps, goal="look up member 10001", target_url="http://127.0.0.1:8000/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(result, "lookup-member", "desc", {"member_id": "10001"}, target_app)

    assert {i.name for i in artifact.inputs} == {"member_id"}
    assert artifact.steps[1].value_template == "{member_id}"


# -- evidence-side: plain substitution is fine, never re-resolved -----------


def test_redact_for_evidence_recursively_scrubs_nested_strings():
    payload = {
        "rationale": f"saw ssn {SSN}",
        "target": {"candidates": [{"value": {"text": SSN}}]},
        "nested_list": [SSN, {"x": SSN}],
        "harmless": "SA-5035",
    }
    redacted = _redact_for_evidence(payload)
    dumped = json.dumps(redacted)
    assert SSN not in dumped
    assert "SA-5035" in dumped, "non-sensitive text must survive untouched"


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_replay_evidence_redacts_sensitive_named_param_but_still_executes_with_real_value():
    """The written replay_input.json evidence must redact a sensitive-named
    param by field name; the actual replay run must still use the real value
    internally (proven here by the run completing successfully at all).
    """
    artifact = Artifact(
        capability_id="redaction-demo",
        version="1.0.0",
        description="Minimal artifact for evidence-redaction testing.",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[InputParam(name="account_number", type=ParamType.STRING, description="sensitive-named, declared but unused by any step")],
        outputs=[],
        steps=[Step(index=0, action=ActionType.NAVIGATE, description="go", value_template=f"{BASE}/accounts")],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/accounts"),
        created_from_run_id="hand_built_for_tests",
    )
    result = replay_artifact(artifact, {"account_number": "4111111111111111"}, headless=True)

    assert result.kind == "success"
    written = json.loads((Path(result.evidence_dir) / "replay_input.json").read_text())
    assert written["params"]["account_number"] == REDACTED
