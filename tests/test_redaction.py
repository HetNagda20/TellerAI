"""Sensitive data must not leak into artifacts or evidence. The recorder drops a sensitive
candidate, since a fake one would break replay. Evidence just substitutes, since it is audit-
only."""

from __future__ import annotations

import json
from pathlib import Path

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
from handoff.session import _redact_action
from guardrails.redact import REDACTED
from replay.executor import replay_artifact

SSN = "123-45-6789"
APP = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")


def _record(steps, outputs=None, params=None):
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs=outputs or {},
        steps=steps, goal="g", target_url="http://127.0.0.1:8000/", started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    return record_artifact(result, "some-capability", "desc", params or {}, APP)


def _nav(rationale="start"):
    return StepLog(index=0, action="navigate", rationale=rationale, url_before="", url_after="http://127.0.0.1:8000/", ok=True)


def test_the_persisted_artifact_never_carries_sensitive_text_but_keeps_declared_inputs():
    sensitive_target = Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "text", "name": SSN}),
            LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": SSN}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "table td b"}),
            LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 1, "y": 2}),
        ]
    )
    read = StepLog(index=1, action="read_text", rationale="read the SSN on file", ref="f0e0", element_name=SSN, target=sensitive_target, ok=True)
    artifact = _record([_nav(), read], outputs={"ssn_on_file": SSN})

    # a sensitive locator candidate is dropped, not faked, and a resolvable fallback remains
    read_step = next(s for s in artifact.steps if s.action == ActionType.READ_TEXT)
    assert {c.strategy for c in read_step.target.candidates} == {LocatorStrategy.CSS_PATH, LocatorStrategy.COORDINATES}
    assert SSN not in artifact.model_dump_json()

    # the model's free-text rationale can echo an observed value; it is display-only, so it is substituted
    echoed = _record([_nav(rationale=f"Reading the member's SSN {SSN} to verify identity")])
    assert SSN not in echoed.steps[0].description and REDACTED in echoed.steps[0].description

    # redaction must not touch legitimate, declared, typed contract values (business data, not PII)
    id_box = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "textbox", "name": "e.g. 10001"})])
    fill = StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=id_box, ok=True)
    declared = _record([_nav(), fill], params={"member_id": "10001"})
    assert {i.name for i in declared.inputs} == {"member_id"} and declared.steps[1].value_template == "{member_id}"


def test_evidence_is_redacted_recursively_and_a_sensitive_param_still_executes_with_its_real_value(base_url):
    payload = {
        "rationale": f"saw ssn {SSN}",
        "target": {"candidates": [{"value": {"text": SSN}}]},
        "nested_list": [SSN, {"x": SSN}],
        "harmless": "SA-5035",
    }
    dumped = json.dumps(_redact_for_evidence(payload))
    assert SSN not in dumped and "SA-5035" in dumped  # non-sensitive text survives untouched

    artifact = Artifact(
        capability_id="redaction-demo",
        version="1.0.0",
        description="Minimal artifact for evidence-redaction testing.",
        goal_template="n/a",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=base_url, entry_path="/"),
        inputs=[
            InputParam(name="account_number", type=ParamType.STRING, description="sensitive-named, declared but unused by any step"),
            InputParam(name="mailing_address", type=ParamType.STRING, description="an address-shaped value, declared but unused by any step"),
        ],
        outputs=[],
        steps=[Step(index=0, action=ActionType.NAVIGATE, description="go", value_template=f"{base_url}/accounts")],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/accounts"),
        created_from_run_id="hand_built_for_tests",
    )
    result = replay_artifact(artifact, {"account_number": "4111111111111111", "mailing_address": "555 W Washington blvd"}, headless=True)

    assert result.kind == "success"  # the run used the real value internally
    written = json.loads((Path(result.evidence_dir) / "replay_input.json").read_text())
    assert written["params"]["mailing_address"] == REDACTED  # a street address is scrubbed by its shape
    assert written["params"]["account_number"] == REDACTED  # the evidence on disk did not


    # what a human typed during a takeover is redacted by the field's name before it reaches the handoff log
    typed = _redact_action({"action": "fill", "descriptor": {"name": "Password"}, "value": "hunter2"})
    assert typed["value"] == REDACTED
    assert _redact_action({"action": "click", "descriptor": {"name": "Go"}, "value": None})["value"] is None
