"""Focused concurrency test for replay isolation.

replay/executor.py's own runtime state (run_id, evidence_dir, browser, page,
outputs, strategy_log) was already per-call-local by inspection -- the one
real, concrete shared-mutable-state hazard was `_run_id()`'s second-
granularity timestamp: two concurrent replays of the same capability
starting within the same wall-clock second used to collide on evidence_dir
and could write over each other's evidence. No queues, workers, clusters, or
locks are introduced anywhere -- this just proves two ordinary concurrent
replay_artifact() calls, sharing the same read-only Artifact object, stay
independently isolated.
"""

from __future__ import annotations

import threading
import urllib.request

import pytest

from artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    InputParam,
    ParamType,
    Step,
    TargetApp,
)
from replay.executor import replay_artifact

BASE = "http://127.0.0.1:8000"


def _mock_app_up() -> bool:
    try:
        return urllib.request.urlopen(BASE + "/", timeout=1).status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mock_app_up(), reason="mock app is not running at 127.0.0.1:8000")


def _lookup_artifact() -> Artifact:
    return Artifact(
        capability_id="member-lookup-concurrency-demo",
        version="1.0.0",
        description="Minimal artifact for concurrency isolation testing.",
        goal_template="Look up member {member_id}.",
        target_app=TargetApp(app_id="cu-servicing-console", base_url=BASE, entry_path="/"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="member id")],
        outputs=[],
        steps=[Step(index=0, action=ActionType.NAVIGATE, description="go", value_template=f"{BASE}/member/{{member_id}}")],
        final_checkpoint=Checkpoint(kind=CheckpointKind.URL_CONTAINS, value="/member/{member_id}"),
        created_from_run_id="hand_built_for_tests",
    )


def test_concurrent_replays_of_the_same_artifact_are_independently_isolated():
    shared_artifact = _lookup_artifact()  # deliberately the SAME object, shared read-only across both threads
    before = shared_artifact.model_dump_json()

    results: dict[str, object] = {}
    errors: list[Exception] = []

    def _run(key: str, member_id: str) -> None:
        try:
            results[key] = replay_artifact(shared_artifact, {"member_id": member_id}, headless=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=_run, args=("a", "10001"))
    t2 = threading.Thread(target=_run, args=("b", "20001"))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not errors, f"concurrent replay raised: {errors}"
    assert "a" in results and "b" in results, "both concurrent invocations must complete"

    r1, r2 = results["a"], results["b"]
    assert r1.kind == "success" and r2.kind == "success"
    assert r1.run_id != r2.run_id, "concurrent replays must get distinct execution IDs"
    assert r1.evidence_dir != r2.evidence_dir, "concurrent replays must get distinct evidence directories"

    assert shared_artifact.model_dump_json() == before, "the shared artifact must remain untouched by either invocation"
