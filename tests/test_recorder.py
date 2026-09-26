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


def _target_with_css(name: str, css: str) -> Target:
    """A target whose role/name candidate is deliberately non-distinguishing
    (mirrors two same-shaped <select> elements sharing an accessible name),
    but whose css_path candidate is positionally distinct -- exactly the shape
    replay/locators.py and artifact/recorder.py actually see for two
    different controls on the same legacy form.
    """
    return Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": name}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css}),
        ]
    )


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


def test_record_artifact_parameterizes_select_and_fill_together():
    """Regression: the discovery run that produced the original
    open-member-subaccount@1.0.0 artifact never demonstrated account_type or
    initial_deposit as controllable (no --param value it was given ever
    literally matched what the agent typed/selected), so the recorder
    correctly — per its own documented contract — left the select hardcoded
    to a literal and dropped both from `inputs`. That left a capability whose
    CLI usage advertised account_type/initial_deposit as params while the
    artifact could never actually honor them (see PROJECT_STATE.md). This
    proves the other side of that same contract: when a discovery run DOES
    demonstrate a param on both a select and a fill step, both must end up
    declared and templated, not just the first one recorder happens to check.
    """
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_target("e.g. 10001"), ok=True),
        StepLog(index=2, action="click", rationale="search", ref="f0e1", element_name="Search", target=_target("Search"), ok=True),
        StepLog(index=3, action="select", rationale="choose account type", ref="f0e2", element_name="Savings\nChecking", value="Savings", target=_target("Savings\nChecking"), ok=True),
        StepLog(index=4, action="fill", rationale="enter deposit", ref="f0e3", element_name="Deposit", value="25", target=_target("Deposit"), ok=True),
    ]
    result = DiscoveryResult(
        run_id="discovery_test",
        success=True,
        outcome="done",
        summary="done",
        outputs={},
        steps=steps,
        goal="open a savings sub-account with a 25 deposit",
        target_url="http://127.0.0.1:8000/",
        started_at="t0",
        ended_at="t1",
        evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(
        result,
        "open-member-subaccount",
        "desc",
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "25"},
        target_app,
    )

    input_names = {i.name for i in artifact.inputs}
    assert input_names == {"member_id", "account_type", "initial_deposit"}

    select_step = next(s for s in artifact.steps if s.action == ActionType.SELECT)
    fill_steps = [s for s in artifact.steps if s.action == ActionType.FILL]
    assert select_step.value_template == "{account_type}"
    assert any(s.value_template == "{initial_deposit}" for s in fill_steps)


def test_record_artifact_distinguishes_identical_values_on_different_controls():
    """Regression for the transfer-funds@1.0.0 bug (see PROJECT_STATE.md): from_account
    and to_account were both demonstrated as "Checking" -- two DIFFERENT controls
    sharing the IDENTICAL value. The old value-only _templatize matching collapsed
    both onto whichever declared param it happened to check first (from_account),
    leaving to_account undeclared entirely and its select never actually distinctly
    templated. The generic fix keys parameterization off (value, control identity)
    via each step's own css_path candidate, not value alone -- no transfer-specific
    logic anywhere in this test or in the recorder.
    """
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(index=1, action="fill", rationale="enter member id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_target("e.g. 10001"), ok=True),
        StepLog(
            index=2, action="select", rationale="choose source account",
            ref="f0e1", element_name="Checking\nSavings", value="Checking",
            target=_target_with_css("Checking\nSavings", "tr:nth-of-type(1) > td:nth-of-type(2) > select"), ok=True,
        ),
        StepLog(
            index=3, action="select", rationale="choose destination account",
            ref="f0e2", element_name="Checking\nSavings", value="Checking",
            target=_target_with_css("Checking\nSavings", "tr:nth-of-type(3) > td:nth-of-type(2) > select"), ok=True,
        ),
    ]
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs={},
        steps=steps, goal="transfer checking to checking", target_url="http://127.0.0.1:8000/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(
        result, "transfer-funds", "desc",
        {"member_id": "10001", "from_account": "Checking", "to_account": "Checking"},
        target_app,
    )

    input_names = {i.name for i in artifact.inputs}
    assert input_names == {"member_id", "from_account", "to_account"}, (
        "both from_account and to_account must be declared even though they share the same demonstrated value"
    )

    select_steps = [s for s in artifact.steps if s.action == ActionType.SELECT]
    assert len(select_steps) == 2
    templates = {s.value_template for s in select_steps}
    assert templates == {"{from_account}", "{to_account}"}, f"expected each select bound to its OWN param, got {templates}"


def test_record_artifact_reuses_same_param_when_same_control_revisited():
    """The same control acted on twice (e.g. re-set after a page refresh) must
    keep mapping to the same declared param both times, not consume a second
    one -- preserves the existing multi-step-reuse behavior while fixing the
    identical-value-different-control case above.
    """
    steps = [
        StepLog(index=0, action="navigate", rationale="start", url_before="", url_after="http://127.0.0.1:8000/", ok=True),
        StepLog(
            index=1, action="select", rationale="choose account type",
            ref="f0e0", element_name="Checking\nSavings", value="Checking",
            target=_target_with_css("Checking\nSavings", "tr:nth-of-type(1) > select"), ok=True,
        ),
        StepLog(
            index=2, action="select", rationale="re-confirm account type after a page refresh",
            ref="f0e1", element_name="Checking\nSavings", value="Checking",
            target=_target_with_css("Checking\nSavings", "tr:nth-of-type(1) > select"), ok=True,
        ),
    ]
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs={},
        steps=steps, goal="set account type twice", target_url="http://127.0.0.1:8000/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    target_app = TargetApp(app_id="cu-servicing-console", base_url="http://127.0.0.1:8000", entry_path="/")
    artifact = record_artifact(result, "some-capability", "desc", {"account_type": "Checking"}, target_app)

    input_names = {i.name for i in artifact.inputs}
    assert input_names == {"account_type"}
    select_steps = [s for s in artifact.steps if s.action == ActionType.SELECT]
    assert [s.value_template for s in select_steps] == ["{account_type}", "{account_type}"]


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
