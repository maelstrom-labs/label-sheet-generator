"""Structured JSON logging.

One line of JSON per event, so a free-tier host's log viewer can filter on
fields rather than on substrings. Tracebacks are logged here and nowhere else;
they never reach a response body.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"asctime", "message", "taskName"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


#: Libraries that log at INFO during normal operation and drown out our own
#: records.
_CHATTY = ("httpx", "httpcore", "PIL", "multipart", "matplotlib")

#: PDF parsers narrate every defect in a damaged file ("EOF marker not found",
#: "Ignoring wrong pointing object") straight to the root handler. On the
#: import path a damaged file is expected input rather than an incident -- it
#: already produces a typed error with a useful message -- so the running
#: commentary only makes real problems harder to see.
_PDF_PARSERS = ("pypdf", "pdfminer", "pdfplumber")


def quiet_noisy_libraries() -> None:
    """Raise third-party log levels. Safe to call from the CLI or the server."""
    for name in _CHATTY:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _PDF_PARSERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def configure(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the root handler. Idempotent, so repeated calls are harmless."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn installs its own duplicate handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    quiet_noisy_libraries()
