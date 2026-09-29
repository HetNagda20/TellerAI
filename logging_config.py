"""System logging setup, used by cli.py and mock_app/app.py.

Every module logs through logging.getLogger(__name__); this file only
configures where those records go. Console plus a rotating file under
logs/ is enough to find a run's successes and failures without adding a
metrics/observability stack this project does not need.

cli.py (the automation system: discovery, replay, router) and
mock_app/app.py (the target app stand-in) are separate processes with
separate root loggers, so each gets its own log file rather than sharing
one: automation activity belongs in logs/app.log, and mock_app's own
request log belongs in logs/mock_app.log. Mixing them would blur which
side, the automation or the legacy app it is driving, produced a given line.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def configure_logging(name: str = "app", level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured in this process

    LOG_DIR.mkdir(exist_ok=True)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    log_file = LOG_DIR / f"{name}.log"
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
