"""The router's local Llama (via Ollama). It reads the catalog and goal and only proposes an answer,
which router/validator.py checks. If the model isn't running, ProposerUnavailable is raised."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from router.catalog import CatalogEntry

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ROUTER_MODEL", "llama3.1:8b")
DEFAULT_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


class ProposerUnavailable(Exception):
    """The model could not be reached or did not return a usable answer."""


SYSTEM = """You route a banking back-office request to one saved capability, or to "none".

Rules:
- In "needed_capabilities" list every saved capability (by capability_id) that would be needed to do the WHOLE request, each once. If the request needs more than one capability, answer "none".
- Choose a capability ONLY if the request clearly asks for exactly what it does, and that capability covers the WHOLE request.
- If the request asks for more than one operation, or for anything the capability does not do, answer "none" and set covers_whole_request to false.
- The capability must be about the same THING the request is about, not only the same verb. Changing a different field or record than the capability's description names (a phone number where it names an address, a loan where it names an account) is a different task: answer "none". Each value must be what its input means: a phone number is not an address.
- If nothing clearly matches, or the request is a different task, answer "none".
- args must contain EVERY input of the chosen capability, as strings, with values copied from the request.
- NEVER invent, guess, or default a value. If any input's value is not stated in the request, answer "none".
- A member id is the number that identifies a member. A number that is clearly something else (an account ending, a phone number, a ticket number, a date) is not one.
- If the request states a value for every input of a capability and asks for what it does, choose that capability even when the wording differs from its example request.
- Answer with JSON only.

Capabilities:
{catalog}
"""


def catalog_text(entries: list[CatalogEntry]) -> str:
    lines: list[str] = []
    for e in entries:
        lines.append(f"- capability_id: {e.capability_id}")
        lines.append(f"  what it does: {e.description}")
        lines.append(f"  example request: {e.example_request}")
        if e.inputs:
            lines.append("  inputs:")
            for i in e.inputs:
                meaning = f": {i.description}" if i.description else ""
                choices = f" Must be one of: {', '.join(i.allowed_values)}." if i.allowed_values else ""
                lines.append(f"    - {i.name} ({i.type}){meaning}. example: {i.example}.{choices}")
        else:
            lines.append("  inputs: none")
    return "\n".join(lines)


class OllamaProposer:
    def __init__(self, model: str = DEFAULT_MODEL, url: str = DEFAULT_URL, timeout_s: float = 120.0):
        self.model, self.url, self.timeout_s = model, url.rstrip("/"), timeout_s

    def propose(self, goal: str, entries: list[CatalogEntry]) -> dict:
        ids = [e.capability_id for e in entries]
        schema = {
            "type": "object",
            "properties": {
                "needed_capabilities": {"type": "array", "items": {"type": "string", "enum": ids}},
                "capability_id": {"type": "string", "enum": ids + ["none"]},
                "args": {"type": "object", "additionalProperties": {"type": "string"}},
                "covers_whole_request": {"type": "boolean"},
            },
            "required": ["needed_capabilities", "capability_id", "args", "covers_whole_request"],
        }
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "seed": 7},
            "messages": [
                {"role": "system", "content": SYSTEM.format(catalog=catalog_text(entries))},
                {"role": "user", "content": goal},
            ],
        }
        request = urllib.request.Request(
            f"{self.url}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                content = json.loads(response.read())["message"]["content"]
            proposal = json.loads(content)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProposerUnavailable(f"the router model at {self.url} could not be reached ({e})") from e
        except (KeyError, ValueError) as e:
            raise ProposerUnavailable(f"the router model returned an unusable answer ({e})") from e
        if not isinstance(proposal, dict):
            raise ProposerUnavailable("the router model returned an unusable answer (not an object)")
        logger.info("router model proposal goal=%r proposal=%s", goal, proposal)
        return proposal
