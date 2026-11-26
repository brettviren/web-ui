import asyncio
import http.client
import os
import socket
import subprocess
import time

import pytest

from web_ui import config, systemd
from web_ui.proxy import DRAIN, Proxy


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cfg(env, monkeypatch):
    monkeypatch.setenv("WEB_UI_UNIT_DIR", str(env / "units"))
    path = env / "c" / "web-ui" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f'hostname = "here"\nport = {free_port()}\nrunner = "process"\n')
    return config.load(path)


ARGV = ["/opt/web-ui/bin/python3", "-m", "web_ui", "--config", "/home/me/.config/web-ui/config.toml"]


class FakeSystemctl:
    def __init__(self, transient=False, fragment="/nonexistent/systemd/transient/web-ui-proxy.service"):
        self.calls, self.transient, self.fragment = [], transient, fragment

    def __call__(self, *args):
        self.calls.append(args)
        out = ""
        if args[0] == "show" and "FragmentPath" in args:
            out = f"{self.fragment}\n" if self.transient else "\n"
        elif args[0] == "show" and "--value" in args:
            out = "active\n" if self.transient else "inactive\n"
        elif args[0] == "show":
            out = f"Transient={'yes' if self.transient else 'no'}\nActiveState=active\n"
        if args[0] == "stop":
            self.transient = False
        return subprocess.CompletedProcess(args, 0, out, "")

    def verbs(self):
        return [c[0] for c in self.calls]


def test_units(cfg):
    u = systemd.units(cfg, ARGV)
    sock, svc = u["web-ui-proxy.socket"], u["web-ui-proxy.service"]
    assert all(t.startswith(systemd.MARK) for t in u.values())
    assert "ListenStream=%t/web-ui/front.sock\n" in sock and f"ListenStream=127.0.0.1:{cfg.port}\n" in sock
    assert "SocketMode=0600" in sock and "DirectoryMode=0700" in sock and "WantedBy=sockets.target" in sock
    assert "Type=notify" in svc and "Requires=web-ui-proxy.socket" in svc
    assert "ExecStart=/opt/web-ui/bin/python3 -m web_ui --config /home/me/.config/web-ui/config.toml serve\n" in svc
    assert "Environment=PATH" not in svc and "KillMode" not in svc
    assert "tunnel --batch %i" in u["web-ui-tunnel@.service"]
    assert "127.0.0.1" not in systemd.units(cfg, ARGV, tcp=False)["web-ui-proxy.socket"]
    assert "KillMode=process" in systemd.units(cfg, ARGV, process_runner=True)["web-ui-proxy.service"]


def test_install_idempotent(cfg, env):
    run = FakeSystemctl()
    done = systemd.install(cfg, ARGV, run=run)
    assert len([d for d in done if d.startswith("wrote")]) == 3 and systemd.installed()
    assert run.verbs() == ["show", "daemon-reload", "enable", "enable", "restart"]
    assert ("enable", "--now", "web-ui-proxy.socket") in run.calls
    run.calls.clear()
    assert systemd.install(cfg, ARGV, run=run) == []
    assert "daemon-reload" not in run.verbs() and "restart" not in run.verbs() and "start" in run.verbs()


def test_install_takes_over_transient(cfg):
    run = FakeSystemctl(transient=True)
    done = systemd.install(cfg, ARGV, run=run)
    assert "stopped the transient web-ui-proxy.service" in done
    v = run.verbs()
    assert v.index("stop") < v.index("reset-failed") < v.index("daemon-reload")
    assert "kill" not in v


def test_install_takeover_stops_socket_and_drops_transient_file(cfg, env):
    """Seen on hokum: a socket from an earlier install restarted the transient proxy on the next
    connection, and systemd's transient file kept outranking ours across daemon-reload."""
    frag = env / "run" / "systemd" / "transient" / "web-ui-proxy.service"
    frag.parent.mkdir(parents=True)
    frag.write_text("[Service]\nExecStart=/old\n")
    run = FakeSystemctl(transient=True, fragment=str(frag))
    systemd.install(cfg, ARGV, run=run)
    assert ("stop", "--no-block", "--", "web-ui-proxy.socket", "web-ui-proxy.service") in run.calls
    assert not frag.exists()
    v = run.verbs()
    assert v.index("reset-failed") < v.index("daemon-reload") < v.index("enable")


def test_install_kills_stuck_transient(cfg, monkeypatch):
    """A transient proxy that ignores SIGTERM is killed, not waited on for 90s."""
    monkeypatch.setattr(systemd.time, "sleep", lambda s: None)
    t = iter(range(0, 1000, 5))
    monkeypatch.setattr(systemd.time, "monotonic", lambda: next(t))
    run = FakeSystemctl(transient=True)
    real = run.__call__

    def stuck(*args):
        if args[0] == "stop":
            run.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "kill":
            run.transient = False
        return real(*args)

    systemd.install(cfg, ARGV, run=stuck)
    v = run.verbs()
    assert v.index("stop") < v.index("kill") < v.index("reset-failed") < v.index("daemon-reload")


def test_close_with_idle_keepalive(cfg):
    """An open, idle client connection (a browser tab) doesn't keep a stopping proxy alive."""

    async def go():
        p = Proxy(cfg)
        await p.start()
        _, w = await asyncio.open_connection("127.0.0.1", p.port)
        await asyncio.sleep(0.1)  # accepted, and waiting for a request head
        await asyncio.wait_for(p.close(), DRAIN + 2)
        w.close()

    asyncio.run(go())


def test_install_refuses_foreign_file(cfg, env):
    (env / "units").mkdir()
    (env / "units" / "web-ui-proxy.service").write_text("[Service]\nExecStart=/mine\n")
    with pytest.raises(RuntimeError, match="not written by web-ui install"):
        systemd.install(cfg, ARGV, run=FakeSystemctl())


def test_uninstall(cfg, env):
    systemd.install(cfg, ARGV, run=FakeSystemctl())
    mine = env / "units" / "other.service"
    mine.write_text("x")
    run = FakeSystemctl()
    assert len(systemd.uninstall(run=run)) == 3 and not systemd.installed() and mine.exists()
    assert run.calls[0] == ("disable", "--now", "web-ui-proxy.socket", "web-ui-proxy.service")


def test_listen_fds_ignores_other_pid(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "2")
    assert systemd.listen_fds() == [] and "LISTEN_FDS" not in os.environ


def test_notify(env, monkeypatch):
    path = env / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        assert systemd.notify("READY=1")
        assert s.recv(100) == b"READY=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert not systemd.notify("READY=1")


def test_proxy_on_systemd_sockets(cfg):
    """Sockets as systemd would hand them over: served, and left in place when the proxy stops."""
    cfg.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix.bind(str(cfg.front_socket))
    unix.listen()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", cfg.port))
    tcp.listen()

    async def go():
        p = Proxy(cfg)
        await p.start([unix, tcp])
        assert p.activated and p.port == cfg.port

        def get():
            c = http.client.HTTPConnection("127.0.0.1", cfg.port, timeout=5)
            c.request("GET", "/", headers={"Host": f"localhost:{cfg.port}"})
            r = c.getresponse()
            return r.status

        assert await asyncio.to_thread(get) == 200
        await p.close()
        assert cfg.front_socket.exists()  # systemd's, not ours to remove

    asyncio.run(go())


def test_drain(cfg):
    async def go():
        p = Proxy(cfg)
        quick = asyncio.create_task(asyncio.sleep(0.05))
        slow = asyncio.create_task(asyncio.sleep(30))
        p.active |= {quick, slow}
        t = time.monotonic()
        await p.drain(grace=0.3)
        await asyncio.sleep(0)
        assert quick.done() and not quick.cancelled() and slow.cancelled()
        assert time.monotonic() - t < 5

    asyncio.run(go())
