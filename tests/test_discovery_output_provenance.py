"""Deterministic validation at the discovery completion boundary
(agent/loop.py::_unproven_outputs) -- the fix for fetch-account-balance@1.0.0's
empty `outputs: []`: the LLM could see a value in the accessibility snapshot
and write it into `done`'s outputs without ever calling read_text, and
artifact/recorder.py (correctly, and left untouched) then had nothing to
attribute the value to and dropped it silently.

Pure tests below build a StepLog list by hand, exactly like tests/test_recorder.py
does for the same reason: this logic needs no live LLM and no live browser to be
meaningfully exercised. One live test (real Playwright, real mock app) proves the
same mechanism end to end, including the iframe-derived savings balance -- the
exact case that was actually lost.
"""

from __future__ import annotations

import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from agent.executor import Executor, StepLog
from agent.loop import _unproven_outputs
from guardrails.allowlist import Allowlist
from handoff.session import HandoffSession

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


def _fill(index: int, value: str, ok: bool = True) -> StepLog:
    return StepLog(index=index, action="fill", rationale="fill", element_name="a field", value=value, ok=ok)


def _select(index: int, value: str, ok: bool = True) -> StepLog:
    return StepLog(index=index, action="select", rationale="select", element_name="a dropdown", value=value, ok=ok)


def _read_text(index: int, element_name: str, ok: bool = True) -> StepLog:
    return StepLog(index=index, action="read_text", rationale="read", element_name=element_name, ok=ok)


# -- pure unit tests: the rule itself, no live services ------------------------


def test_extracted_output_with_no_matching_step_is_unproven():
    steps = [_fill(0, "10001")]
    unproven = _unproven_outputs(steps, {"member_id": "10001", "checking_balance": "$662.44"})
    assert unproven == ["checking_balance"]


def test_input_echo_matching_a_fill_step_does_not_require_provenance():
    steps = [_fill(0, "10001")]
    unproven = _unproven_outputs(steps, {"member_id": "10001"})
    assert unproven == []


def test_input_echo_matching_a_select_step_does_not_require_provenance():
    steps = [_select(0, "savings")]
    unproven = _unproven_outputs(steps, {"account_type": "savings"})
    assert unproven == []


def test_output_matching_a_successful_read_text_step_is_proven():
    steps = [_read_text(0, "$662.44")]
    unproven = _unproven_outputs(steps, {"checking_balance": "$662.44"})
    assert unproven == []


def test_multiple_outputs_are_each_checked_independently():
    # One proven via read_text, one an input echo, one with nothing to back it --
    # only the genuinely unproven one should ever be flagged.
    steps = [_fill(0, "10001"), _read_text(1, "$662.44")]
    unproven = _unproven_outputs(
        steps,
        {"member_id": "10001", "checking_balance": "$662.44", "savings_balance": "$1204.09"},
    )
    assert unproven == ["savings_balance"]


def test_a_value_that_was_only_typed_by_a_failed_fill_does_not_count_as_an_echo():
    steps = [_fill(0, "10001", ok=False)]
    unproven = _unproven_outputs(steps, {"member_id": "10001"})
    assert unproven == ["member_id"]


def test_empty_outputs_are_trivially_fully_proven():
    assert _unproven_outputs([], {}) == []


# -- live: real Playwright, real mock app, including the iframe case -----------


@pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")
def test_real_discovery_style_run_proves_provenance_for_an_input_echo_and_two_read_text_outputs_including_the_iframe(
    tmp_path,
):
    """Mirrors the actual discovery_20260923T214406Z bug: look up member 10001,
    then claim member_id (typed), checking_balance (main-frame text), and
    savings_balance (iframe text) as outputs. Before any read_text, the two
    extracted values must be unproven exactly like the real buggy run; after
    read_text on the real elements (main frame AND iframe, via the same
    Executor.read_text path discovery actually uses), all three must be proven.
    """
    checking_live = urllib.request.urlopen(BASE + "/member/10001", timeout=2).read().decode()
    import re

    checking_value = re.search(r"Checking Balance:</td><td>(\$[\d.]+)</td>", checking_live).group(1)
    savings_live = urllib.request.urlopen(BASE + "/member/10001/balance-frame", timeout=2).read().decode()
    savings_value = re.search(r"Current Savings Balance:</td><td><b>(\$[\d.]+)</b>", savings_live).group(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        allowlist = Allowlist.load()
        handoff = HandoffSession(page=page, run_id="test_provenance", evidence_dir=tmp_path)
        executor = Executor(
            page=page, allowlist=allowlist, handoff=handoff, goal="Fetch the account balance for member 10001.",
            evidence_dir=tmp_path,
        )

        executor.navigate(BASE + "/accounts", rationale="start")
        snap = executor.current_snapshot
        textbox_ref = next(e.ref for e in snap.elements if e.role == "textbox")
        executor.fill(textbox_ref, "10001", rationale="enter member id")  # the input echo source

        snap = executor.current_snapshot
        search_ref = next(e.ref for e in snap.elements if e.role == "button" and e.name == "Search")
        executor.click(search_ref, rationale="search")

        claimed_outputs = {
            "member_id": "10001",
            "checking_balance": checking_value,
            "savings_balance": savings_value,
        }

        # Before read_text: exactly the real bug -- both extracted values unproven,
        # the typed member_id already fine as an input echo.
        unproven_before = _unproven_outputs(executor.steps, claimed_outputs)
        assert set(unproven_before) == {"checking_balance", "savings_balance"}

        snap = executor.current_snapshot
        checking_ref = next(e.ref for e in snap.elements if e.role == "text" and e.name == checking_value and e.frame_index == 0)
        executor.read_text(checking_ref, rationale="read checking balance")

        savings_ref = next(e.ref for e in snap.elements if e.role == "text" and e.name == savings_value and e.frame_index != 0)
        executor.read_text(savings_ref, rationale="read savings balance from the iframe")

        # After both real read_text calls (one main-frame, one iframe -- same
        # Executor.read_text path, no special-casing): everything is proven.
        unproven_after = _unproven_outputs(executor.steps, claimed_outputs)
        assert unproven_after == []

        # A value nothing ever produced stays unproven regardless.
        still_unproven = _unproven_outputs(executor.steps, {**claimed_outputs, "bogus_field": "not on the page anywhere"})
        assert still_unproven == ["bogus_field"]

        browser.close()
