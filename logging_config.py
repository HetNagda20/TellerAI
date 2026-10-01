"""Logging setup for cli.py and mock_app/app.py: console plus a rotating file under logs/. Each
process gets its own file so automation and app lines don't mix."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from guardrails.redact import redact_text

LOG_DIR = Path(__file__).resolve().parent / "logs"


class _RedactFilter(logging.Filter):
    """Scrubs PII shapes out of every log line, so a goal or value logged by any module is redacted."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact_text(record.getMessage()), None
        return True


def configure_logging(name: str = "app", level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured in this process

    LOG_DIR.mkdir(exist_ok=True)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(_RedactFilter())
    root.addHandler(console)

    log_file = LOG_DIR / f"{name}.log"
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_RedactFilter())
    root.addHandler(file_handler)
