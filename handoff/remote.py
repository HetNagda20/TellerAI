"""A remote-debugging port for a run's browser, so a person who cannot see the window (a headless run, or
someone at another machine) can still open the live page in Chrome DevTools, look at it and operate it.

Scope, stated plainly: the port is reachable only from THIS computer. Reaching it from another machine needs an
SSH tunnel to the port (`ssh -L PORT:127.0.0.1:PORT host`), which the operator sets up; this module does not build
or open one. Anyone who can reach the port controls the browser, which is why it is loopback-only.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


def remote_debugging_enabled() -> bool:
    """On by default. REMOTE_DEBUGGING=off keeps the browser closed to everything but Playwright."""
    return os.environ.get("REMOTE_DEBUGGING", "on").strip().lower() not in ("0", "off", "false", "no")


def free_loopback_port() -> int:
    """A port nobody is using right now, chosen per run, so several runs can be active at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_args(port: int) -> list[str]:
    """Chromium arguments that open the debugging port.

    LOOPBACK ONLY. Chromium binds --remote-debugging-port to 127.0.0.1 and we deliberately never pass
    --remote-debugging-address, which is the only way to widen that. A test checks that the port refuses a
    connection on the machine's own network address. The port gives full, unauthenticated control of the
    browser, so it must never be exposed on another interface.

    --remote-allow-origins lists only this port's own origin, so DevTools served by the browser can connect to
    it and no web page from anywhere else can (never "*").
    """
    return [f"--remote-debugging-port={port}", f"--remote-allow-origins=http://127.0.0.1:{port},http://localhost:{port}"]


def devtools_link(page, port: Optional[int]) -> str:
    """The link that opens DevTools on this run's own tab, or "" when the port is off or not answering.

    It opens the DevTools frontend the browser serves itself (no internet needed) on the exact tab id of `page`,
    not the first tab in the list. DevTools shows the tab's live DOM and console, and its screencast view of the
    page takes mouse and keyboard input; everything done that way reaches the page as ordinary input events, so
    the gesture-capture listener records it the same as a click in a headed window.
    """
    if not port:
        return ""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            pass
        target_id = page.context.new_cdp_session(page).send("Target.getTargetInfo")["targetInfo"]["targetId"]
    except Exception as e:  # the port did not open (taken meanwhile), or the tab is gone
        logger.warning("remote debugging link unavailable port=%s error=%s", port, e)
        return ""
    return f"http://127.0.0.1:{port}/devtools/inspector.html?ws=127.0.0.1:{port}/devtools/page/{target_id}"
