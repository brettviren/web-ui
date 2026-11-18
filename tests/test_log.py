import sys

from click.testing import CliRunner

from web_ui import config, log, tunnel
from web_ui.cli import main


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


def test_default_sink_is_state_file(env):
    r = CliRunner().invoke(main, ["-l", "debug", "config"])
    assert r.exit_code == 0, r.output
    path = env / "s" / "web-ui" / "web-ui.log"
    assert log.current_file() == path
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert " DEBUG web_ui.cli: " in text and "web-ui[" in text


def test_level_filters(env):
    sink = env / "x.log"
    r = CliRunner().invoke(main, ["-L", str(sink), "config"])
    assert r.exit_code == 0, r.output
    assert not any(" DEBUG " in ln for ln in lines(sink))


def test_stream_sinks(env):
    log.setup("debug", "stderr")
    assert log.current_file() is None
    assert log.argv() == ["--log-level", "debug", "--log-sink", "stderr"]
    r = CliRunner().invoke(main, ["-l", "DEBUG", "-L", "stdout", "config"])
    assert r.exit_code == 0
    assert " DEBUG web_ui.cli: " in r.output


def test_env_vars_and_errors_logged(env, monkeypatch):
    sink = env / "e.log"
    monkeypatch.setenv("WEB_UI_LOG_SINK", str(sink))
    monkeypatch.setenv("WEB_UI_LOG_LEVEL", "warning")
    r = CliRunner().invoke(main, ["url", "nosuch"])
    assert r.exit_code != 0
    errs = [ln for ln in lines(sink) if " ERROR " in ln]
    assert errs and "unknown service 'nosuch'" in errs[0]


def test_bad_level_rejected(env):
    r = CliRunner().invoke(main, ["-l", "loud", "config"])
    assert r.exit_code == 2


def test_children_inherit_logging(env):
    sink = env / "child.log"
    log.setup("debug", str(sink))
    cfg = config.load()
    argv = tunnel.launch(cfg, cfg.host("far")).argv
    assert argv[:3] == [sys.executable, "-m", "web_ui"]
    assert argv[argv.index("--log-sink") + 1] == str(sink)
    assert argv[argv.index("--log-level") + 1] == "debug"
    assert argv[argv.index("--config") + 1] == str(cfg.path)
    assert argv[-3:] == ["tunnel", "--batch", "far"]
