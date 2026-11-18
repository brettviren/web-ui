import asyncio
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from web_ui import config, instances
from web_ui.proxy import Proxy

BACKEND = str(Path(__file__).with_name("echo_backend.py"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cfg(env):
    path = env / "c" / "web-ui" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f"""
hostname = "here"
port = {free_port()}
runner = "process"

[services.echo]
command = ["{sys.executable}", "{BACKEND}", "--socket", "{{socket}}"]
marker = ".proj"

[services.solo]
command = ["{sys.executable}", "{BACKEND}", "--port", "{{port}}"]
listen = "tcp"
singleton = true
rewrite_host = true

[hosts.there]
autotunnel = false
""")
    c = config.load(path)
    yield c
    for inst in instances.discovered(c):
        instances.stop(c, inst.service, inst.name)


@pytest.fixture
def proxy(cfg):
    loop = asyncio.new_event_loop()
    p = Proxy(cfg)
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(p.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    yield p
    asyncio.run_coroutine_threadsafe(p.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def get(cfg, host, path="/", method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", cfg.port, timeout=10)
    conn.request(method, path, body=body, headers={"Host": f"{host}:{cfg.port}", **(headers or {})})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def project(env, name="projx"):
    d = env / name
    (d / ".proj").mkdir(parents=True)
    return d


def test_landing(cfg, proxy):
    st, body = get(cfg, "localhost")
    assert st == 200 and b"web-ui on here" in body
    st, body = get(cfg, "localhost", "/status.json")
    data = json.loads(body)
    assert data["host"] == "here"
    assert [h["host"] for h in data["hosts"]] == ["there"]
    assert any(i["service"] == "solo" for i in data["instances"])


def test_rejects_foreign_host(cfg, proxy):
    st, _ = get(cfg, "evil.example.com")
    assert st == 421


def test_up_and_route(cfg, proxy, env):
    d = project(env)
    (d / "sub").mkdir()
    svc = cfg.service("echo")
    found = instances.resolve_dir(svc, str(d / "sub"))  # marker is searched upward
    assert found == d.resolve()
    name = instances.choose_name(cfg, svc, found)
    assert name == "projx"
    inst = instances.Instance("echo", name, str(found))
    instances.save(cfg, inst)
    instances.start(cfg, inst)
    assert instances.is_up(cfg, "echo", "projx")
    st, body = get(cfg, "projx.echo.here.localhost", "/a?b=1", headers={"Connection": "keep-alive"})
    assert st == 200
    req = json.loads(body)
    assert req["target"] == "/a?b=1"
    assert req["headers"]["host"] == f"projx.echo.here.localhost:{cfg.port}"
    assert req["headers"]["connection"] == "close"


def test_autostart_registered(cfg, proxy, env):
    d = project(env, "lazy")
    instances.save(cfg, instances.Instance("echo", "lazy", str(d)))
    assert not instances.is_up(cfg, "echo", "lazy")
    st, body = get(cfg, "lazy.echo.here.localhost")
    assert st == 200
    assert instances.is_up(cfg, "echo", "lazy")


def test_unknown_instance_404(cfg, proxy):
    st, body = get(cfg, "nope.echo.here.localhost")
    assert st == 404 and b"web-ui up echo" in body


def test_singleton_tcp_autostart_rewrites_host(cfg, proxy):
    st, body = get(cfg, "solo.here.localhost")
    assert st == 200
    req = json.loads(body)
    port = int(cfg.instance_portfile("solo", "default").read_text())
    assert req["headers"]["host"] == f"127.0.0.1:{port}"
    assert req["headers"]["x-forwarded-host"] == f"solo.here.localhost:{cfg.port}"


def test_origin_checks(cfg, proxy):
    host = f"solo.here.localhost:{cfg.port}"
    st, _ = get(cfg, "solo.here.localhost", method="POST", body=b"x", headers={"Origin": "http://evil.example.com"})
    assert st == 403
    st, _ = get(cfg, "solo.here.localhost", method="POST", body=b"x", headers={"Origin": f"http://other.here.localhost:{cfg.port}"})
    assert st == 403
    st, body = get(cfg, "solo.here.localhost", method="POST", body=b"hi", headers={"Origin": f"http://{host}"})
    assert st == 200 and json.loads(body)["body"] == "hi"
    st, _ = get(cfg, "solo.here.localhost", method="POST", body=b"x", headers={"Sec-Fetch-Site": "cross-site"})
    assert st == 403


def test_upgrade_passthrough(cfg, proxy):
    get(cfg, "solo.here.localhost")  # start it
    with socket.create_connection(("127.0.0.1", cfg.port), timeout=5) as s:
        host = f"solo.here.localhost:{cfg.port}"
        s.sendall(f"GET /ws HTTP/1.1\r\nHost: {host}\r\nOrigin: http://{host}\r\nConnection: Upgrade\r\nUpgrade: test\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        assert resp.startswith(b"HTTP/1.1 101")
        s.sendall(b"ping")
        assert s.recv(4096) == b"ping"


def test_remote_host_via_socket(cfg, proxy, env):
    sock = cfg.host_socket("there")
    p = subprocess.Popen([sys.executable, BACKEND, "--socket", str(sock)])
    try:
        for _ in range(50):
            if instances.probe(("unix", str(sock))):
                break
            time.sleep(0.1)
        st, body = get(cfg, "x.maggie.there.localhost", "/r")
        assert st == 200
        req = json.loads(body)
        assert req["headers"]["host"] == f"x.maggie.there.localhost:{cfg.port}"
        assert req["target"] == "/r"
    finally:
        p.terminate()
        p.wait()


def test_remote_without_tunnel(cfg, proxy):
    st, body = get(cfg, "x.maggie.there.localhost")
    assert st == 502 and b"web-ui tunnel there" in body


def test_unix_front_ignores_host_label(cfg, proxy, env):
    d = project(env, "u")
    inst = instances.Instance("echo", "u", str(d))
    instances.save(cfg, inst)

    class UnixHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(str(cfg.front_socket))

    c = UnixHTTP("x", timeout=10)
    c.request("GET", "/", headers={"Host": "u.echo.whatever-seat-calls-it.localhost:9999"})
    r = c.getresponse()
    assert r.status == 200
    assert json.loads(r.read())["headers"]["host"].startswith("u.echo.whatever")


def test_access_log(env, cfg, proxy):
    from web_ui import log

    sink = env / "access.log"
    log.setup("info", str(sink))
    d = project(env)
    instances.start(cfg, instances.Instance("echo", "projx", str(d)))
    assert get(cfg, "projx.echo.here.localhost", "/hello?x=1")[0] == 200
    assert get(cfg, "evil.example")[0] == 421
    conn = http.client.HTTPConnection("127.0.0.1", cfg.port, timeout=5)  # a probe: no request, no line
    conn.connect()
    conn.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        access = [ln for ln in sink.read_text().splitlines() if " web_ui.access: " in ln]
        if len(access) >= 2:
            break
        time.sleep(0.05)
    assert len(access) == 2, access
    ok = next(ln for ln in access if " 200 " in ln)  # lines are written as connections close, in any order
    assert "tcp GET http://projx.echo.here.localhost:" in ok
    assert "/hello?x=1 200 " in ok and ok.endswith(" echo/projx")
    bad = next(ln for ln in access if " 421 " in ln)
    assert "evil.example" in bad and "not a web-ui name" in bad
    assert " INFO web_ui.instances: starting echo projx" in sink.read_text()
