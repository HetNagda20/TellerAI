"""Explicit, configurable allowlist of what the agent may act on.

Loaded from config/allowlist.json so it can be reviewed/edited without
touching code. This is a policy artifact, not a code path.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "allowlist.json"


class Allowlist:
    def __init__(self, allowed_domains: list[str], allowed_path_prefixes: list[str], allowed_actions: list[str]):
        self.allowed_domains = set(allowed_domains)
        self.allowed_path_prefixes = allowed_path_prefixes
        self.allowed_actions = set(allowed_actions)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Allowlist":
        data = json.loads(path.read_text())
        return cls(
            allowed_domains=data["allowed_domains"],
            allowed_path_prefixes=data["allowed_path_prefixes"],
            allowed_actions=data["allowed_actions"],
        )

    def url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host not in self.allowed_domains:
            return False
        # A bare-host URL (no trailing slash, e.g. "http://host:port") parses to an
        # empty path, not "/", which then never matches a "/" prefix even though
        # that is exactly the root path a browser would actually load. A real
        # discovery run got blocked 4 times navigating to a target URL with no
        # trailing slash before self-correcting. Treat "" the same as "/" here.
        path = parsed.path or "/"
        return any(path.startswith(p) for p in self.allowed_path_prefixes)

    def action_allowed(self, action: str) -> bool:
        return action in self.allowed_actions
