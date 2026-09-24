from agent.executor import StepLog
from agent.loop import DiscoveryResult
from artifact.recorder import _templatize, record_artifact
from artifact.schema import ActionType, LocatorCandidate, LocatorStrategy, Target, TargetApp


def test_templatize_replaces_declared_param_values():
    out = _templatize("http://127.0.0.1:8000/member/10001", {"member_id": "10001"})
    assert out == "http://127.0.0.1:8000/member/{member_id}"


def test_templatize_leaves_unmatched_text_alone():
    out = _templatize("http://127.0.0.1:8000/", {"member_id": "10001"})
    assert out == "http://127.0.0.1:8000/"


def _target(name="Search") -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": name})])


def test_record_artifact_parameterizes_fill_and_drops_uncorrelated_reads():
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_target("e.g. 10001"), ok=True),
        StepLog(index=2, action="click", rationale="search", ref="f0e1", element_name="Search", target=_target("Search"), ok=True),
        StepLog(index=3, action="read_text", rationale="check balance, not a declared output", ref="f0e2", element_name="$812.44", target=_target("$812.44"), ok=True),
    ]
    result = DiscoveryResult(
        run_id="discovery_test",
        success=True,
        outcome="done",
        summary="done",
        outputs={},
        steps=steps,
        goal="look up member 10001",
        target_url="http://127.0.0.1:8000/",
        started_at="t0",
        ended_at="t1",
        evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(result, "lookup-member", "desc", {"member_id": "10001"}, target_app)

    fill_step = next(s for s in artifact.steps if s.action == ActionType.FILL)
    assert fill_step.value_template == "{member_id}"
    # the read_text step didn't match any declared output, so it's dropped from the artifact
    assert all(s.action != ActionType.READ_TEXT for s in artifact.steps)


def test_record_artifact_drops_unused_declared_params_and_unresolved_outputs():
    # Regression: a real run declared `account_type` as a required input (it was
    # passed via --param) even though the agent left that dropdown at its default
    # and no step ever templated {account_type} in — and separately reported extra
    # "outputs" that were never actually read off the page via read_text. Both used
    # to end up in the artifact's contract, which would have silently misrepresented
    # what replay can actually control or produce.
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_target("e.g. 10001"), ok=True),
        StepLog(index=2, action="click", rationale="search", ref="f0e1", element_name="Search", target=_target("Search"), ok=True),
        # note: no "select" step touches account_type at all — its default already matched the goal
    ]
    result = DiscoveryResult(
        run_id="discovery_test",
        success=True,
        outcome="done",
        summary="done",
        # the model echoed member_id and a made-up "status" back as outputs, but only
        # ever actually *read* nothing from the page in this trimmed step log
        outputs={"member_id": "10001", "status": "ok"},
        steps=steps,
        goal="do the thing",
        target_url="http://127.0.0.1:8000/",
        started_at="t0",
        ended_at="t1",
        evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(
        result, "some-capability", "desc", {"member_id": "10001", "account_type": "savings"}, target_app
    )

    input_names = {i.name for i in artifact.inputs}
    assert input_names == {"member_id"}, "account_type was never used by any step and must not be declared"
    assert artifact.outputs == [], "outputs with no matching read step must not be declared"


def test_record_artifact_rejects_failed_run():
    result = DiscoveryResult(
        run_id="x", success=False, outcome="give_up", summary="stuck", outputs={}, steps=[],
        goal="g", target_url="http://x", started_at="t0", ended_at="t1", evidence_dir="/tmp/x",
    )
    target_app = TargetApp(app_id="a", base_url="http://x", entry_path="/")
    try:
        record_artifact(result, "cap", "desc", {}, target_app)
        assert False, "expected ValueError"
    except ValueError:
        pass
