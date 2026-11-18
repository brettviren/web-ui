"""Logging setup: one level and one sink per process.

The sink is ``stdout``, ``stderr`` or a file name.  The default is
``$XDG_STATE_HOME/web-ui/web-ui.log``, shared by every web-ui process on
the host (the CLI, the proxy and tunnels), so each line carries the pid.

Levels are used like this:

- ``debug``: anything that helps trace a problem (commands run, endpoints
  probed, routing decisions);
- ``info``: the proxy's access log, plus the few events that matter in
  normal operation (listening, starting and stopping instances and tunnels);
- ``warning``: something was skipped or degraded;
- ``error`` / ``critical``: a failure, with the traceback when there is one.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from web_ui.config import state_dir

LEVELS = ("debug", "info", "warning", "error", "critical")
DEFAULT_LEVEL = "info"
STREAMS = ("stdout", "stderr")
FORMAT = "%(asctime)s web-ui[%(process)d] %(levelname)s %(name)s: %(message)s"

_handler: logging.Handler | None = None
_level = DEFAULT_LEVEL
_sink = ""


def default_sink() -> Path:
    return state_dir() / "web-ui.log"


class FileHandler(logging.handlers.WatchedFileHandler):
    """Reopens the file if logrotate (or you) moves it away.

    A failure to log (the directory vanished, the disk is full) is reported
    on stderr by ``handleError`` and never raised into the caller.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


def _open_file(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Request paths and directories end up in the log: keep it private.
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    return FileHandler(path, encoding="utf-8")


def setup(level: str | None = None, sink: str | None = None) -> None:
    """Send the ``web_ui`` loggers to ``sink`` at ``level``.  Safe to call again."""
    global _handler, _level, _sink
    level = (level or DEFAULT_LEVEL).lower()
    if level not in LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LEVELS)}, not {level!r}")
    if not sink:
        sink = str(default_sink())
    if sink in STREAMS:
        handler: logging.Handler = logging.StreamHandler(getattr(sys, sink))
    else:
        path = Path(sink).expanduser().absolute()
        sink = str(path)
        handler = _open_file(path)
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger("web_ui")
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    _handler, _level, _sink = handler, level, sink


def reset() -> None:
    """Remove the handler installed by setup()."""
    global _handler, _level, _sink
    if _handler is not None:
        logging.getLogger("web_ui").removeHandler(_handler)
        _handler.close()
    _handler, _level, _sink = None, DEFAULT_LEVEL, ""


def argv() -> list[str]:
    """Options giving a child web-ui process this process's level and sink."""
    if _handler is None:
        return []
    return ["--log-level", _level, "--log-sink", _sink]


def current_file() -> Path | None:
    """The log file in use, if the sink is a file."""
    return Path(_sink) if _handler is not None and _sink not in STREAMS else None
