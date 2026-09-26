"""Integration verification for the re-recorded open-member-subaccount@1.0.1
artifact — the actual discovery-produced capability (not a hand-built stand-in
like test_replay_integration.py uses), loaded the same way the CLI's `replay`
command does.

This exists to prove the fix for the initial_deposit parameterization bug
(see PROJECT_STATE.md): the original @1.0.0 artifact never declared
account_type/initial_deposit as inputs and hardcoded the deposit fill to a
literal, so any --param value for them was silently ignored. @1.0.1 was
re-recorded from a discovery run that explicitly demonstrated all three
params, and every replay of it below runs the real recorded flow with real
supplied params through the unmodified replay engine — no special-casing of
these particular values anywhere in replay/executor.py.

Requires the mock app running at http://127.0.0.1:8000 (see README.md).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from artifact.store import load_path
from replay.executor import replay_artifact

BASE = "http://127.0.0.1:8000"
ARTIFACT_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "open-member-subaccount@1.0.1.json"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")


def _artifact():
    return load_path(ARTIFACT_PATH)


def test_artifact_declares_all_three_demonstrated_inputs():
    artifact = _artifact()
    input_names = {i.name for i in artifact.inputs}
    assert input_names == {"member_id", "account_type", "initial_deposit"}


def test_artifact_uses_templates_not_hardcoded_literals_for_account_type_and_deposit():
    artifact = _artifact()
    select_steps = [s for s in artifact.steps if s.action.value == "select"]
    fill_steps = [s for s in artifact.steps if s.action.value == "fill" and s.target is not None]
    assert any(s.value_template == "{account_type}" for s in select_steps)
    assert any(s.value_template == "{initial_deposit}" for s in fill_steps)


def test_valid_deposit_of_100_succeeds():
    result = replay_artifact(
        _artifact(),
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "100"},
        headless=True,
    )
    assert result.kind == "success"
    assert result.outputs["new_subaccount_id"].startswith("SA-")


def test_deposit_below_minimum_is_a_business_outcome_not_a_crash():
    result = replay_artifact(
        _artifact(),
        {"member_id": "10001", "account_type": "Savings", "initial_deposit": "5"},
        headless=True,
    )
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "invalid_deposit_amount"


def test_unknown_member_is_a_business_outcome_not_a_crash():
    result = replay_artifact(
        _artifact(),
        {"member_id": "12345", "account_type": "Savings", "initial_deposit": "100"},
        headless=True,
    )
    assert result.kind == "business_outcome"
    assert result.business_outcome_name == "member_not_found"
