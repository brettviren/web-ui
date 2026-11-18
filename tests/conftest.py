import tempfile
from pathlib import Path

import pytest

from web_ui import log


@pytest.fixture
def env(monkeypatch):
    """Point every XDG dir at a short temp dir (Unix socket paths max ~108 bytes)."""
    with tempfile.TemporaryDirectory(prefix="wu-", dir="/tmp") as d:
        root = Path(d)
        for var, sub in [("XDG_CONFIG_HOME", "c"), ("XDG_STATE_HOME", "s"), ("XDG_RUNTIME_DIR", "r")]:
            (root / sub).mkdir(mode=0o700)
            monkeypatch.setenv(var, str(root / sub))
        monkeypatch.delenv("WEB_UI_CONFIG", raising=False)
        yield root


@pytest.fixture(autouse=True)
def reset_logging():
    """CLI tests install a log handler on a temp dir; drop it after each test."""
    yield
    log.reset()
