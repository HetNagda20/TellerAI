"""Architecture boundaries, checked from source. Replay has no model. The router core never touches
a browser or the discovery model, and its local proposer can only propose."""

import subprocess
import sys
from pathlib import Path

import replay.executor as replay_executor
import router.catalog as router_catalog
import router.proposer as router_proposer
import router.router as router_module
import router.validator as router_validator

ROOT = Path(__file__).resolve().parent.parent
_NO_BROWSER_OR_DISCOVERY = ("playwright", "anthropic", "from agent", "import agent", "handoff.gesture", "make_client", "next_action")


def test_replay_and_the_router_stay_inside_their_boundaries():
    # artifact/ sits below agent/: importing the recorder must not load the discovery loop, the LLM client or a browser
    probe = "import sys, artifact.recorder, artifact.store, artifact.annotations; print(sorted(m for m in ('agent', 'anthropic', 'playwright') if m in sys.modules))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=True).stdout.strip()
    assert loaded == "[]", f"artifact/ pulled in {loaded} at import time"

    boundaries = {
        replay_executor: ("anthropic", "agent.llm", "agent.loop", "make_client", "next_action", "router"),
        router_module: _NO_BROWSER_OR_DISCOVERY,
        router_catalog: _NO_BROWSER_OR_DISCOVERY,
        router_validator: _NO_BROWSER_OR_DISCOVERY + ("urllib.request", "OllamaProposer"),  # the gate itself never calls a model
        router_proposer: _NO_BROWSER_OR_DISCOVERY,
    }
    for module, forbidden in boundaries.items():
        source = open(module.__file__).read()
        for token in forbidden:
            assert token not in source, f"{module.__name__} must never reference {token!r}"
