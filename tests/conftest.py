"""Test-generated replay/discovery evidence should not land in the real
/evidence/ deliverable directory (that's reserved for the genuine discovery
run and its replays). Redirect both evidence roots to a temp dir for the
whole test session.
"""

import pytest

import agent.loop
import replay.executor


@pytest.fixture(autouse=True)
def _isolated_evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.loop, "EVIDENCE_ROOT", tmp_path)
    monkeypatch.setattr(replay.executor, "EVIDENCE_ROOT", tmp_path)
    yield
