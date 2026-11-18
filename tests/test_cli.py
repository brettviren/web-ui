import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from web_ui import config, instances
from web_ui.cli import main

BACKEND = str(Path(__file__).with_name("echo_backend.py"))


@pytest.fixture
def cli(env):
    path = env / "c" / "web-ui" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f"""
hostname = "here"
runner = "process"
[services.echo]
command = ["{sys.executable}", "{BACKEND}", "--socket", "{{socket}}"]
marker = ".proj"
[hosts.there]
ssh = "me@there.example.org"
remote_runtime_dir = "/run/user/4242"
""")
    runner = CliRunner()

    def run(*args, ok=True):
        r = runner.invoke(main, list(args), catch_exceptions=False)
        if ok:
            assert r.exit_code == 0, r.output
        return r

    yield run
    cfg = config.load(path)
    for inst in instances.discovered(cfg):
        instances.stop(cfg, inst.service, inst.name)


def test_up_ls_down(cli, env):
    a = env / "a" / "proj"
    b = env / "b" / "proj"
    for d in (a, b):
        (d / ".proj").mkdir(parents=True)
    r = cli("up", "echo", str(a))
    assert r.output.splitlines()[0] == "http://proj.echo.here.localhost:8484/"
    # same basename, different dir: gets a hashed name
    r = cli("up", "echo", str(b))
    url_b = r.output.splitlines()[0]
    assert url_b.startswith("http://proj-") and url_b != "http://proj.echo.here.localhost:8484/"
    # same dir again: same instance
    assert cli("up", "echo", str(a / ".proj")).output.splitlines()[0] == "http://proj.echo.here.localhost:8484/"
    data = json.loads(cli("ls", "--json").output)
    up = {i["instance"]: i["up"] for i in data["instances"]}
    assert up["proj"] and len(up) == 2 and all(up.values())
    assert "stopped" in cli("down", "echo", str(a), "--forget").output
    assert "proj" not in {i["instance"] for i in json.loads(cli("ls", "--json").output)["instances"]}


def test_up_without_marker_fails(cli, env):
    r = cli("up", "echo", str(env), ok=False)
    assert r.exit_code != 0 and "no .proj found" in r.output


def test_unknown_service(cli):
    r = cli("up", "nope", ok=False)
    assert "unknown service 'nope'" in r.output


def test_tunnel_print(cli, env):
    out = cli("tunnel", "--print", "there").output.splitlines()
    assert out[0].endswith("me@there.example.org 'web-ui serve --detach --print-socket'")
    assert f"-L {env}/r/web-ui/hosts/there.sock:/run/user/4242/web-ui/front.sock me@there.example.org" in out[1]
    assert "ControlPath=none" in out[1]


def test_gen(cli):
    out = cli("gen", "ssh-config").output
    assert "Match tagged web-ui originalhost there" in out
    assert "LocalForward /run/user/%i/web-ui/hosts/%n.sock /run/user/4242/web-ui/front.sock" in out
    out = cli("gen", "systemd").output
    assert "web-ui-tunnel@.service" in out and "tunnel --batch %i" in out


def test_shared_config_declares_per_host(env, tmp_path):
    path = env / "shared.toml"
    path.write_text(f"""
hostname = "haiku"
runner = "process"
bin_dirs = ["~/somewhere/bin"]
[services.maggie]
command = ["maggie", "{{socket}}"]
marker = ".beads"
[services.wckb]
command = ["{sys.executable}", "{BACKEND}", "--socket", "{{socket}}"]
[services.agentsview]
command = ["agentsview", "--port", "{{port}}"]
listen = "tcp"
singleton = true
hosts = ["wcgpu0"]
[instances.wcgpu0.maggie]
xero = "~/dune/xero/.beads"
[instances.wcgpu0.wckb]
xero = "~/dune/xero"
[instances.haiku.wckb]
kb = "{env}/kb"
""")
    here = config.load(path)
    assert set(here.services) == {"maggie", "wckb"}
    assert here.elsewhere == {"agentsview": ["wcgpu0"]}
    assert here.declared == {("wckb", "kb"): f"{env}/kb"}
    assert [(i.service, i.name) for i in instances.registered(here)] == [("wckb", "kb")]
    launch = instances.prepare_launch(here, instances.get(here, "wckb", "kb"))
    assert launch.env["PATH"].startswith(str(Path("~/somewhere/bin").expanduser()) + ":")
    assert launch.argv[0] == sys.executable

    path.write_text(path.read_text().replace('hostname = "haiku"', 'hostname = "wcgpu0"'))
    there = config.load(path)
    assert set(there.services) == {"maggie", "wckb", "agentsview"}
    home = str(Path.home())
    assert there.declared == {("maggie", "xero"): f"{home}/dune/xero", ("wckb", "xero"): f"{home}/dune/xero"}


def test_declared_instance_reused_by_up(cli, env):
    path = env / "c" / "web-ui" / "config.toml"
    kb = env / "kb"
    (kb / ".proj").mkdir(parents=True)
    path.write_text(path.read_text() + f'\n[instances.here.echo]\nmine = "{kb}"\n')
    assert cli("up", "echo", str(kb)).output.splitlines()[0] == "http://mine.echo.here.localhost:8484/"
    assert "still declared" in cli("down", "echo", str(kb), "--forget").output


def test_parse_front_socket():
    from web_ui.tunnel import parse_front_socket

    assert parse_front_socket("motd junk\n/run/user/5123/web-ui/front.sock\n") == "/run/user/5123/web-ui/front.sock"
    assert parse_front_socket("") is None
