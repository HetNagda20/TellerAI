"""Shared test setup. The mock app runs in-process on a free port, state is reset per test,
artifacts point at the test server, and evidence goes to a temp dir."""

from __future__ import annotations

import copy
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

import agent.loop
import mock_app.app as mock_app_module
import mock_app.data as mock_data
import replay.executor
from artifact.schema import Artifact, ArtifactStatus
from artifact.store import ARTIFACTS_DIR

RECORDED_BASE_URL = "http://127.0.0.1:8000"

_STATE_NAMES = ("MEMBERS", "SUBACCOUNTS", "LOANS", "TRANSACTIONS")
_COUNTER_NAMES = ("_next_subaccount_seq", "_next_transfer_seq", "_next_loan_seq")


@pytest.fixture(autouse=True)
def _isolated_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.loop, "EVIDENCE_ROOT", tmp_path)
    monkeypatch.setattr(replay.executor, "EVIDENCE_ROOT", tmp_path)
    yield


@pytest.fixture(autouse=True)
def _short_replay_waits(monkeypatch):
    """Replay waits patiently for slow apps; the mock app is fast, so cap the waits to keep expected-failure
    tests quick. The one test about patience sets its own values."""
    monkeypatch.setattr(replay.executor, "_ACTION_TIMEOUT_MS", 1500)
    monkeypatch.setattr(replay.executor, "_CHECKPOINT_TIMEOUT_MS", 2000)
    monkeypatch.setattr(replay.executor, "_SETTLE_MS", 1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def _server():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mock_app_module.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("mock app did not start")
        time.sleep(0.05)
    pristine = {
        "state": {n: copy.deepcopy(getattr(mock_data, n)) for n in _STATE_NAMES},
        "counters": {n: getattr(mock_data, n) for n in _COUNTER_NAMES},
    }
    yield f"http://127.0.0.1:{port}", pristine
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def base_url(_server) -> str:
    """The test mock app's URL, with its state reset to the seed data."""
    url, pristine = _server
    for name, seed in pristine["state"].items():
        live = getattr(mock_data, name)
        live.clear()
        live.update(copy.deepcopy(seed))
    for name, value in pristine["counters"].items():
        setattr(mock_data, name, value)
    mock_app_module._armed_test_failure = None
    mock_app_module._response_delay_ms = 0
    mock_app_module._session_epoch = 0
    return url


def load_capability(name: str, version: str, base_url: str, status: ArtifactStatus = ArtifactStatus.APPROVED) -> Artifact:
    """A saved artifact pointed at the test server. Approved by default so tests
    of other behavior aren't stopped by the risky-step gate; the gate's own
    tests pass DRAFT explicitly."""
    text = (Path(ARTIFACTS_DIR) / f"{name}@{version}.json").read_text().replace(RECORDED_BASE_URL, base_url)
    return Artifact.model_validate_json(text).model_copy(update={"status": status})


def member(member_id: str) -> dict:
    return mock_data.MEMBERS[member_id]


def ledger(member_id: str) -> list[dict]:
    """Newest first; rows written by automation carry source 'replay'/'discovery'."""
    return [t for t in mock_data.TRANSACTIONS.get(member_id, []) if t["source"] != "seed"]
