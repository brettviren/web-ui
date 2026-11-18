"""The front door: one small HTTP router per machine.

It listens on ``$XDG_RUNTIME_DIR/web-ui/front.sock`` (what remote seats reach
through SSH) and, on a seat, on one loopback TCP port (what the browser
reaches).  It reads each request's head, routes on the Host header, then
splices bytes between client and backend, so WebSockets and server-sent
events pass through untouched.

Routing:

- On the Unix socket every request is for this host; the host label is
  ignored.
- On the TCP port, ``*.<self>.localhost`` goes to local instances and
  ``*.<other>.localhost`` goes to ``hosts/<other>.sock``, the SSH-forwarded
  front door of that host.

To keep routing and checks per request, every request gets its own backend
connection: the proxy sends ``Connection: close`` (except for protocol
upgrades), so a kept-alive connection can never carry a second request past
the checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from web_ui import instances, tunnel
from web_ui.config import Config, ensure_private_dir
from web_ui.names import (
    DEFAULT_INSTANCE,
    NameError_,
    Target,
    hostname_for,
    parse_host,
    split_host_header,
)
from web_ui.peercred import peer_uid
from web_ui.runner import get_runner

log = logging.getLogger(__name__)

MAX_HEAD = 64 * 1024
HEAD_TIMEOUT = 30.0
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection"}


class HTTPError(Exception):
    def __init__(self, status: int, reason: str, message: str, hint: str = ""):
        super().__init__(message)
        self.status, self.reason, self.message, self.hint = status, reason, message, hint


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, head: bytes) -> Request:
        try:
            text = head.decode("latin-1")
            lines = text.split("\r\n")
            method, target, version = lines[0].split(" ")
        except ValueError:
            raise HTTPError(400, "Bad Request", "malformed request line")
        if not version.startswith("HTTP/1."):
            raise HTTPError(505, "HTTP Version Not Supported", f"unsupported {version}")
        headers = []
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise HTTPError(400, "Bad Request", "malformed header")
            headers.append((name, value.strip()))
        return cls(method, target, version, headers)

    def get(self, name: str) -> str | None:
        name = name.lower()
        vals = [v for k, v in self.headers if k.lower() == name]
        if len(vals) > 1 and name in ("host", "origin"):
            raise HTTPError(400, "Bad Request", f"duplicate {name} header")
        return vals[0] if vals else None

    def set(self, name: str, value: str | None) -> None:
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()]
        if value is not None:
            self.headers.append((name, value))

    @property
    def is_upgrade(self) -> bool:
        conn = {t.strip().lower() for t in (self.get("connection") or "").split(",")}
        return "upgrade" in conn and self.get("upgrade") is not None

    def encode(self) -> bytes:
        lines = [f"{self.method} {self.target} {self.version}"]
        lines += [f"{k}: {v}" for k, v in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def response(status: int, reason: str, body: str, ctype: str = "text/html; charset=utf-8") -> bytes:
    data = body.encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + data


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --fg:#1d1d1f; --bg:#fbfbfa; --mute:#6b6b6b; --line:#e3e3e0; --ok:#1a7f37; --link:#0b57d0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e8e6; --bg:#17181a; --mute:#9a9a9a; --line:#2c2d30; --ok:#4ac26b; --link:#8ab4f8; }} }}
body {{ font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 60rem; margin: 2rem auto; padding: 0 16px; }}
h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin-top: 2rem; color: var(--mute); text-transform: uppercase; letter-spacing: .05em; }}
table {{ border-collapse: collapse; width: 100%; }} td, th {{ text-align: left; padding: .35rem .6rem .35rem 0; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ font-weight: 500; color: var(--mute); }} a {{ color: var(--link); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
code, .dir {{ font-family: ui-monospace, monospace; font-size: .85em; color: var(--mute); overflow-wrap: anywhere; }}
.up {{ color: var(--ok); }} .down {{ color: var(--mute); }} p.hint {{ color: var(--mute); }}
</style></head><body>
{body}
</body></html>
"""


def page(title: str, body: str) -> str:
    return PAGE.format(title=html.escape(title), body=body)


def error_page(e: HTTPError) -> bytes:
    body = f"<h1>{e.status} {html.escape(e.reason)}</h1><p>{html.escape(e.message)}</p>"
    if e.hint:
        body += f"<p class=hint>{html.escape(e.hint)}</p>"
    return response(e.status, e.reason, page(f"{e.status} {e.reason}", body))


class Proxy:
    def __init__(self, cfg: Config, port: int | None = None, autostart: bool = True):
        self.cfg = cfg
        self.port = cfg.port if port is None else port
        self.autostart = autostart
        self.uid = os.getuid()
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.servers: list[asyncio.base_events.Server] = []

    # ---- listening ------------------------------------------------------

    async def start(self) -> None:
        cfg = self.cfg
        ensure_private_dir(cfg.runtime_dir)
        ensure_private_dir(cfg.hosts_dir)
        front = cfg.front_socket
        if instances.probe(("unix", str(front))):
            raise RuntimeError(f"a proxy is already listening on {front}")
        front.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            srv = await asyncio.start_unix_server(lambda r, w: self.handle(r, w, "unix"), path=str(front), limit=MAX_HEAD)
        finally:
            os.umask(old)
        self.servers.append(srv)
        log.info("listening on %s", front)
        if self.port:
            for addr in ("127.0.0.1", "::1"):
                try:
                    srv = await asyncio.start_server(lambda r, w: self.handle(r, w, "tcp"), host=addr, port=self.port, limit=MAX_HEAD)
                except OSError as e:
                    if addr == "::1" and e.errno in (99, 97):  # no IPv6 loopback
                        continue
                    raise RuntimeError(f"cannot listen on {addr}:{self.port}: {e.strerror}") from e
                self.servers.append(srv)
                log.info("listening on http://%s:%d (try http://localhost:%d/)", addr, self.port, self.port)

    async def close(self) -> None:
        for srv in self.servers:
            srv.close()
        for srv in self.servers:
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        self.servers.clear()
        if not instances.probe(("unix", str(self.cfg.front_socket))):
            self.cfg.front_socket.unlink(missing_ok=True)

    async def serve_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await self.close()

    # ---- per connection -------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str) -> None:
        try:
            await self._handle(reader, writer, kind)
        except HTTPError as e:
            log.info("%s %s", e.status, e.message)
            with contextlib.suppress(Exception):
                writer.write(error_page(e))
                await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("error handling request")
            with contextlib.suppress(Exception):
                writer.write(error_page(HTTPError(500, "Internal Server Error", "web-ui proxy error; see its log")))
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str) -> None:
        if self.cfg.check_uid:
            sock = writer.get_extra_info("socket")
            uid = peer_uid(sock) if sock is not None else None
            if uid != self.uid:
                raise HTTPError(403, "Forbidden", f"connection from uid {uid}; this proxy serves uid {self.uid} only")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except asyncio.LimitOverrunError:
            raise HTTPError(431, "Request Header Fields Too Large", "request head too large")
        req = Request.parse(head)
        host_header = req.get("host")
        if not host_header:
            raise HTTPError(400, "Bad Request", "no Host header")
        try:
            target = parse_host(host_header, self.cfg.domain)
        except NameError_ as e:
            raise HTTPError(421, "Misdirected Request", f"not a web-ui name: {e}",
                            f"Use [instance.]service.host.{self.cfg.domain}")  # fmt: skip
        self.check_origin(req, host_header)

        remote = kind == "tcp" and target.host is not None and not self.cfg.is_self(target.host)
        log.debug("%s %s %s -> %s", kind, req.method, host_header, target)
        if remote:
            breader, bwriter = await self.connect_remote(target.host)
        elif target.service is None:
            writer.write(await self.landing(req, host_header, target.host or self.cfg.hostname))
            await writer.drain()
            return
        else:
            breader, bwriter = await self.connect_local(target, req)

        if not req.is_upgrade:
            for h in HOP_BY_HOP:
                req.set(h, None)
            req.set("Connection", "close")
        bwriter.write(req.encode())
        await self.splice(reader, writer, breader, bwriter)

    def check_origin(self, req: Request, host_header: str) -> None:
        """Refuse cross-origin state changes and WebSockets (CSRF, rebinding)."""
        if not self.cfg.strict_origin:
            return
        if req.method in SAFE_METHODS and not req.is_upgrade:
            return
        origin = req.get("origin")
        if origin is None:
            if req.is_upgrade or (req.get("sec-fetch-site") or "same-origin") in ("same-origin", "none"):
                return  # non-browser client, or a browser that says it is same-origin
            raise HTTPError(403, "Forbidden", "cross-site request refused")
        parts = urlsplit(origin)
        if origin == "null" or parts.netloc.lower() != host_header.strip().lower():
            raise HTTPError(403, "Forbidden", f"cross-origin request from {origin} refused")

    # ---- backends -------------------------------------------------------

    async def connect_local(self, target: Target, req: Request):
        cfg = self.cfg
        svc_name, name = target.service, target.instance
        assert svc_name
        svc = cfg.services.get(svc_name)
        ep = instances.endpoint(cfg, svc_name, name)
        if not instances.probe(ep):
            inst = instances.get(cfg, svc_name, name)
            if inst is None and svc and svc.singleton and name == DEFAULT_INSTANCE:
                inst = instances.Instance(svc_name, name)
            if inst is None or svc is None:
                hint = f"Start it with: web-ui up {svc_name}" + ("" if name == DEFAULT_INSTANCE else f" DIR --name {name}")
                if svc is None:
                    hint = f"{svc_name!r} is not a configured service on {cfg.hostname}."
                raise HTTPError(404, "Not Found", f"no instance {name!r} of {svc_name!r} on {cfg.hostname}", hint)
            if not (self.autostart and svc.autostart):
                raise HTTPError(502, "Bad Gateway", f"{svc_name} {name} is not running", f"web-ui up {svc_name} --name {name}")
            async with self.locks[f"{svc_name}/{name}"]:
                if not instances.probe(instances.endpoint(cfg, svc_name, name)):
                    log.info("starting %s %s", svc_name, name)
                    if instances.get(cfg, svc_name, name) is None:
                        instances.save(cfg, inst)
                    try:
                        await asyncio.to_thread(instances.start, cfg, inst)
                    except Exception as e:
                        raise HTTPError(502, "Bad Gateway", f"could not start {svc_name} {name}: {e}")
            ep = instances.endpoint(cfg, svc_name, name)
        kind, addr = ep
        try:
            if kind == "unix":
                conn = await asyncio.open_unix_connection(addr)
            else:
                conn = await asyncio.open_connection(*addr)
        except OSError as e:
            raise HTTPError(502, "Bad Gateway", f"cannot connect to {svc_name} {name}: {e}")
        if svc and svc.rewrite_host:
            orig = req.get("host")
            req.set("Host", f"{addr[0]}:{addr[1]}" if kind == "tcp" else "localhost")
            req.set("X-Forwarded-Host", orig)
            req.set("X-Forwarded-Proto", "http")
        return conn

    async def connect_remote(self, host: str):
        cfg = self.cfg
        path = cfg.host_socket(host)
        if not instances.probe(("unix", str(path))):
            hcfg = cfg.hosts.get(host)
            if not (self.autostart and hcfg and hcfg.autotunnel):
                raise HTTPError(502, "Bad Gateway", f"no tunnel to {host}", f"Run: web-ui tunnel {host}")
            async with self.locks[f"tunnel/{host}"]:
                if not instances.probe(("unix", str(path))):
                    log.info("starting tunnel to %s", host)
                    await asyncio.to_thread(self._start_tunnel, hcfg)
        try:
            return await asyncio.open_unix_connection(str(path))
        except OSError as e:
            raise HTTPError(502, "Bad Gateway", f"cannot connect to {host}'s tunnel: {e}")

    def _start_tunnel(self, hcfg) -> None:
        runner = get_runner(self.cfg)
        unit = tunnel.unit_name(hcfg.name)
        runner.start(tunnel.launch(self.cfg, hcfg))
        path = str(self.cfg.host_socket(hcfg.name))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if instances.probe(("unix", path)):
                return
            if not runner.is_active(unit):
                break
            time.sleep(0.2)
        raise HTTPError(502, "Bad Gateway", f"tunnel to {hcfg.name} did not come up",
                        f"Check: web-ui logs --tunnel {hcfg.name}")  # fmt: skip

    @staticmethod
    async def splice(creader, cwriter, breader, bwriter) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
                if dst.can_write_eof():
                    dst.write_eof()
            except (ConnectionError, OSError):
                pass

        up = asyncio.create_task(pump(creader, bwriter))
        down = asyncio.create_task(pump(breader, cwriter))
        try:
            await down  # the response is done when the backend closes
        finally:
            up.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await up
            bwriter.close()
            with contextlib.suppress(Exception):
                await bwriter.wait_closed()

    # ---- landing page ---------------------------------------------------

    def status(self, port: int | None, label: str | None = None) -> dict:
        """What runs here.  ``label`` is the name the client calls this host by."""
        cfg = self.cfg
        label = label or cfg.hostname
        insts = []
        seen = set()
        for inst in instances.discovered(cfg):
            seen.add((inst.service, inst.name))
            insts.append({
                "service": inst.service, "instance": inst.name, "dir": inst.dir,
                "up": instances.is_up(cfg, inst.service, inst.name),
                "url": "http://" + hostname_for(label, inst.service, inst.name, cfg.domain) + (f":{port}" if port else "") + "/",
            })  # fmt: skip
        for svc in cfg.services.values():
            if svc.singleton and (svc.name, DEFAULT_INSTANCE) not in seen:
                insts.append({
                    "service": svc.name, "instance": DEFAULT_INSTANCE, "dir": None, "up": False,
                    "url": "http://" + hostname_for(label, svc.name, DEFAULT_INSTANCE, cfg.domain) + (f":{port}" if port else "") + "/",
                })  # fmt: skip
        insts.sort(key=lambda d: (d["service"], d["instance"]))
        hosts = set(cfg.hosts)
        if cfg.hosts_dir.is_dir():
            hosts |= {p.stem for p in cfg.hosts_dir.glob("*.sock")}
        host_list = [
            {
                "host": h,
                "tunnel": instances.probe(("unix", str(cfg.host_socket(h)))),
                "url": "http://" + hostname_for(h, domain=cfg.domain) + (f":{port}" if port else "") + "/",
            }
            for h in sorted(hosts - {cfg.hostname, label})
        ]
        return {"host": label, "instances": insts, "hosts": host_list}

    async def landing(self, req: Request, host_header: str, label: str) -> bytes:
        _, port = split_host_header(host_header)
        st = await asyncio.to_thread(self.status, port, label)
        path = urlsplit(req.target).path
        if path == "/status.json":
            return response(200, "OK", json.dumps(st, indent=2) + "\n", "application/json")
        if path != "/":
            raise HTTPError(404, "Not Found", f"no such page {path}")
        esc = html.escape
        rows = []
        for i in st["instances"]:
            state = '<span class=up>● up</span>' if i["up"] else '<span class=down>○ idle</span>'
            name = i["service"] if i["instance"] == DEFAULT_INSTANCE else f'{i["service"]} / {i["instance"]}'
            rows.append(f'<tr><td><a href="{esc(i["url"])}">{esc(name)}</a></td><td>{state}</td>'
                        f'<td class=dir>{esc(i["dir"] or "")}</td></tr>')  # fmt: skip
        body = f"<h1>web-ui on {esc(st['host'])}</h1>"
        if rows:
            body += "<table><tr><th>service</th><th>state</th><th>directory</th></tr>" + "".join(rows) + "</table>"
        else:
            body += "<p class=hint>No instances. Start one with <code>web-ui up SERVICE [DIR]</code>.</p>"
        if st["hosts"]:
            hrows = []
            for h in st["hosts"]:
                state = '<span class=up>● connected</span>' if h["tunnel"] else '<span class=down>○ not connected</span>'
                hrows.append(f'<tr><td><a href="{esc(h["url"])}">{esc(h["host"])}</a></td><td>{state}</td></tr>')
            body += "<h2>Other hosts</h2><table>" + "".join(hrows) + "</table>"
        return response(200, "OK", page(f"web-ui {st['host']}", body))


def run(cfg: Config, port: int | None = None, autostart: bool = True) -> None:
    asyncio.run(Proxy(cfg, port, autostart).serve_forever())
