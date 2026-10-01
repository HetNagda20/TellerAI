"""Turning a discovery step log into a reusable, parameterized artifact. Pure logic, no browser,
hand-built StepLogs, and nothing specific to one workflow."""

import pytest

from agent.executor import StepLog
from agent.loop import DiscoveryResult
from artifact.grounding import templatize_goal
from artifact.recorder import _templatize, _templatize_candidate, _templatize_target, record_artifact
from artifact.schema import ActionType, LocatorCandidate, LocatorStrategy, Target, TargetApp

BASE = "http://127.0.0.1:8000"
APP = TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/")


def _target(name="Search") -> Target:
    return Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "button", "name": name})])


def _target_with_css(name: str, css: str) -> Target:
    """Role and name are shared, like two same-shaped selects, but the css_path is distinct. That is
    what the recorder sees for two controls on one form."""
    return Target(
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value={"role": "combobox", "name": name}),
            LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css}),
        ]
    )


def _nav() -> StepLog:
    return StepLog(index=0, action="navigate", rationale="start", url_before="", url_after=BASE + "/", ok=True)


def _select(index, value, css, rationale="choose", options=None):
    return StepLog(index=index, action="select", rationale=rationale, ref=f"f0e{index}", element_name="Account:", value=value, target=_target_with_css("Account:", css), ok=True, options=options)


def _record(steps, params, outputs=None):
    result = DiscoveryResult(
        run_id="discovery_test", success=True, outcome="done", summary="done", outputs=outputs or {}, steps=steps,
        goal="g", target_url=BASE + "/", started_at="t0", ended_at="t1", evidence_dir="/tmp/evidence_test",
    )
    return record_artifact(result, "some-capability", "desc", params, APP)


def _selects(artifact):
    return [s for s in artifact.steps if s.action == ActionType.SELECT]


def test_declared_values_become_templates_and_the_contract_only_promises_what_steps_use():
    assert _templatize("http://127.0.0.1:8000/member/10001", {"member_id": "10001"}) == "http://127.0.0.1:8000/member/{member_id}"
    assert _templatize(BASE + "/", {"member_id": "10001"}) == BASE + "/"

    steps = [
        _nav(),
        StepLog(index=1, action="fill", rationale="enter id", ref="f0e0", element_name="e.g. 10001", value="10001", target=_target("e.g. 10001"), ok=True),
        StepLog(index=2, action="click", rationale="search", ref="f0e1", element_name="Search", target=_target("Search"), ok=True),
        _select(3, "Savings", "tr:nth-of-type(1) > select", "choose account type", options=["Savings", "Checking"]),
        StepLog(index=4, action="fill", rationale="enter deposit", ref="f0e3", element_name="Deposit", value="25", target=_target("Deposit"), ok=True),
        StepLog(index=5, action="read_text", rationale="check a balance, not a declared output", ref="f0e4", element_name="$812.44", target=_target("$812.44"), ok=True),
    ]
    artifact = _record(
        steps,
        # "funding" is declared on the CLI but no step ever uses it; "status" is echoed as an output but never read
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "25", "funding": "checking"},
        outputs={"status": "ok"},
    )

    assert {i.name for i in artifact.inputs} == {"member_id", "account_type", "initial_deposit"}
    assert artifact.outputs == []
    assert _selects(artifact)[0].value_template == "{account_type}"
    # the dropdown's own choices travel with the input, so the router can check a spelling against them
    assert {i.name: i.allowed_values for i in artifact.inputs} == {"member_id": None, "account_type": ["Savings", "Checking"], "initial_deposit": None}
    assert [s.value_template for s in artifact.steps if s.action == ActionType.FILL] == ["{member_id}", "{initial_deposit}"]
    assert all(s.action != ActionType.READ_TEXT for s in artifact.steps)  # an informational read is not part of the capability

    failed = DiscoveryResult(
        run_id="x", success=False, outcome="give_up", summary="stuck", outputs={}, steps=[],
        goal="g", target_url="http://x", started_at="t0", ended_at="t1", evidence_dir="/tmp/x",
    )
    with pytest.raises(ValueError):
        record_artifact(failed, "cap", "desc", {}, TargetApp(app_id="a", base_url="http://x", entry_path="/"))


def test_values_bind_to_the_control_that_demonstrated_them_not_just_to_the_value():
    """Two DIFFERENT controls demonstrating the IDENTICAL value (a transfer's source
    and destination both "Checking") must stay two distinct params; the same control
    revisited must keep its one param."""
    two_controls = _record(
        [_nav(), _select(1, "Checking", "tr:nth-of-type(1) > td:nth-of-type(2) > select"), _select(2, "Checking", "tr:nth-of-type(3) > td:nth-of-type(2) > select")],
        {"from_account_type": "Checking", "to_account_type": "Checking"},
    )
    assert {i.name for i in two_controls.inputs} == {"from_account_type", "to_account_type"}
    assert [s.value_template for s in _selects(two_controls)] == ["{from_account_type}", "{to_account_type}"]

    revisited = _record(
        [_nav(), _select(1, "Checking", "tr:nth-of-type(1) > select"), _select(2, "Checking", "tr:nth-of-type(1) > select", "re-set after a page refresh")],
        {"account_type": "Checking"},
    )
    assert {i.name for i in revisited.inputs} == {"account_type"}
    assert [s.value_template for s in _selects(revisited)] == ["{account_type}", "{account_type}"]


def test_a_recorded_value_matches_its_declared_param_despite_case_or_whitespace_and_nothing_fuzzier():
    """A select logs the option's visible label ("Checking"); the operator declares the
    underlying value ("checking"). Same demonstrated input, different representation."""
    for recorded, declared in [("checking", "checking"), ("Checking", "checking"), ("Savings", "savings"), ("checking", "Checking"), (" Checking ", "checking"), ("Checking", "  checking\t"), ("Checking\n", "checking")]:
        artifact = _record([_nav(), _select(1, recorded, "select:nth-of-type(1)")], {"account": declared})
        assert [s.value_template for s in _selects(artifact)] == ["{account}"], (recorded, declared)
        assert {i.name for i in artifact.inputs} == {"account"}, (recorded, declared)

    # ...but nothing fuzzier: a different word, a substring, and interior whitespace must stay unmatched
    for recorded, declared in [("Savings", "checking"), ("Checking", "check"), ("Checking Plus", "checking"), ("Check ing", "checking")]:
        artifact = _record([_nav(), _select(1, recorded, "select:nth-of-type(1)")], {"account": declared})
        assert [s.value_template for s in _selects(artifact)] == [recorded], (recorded, declared)
        assert artifact.inputs == [], (recorded, declared)

    unrelated = _record(
        [
            _nav(),
            StepLog(index=1, action="fill", rationale="enter id", ref="a", element_name="Member", value="10001", target=_target("Member"), ok=True),
            StepLog(index=2, action="fill", rationale="enter memo", ref="b", element_name="Memo", value="Rent", target=_target("Memo"), ok=True),
        ],
        {"member_id": "10001", "account": "checking"},
    )
    assert {s.description: s.value_template for s in unrelated.steps if s.action == ActionType.FILL} == {"enter id": "{member_id}", "enter memo": "Rent"}

    # normalization must not collapse two same-valued controls onto one param
    both = _record(
        [_nav(), _select(1, "Checking", "tr:nth-of-type(1) > select"), _select(2, "Checking", "tr:nth-of-type(3) > select")],
        {"from_account_type": "checking", "to_account_type": "checking"},
    )
    assert [s.value_template for s in _selects(both)] == ["{from_account_type}", "{to_account_type}"]


def test_a_declared_value_embedded_in_a_locator_or_frame_chain_is_templated_too():
    """A locator can embed a declared input, like an iframe src built from a member id. Untemplated,
    another member would read the wrong frame."""
    frame = LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/widget/42/panel"]'})
    assert _templatize_candidate(frame, {"widget_id": "42"}).value["css"] == 'iframe[src*="/widget/{widget_id}/panel"]'

    target = Target(candidates=[LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "$99.00"})], frame_chain=[[frame]])
    templated = _templatize_target(target, {"widget_id": "42"})
    assert templated.frame_chain[0][0].value["css"] == 'iframe[src*="/widget/{widget_id}/panel"]'
    assert templated.candidates[0].value["text"] == "$99.00"  # unrelated text untouched

    coords = LocatorCandidate(strategy=LocatorStrategy.COORDINATES, value={"x": 100, "y": 42})
    assert _templatize_candidate(coords, {"widget_id": "42"}).value == {"x": 100, "y": 42}  # numbers are never templated
    arbitrary = LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": "td[data-order='ORD-7788']"})
    assert _templatize_candidate(arbitrary, {"order_reference": "ORD-7788"}).value["css"] == "td[data-order='{order_reference}']"
    assert _templatize_target(None, {"widget_id": "42"}) is None

    # end to end through the recorder
    real = Target(
        candidates=[LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": "$1204.09"})],
        frame_chain=[[LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": 'iframe[src*="/member/10001/balance-frame"]'})]],
    )
    artifact = _record(
        [
            StepLog(index=0, action="navigate", rationale="start", url_before="", url_after=f"{BASE}/member/10001", ok=True),
            StepLog(index=1, action="read_text", rationale="read savings", element_name="$1204.09", target=real, ok=True),
        ],
        {"member_id": "10001"},
        outputs={"savings_balance": "$1204.09"},
    )
    read_step = next(s for s in artifact.steps if s.action == ActionType.READ_TEXT)
    assert read_step.target.frame_chain[0][0].value["css"] == 'iframe[src*="/member/{member_id}/balance-frame"]'


def test_inputs_declared_by_discovery_template_their_steps_even_when_redacted_and_the_goal_becomes_a_template():
    """Discovery's proven inputs arrive with param_binding. A redacted step still becomes a {param},
    and no raw or demo value reaches the artifact."""
    ssn = "123-45-6789"
    goal = f"Update address of member 10001 to 555 W Washington blvd chicago, SSN {ssn}"
    bound = lambda index, value, name, binding: StepLog(  # noqa: E731
        index=index, action="fill", rationale="fill", ref=f"f0e{index}", element_name=name, value=value,
        target=_target(name), ok=True, param_binding=binding,
    )
    steps = [
        _nav(),
        bound(1, "10001", "e.g. 10001", "member_id"),
        bound(2, "555 W Washington blvd chicago", "Mailing Address:", "address"),
        bound(3, "[REDACTED]", "SSN", "ssn"),
    ]
    result = DiscoveryResult(
        run_id="d", success=True, outcome="done", summary="", outputs={}, steps=steps, goal=goal, target_url=BASE + "/",
        started_at="t0", ended_at="t1", evidence_dir="/tmp/e",
        inputs={"member_id": "10001", "address": "555 W Washington blvd chicago", "ssn": ssn},
        input_descriptions={"member_id": "The member whose contact info changes", "address": "The new mailing address"},
        success_text="Contact information updated",
    )

    artifact = record_artifact(result, "update-address", "desc", {}, APP)

    assert [s.value_template for s in artifact.steps if s.action == ActionType.FILL] == ["{member_id}", "{address}", "{ssn}"]
    described = {i.name: (i.description, i.example, i.type.value) for i in artifact.inputs}
    assert described["member_id"] == ("The member whose contact info changes", "10001", "number")
    assert described["ssn"][1] == "[REDACTED]" and described["ssn"][0] == "Value for ssn."
    assert artifact.goal_template == "Update address of member {member_id} to {address}, SSN {ssn}"
    # the success phrase the agent saw is the checkpoint, not the URL path
    assert (artifact.final_checkpoint.kind.value, artifact.final_checkpoint.value) == ("text_contains", "Contact information updated")
    dumped = artifact.model_dump_json()
    assert ssn not in dumped  # a sensitive value is never persisted: not as a step value, an example, or in the goal
    assert "555 W Washington" not in dumped  # a street address is PII: not a step value, a locator, or even the example
    assert described["address"][1] == "[REDACTED] chicago"

    # the goal template placeholders, in the forms a goal actually writes values
    assert templatize_goal("Create a $12,500 auto loan for member 20001", {"loan_amount": "12500", "member_id": "20001"}) == "Create a ${loan_amount} auto loan for member {member_id}"
    assert templatize_goal("from checking to checking", {"from_account_type": "checking", "to_account_type": "checking"}) == "from {from_account_type} to {to_account_type}"
    assert templatize_goal("member 10001's balance", {"member_id": "1000"}) == "member 10001's balance"  # never inside a longer token
