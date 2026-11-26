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
import socket
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from web_ui import config as config_mod
from web_ui import instances, systemd, tunnel
from web_ui.config import Config, ConfigError, ensure_private_dir
from web_ui.names import (
    DEFAULT_INSTANCE,
    NameError_,
    Target,
    hostname_for,
    parse_host,
    split_host_header,
)
from web_ui.peercred import peer_uid
from web_ui.runner import RunnerError, get_runner

log = logging.getLogger(__name__)
access_log = logging.getLogger("web_ui.access")

MAX_HEAD = 64 * 1024
HEAD_TIMEOUT = 30.0
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection"}
DRAIN = 5.0  # seconds a stopping proxy lets open connections finish


def has_body(req: Request) -> bool:
    """Whether a request carries a body (so it can't simply be sent again)."""
    return req.get("transfer-encoding") is not None or (req.get("content-length") or "0").strip() != "0"


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


@dataclass
class Access:
    """What the access log says about one connection."""

    kind: str  # "unix" (front socket) or "tcp" (loopback port)
    t0: float = field(default_factory=time.monotonic)
    req: Request | None = None
    status: int | None = None
    nbytes: int = 0
    backend: str = "-"
    note: str = ""

    def line(self) -> str:
        if self.req is None:
            what = "- -"
        else:
            host = next((v for k, v in self.req.headers if k.lower() == "host"), "-")
            what = f"{self.req.method} http://{host}{self.req.target}"
        status = self.status if self.status is not None else "-"
        out = f"{self.kind} {what} {status} {self.nbytes} {time.monotonic() - self.t0:.3f}s {self.backend}"
        return out + (f" ({self.note})" if self.note else "")


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


def _file_signature(path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def config_changes(old: Config, new: Config) -> list[str]:
    """What a reload changes, briefly, for the log."""
    out = []

    def diff(what: str, a, b) -> None:
        added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
        changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        for sign, keys in (("+", added), ("-", removed), ("~", changed)):
            if keys:
                out.append(f"{what} {sign}{', '.join(map(str, keys))}")

    diff("services", old.services, new.services)
    diff("hosts", old.hosts, new.hosts)
    diff("instances", {f"{s}/{n}": d for (s, n), d in old.declared.items()},
         {f"{s}/{n}": d for (s, n), d in new.declared.items()})  # fmt: skip
    for key in ("hostname", "aliases", "domain", "port", "runner", "check_uid", "strict_origin", "bin_dirs"):
        if getattr(old, key) != getattr(new, key):
            out.append(f"{key} {getattr(old, key)!r} -> {getattr(new, key)!r}")
    return out


class Proxy:
    def __init__(self, cfg: Config, port: int | None = None, autostart: bool = True):
        self.cfg = cfg
        self.cfg_signature = _file_signature(cfg.path) if cfg.path else None
        self.port_from_config = port is None
        self.port = cfg.port if port is None else port
        self.autostart = autostart
        self.uid = os.getuid()
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.servers: list[asyncio.base_events.Server] = []
        self.activated = False  # listening on sockets systemd holds (web-ui-proxy.socket)
        self.active: set[asyncio.Task] = set()  # connections being handled

    # ---- config ---------------------------------------------------------

    def reload(self, force: bool = False) -> bool:
        """Reload the config file if it changed (or ``force``).  True if reloaded.

        Called before each request, so an edit takes effect on the next one.
        A file that is missing or does not parse is logged and the config in
        use is kept: an editor may be half way through saving it.
        """
        path = self.cfg.path
        if path is None:
            return False
        sig = _file_signature(path)
        if sig == self.cfg_signature and not force:
            return False
        self.cfg_signature = sig  # also on failure: say so once per change, not per request
        if sig is None:
            log.warning("config %s is gone; keeping the config in use", path)
            return False
        try:
            new = config_mod.load(path)
        except (ConfigError, NameError_) as e:
            log.error("not reloading config: %s (keeping the config in use)", e)
            return False
        changes = config_changes(self.cfg, new)
        self.cfg = new
        log.info("reloaded config %s%s", path, (": " + "; ".join(changes)) if changes else " (no changes)")
        if self.port_from_config and new.port != self.port:
            log.warning("port changed to %s: restart the proxy to listen there (still on %s)", new.port, self.port)
        return True

    # ---- listening ------------------------------------------------------

    async def start(self, sockets: list | None = None) -> None:
        """Listen: on ``sockets`` from systemd if given (else any it passed us), or bind our own."""
        cfg = self.cfg
        ensure_private_dir(cfg.runtime_dir)
        ensure_private_dir(cfg.hosts_dir)
        sockets = systemd.listen_fds() if sockets is None else sockets
        if sockets:
            await self._start_activated(sockets)
            return
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
                shown = f"[{addr}]" if ":" in addr else addr
                log.info("listening on http://%s:%d (try http://localhost:%d/)", shown, self.port, self.port)

    async def _start_activated(self, sockets: list) -> None:
        """Serve on sockets systemd holds: the Unix one is the front door, TCP ones the loopback port."""
        self.activated = True
        ports = set()
        for sock in sockets:
            if sock.family == socket.AF_UNIX:
                srv = await asyncio.start_unix_server(lambda r, w: self.handle(r, w, "unix"), sock=sock, limit=MAX_HEAD)
                log.info("listening on %s (from systemd)", sock.getsockname())
            else:
                srv = await asyncio.start_server(lambda r, w: self.handle(r, w, "tcp"), sock=sock, limit=MAX_HEAD)
                ports.add(sock.getsockname()[1])
                log.info("listening on %s port %d (from systemd)", sock.getsockname()[0], sock.getsockname()[1])
            self.servers.append(srv)
        self.port = min(ports) if ports else 0
        if self.port_from_config and ports and self.cfg.port not in ports:
            log.warning("config port is %s but systemd gave %s: run `web-ui install` to update the socket unit",
                        self.cfg.port, sorted(ports))  # fmt: skip

    async def close(self) -> None:
        for srv in self.servers:
            srv.close()
        for srv in self.servers:
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        self.servers.clear()
        if not self.activated and not instances.probe(("unix", str(self.cfg.front_socket))):
            self.cfg.front_socket.unlink(missing_ok=True)  # systemd's socket stays for the next proxy
        log.info("stopped listening")

    async def drain(self, grace: float = DRAIN) -> None:
        """Wait up to ``grace`` seconds for connections being handled, then cut the rest."""
        if not self.active:
            return
        log.info("letting %d connection(s) finish (up to %gs)", len(self.active), grace)
        _, pending = await asyncio.wait(set(self.active), timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            log.info("cut %d connection(s) still open after %gs", len(pending), grace)

    async def serve_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        def on_signal(sig: signal.Signals) -> None:
            log.info("got %s, shutting down", sig.name)
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal, sig)
        loop.add_signal_handler(signal.SIGHUP, self.reload, True)
        log.debug("serving %s as uid %d: autostart=%s check_uid=%s strict_origin=%s",
                  self.cfg.hostname, self.uid, self.autostart, self.cfg.check_uid, self.cfg.strict_origin)  # fmt: skip
        systemd.notify(f"READY=1\nSTATUS=serving {self.cfg.hostname}")
        try:
            await stop.wait()
        finally:
            systemd.notify("STOPPING=1")
            await self.close()  # stop accepting; with systemd's sockets, new connections queue
            await self.drain()

    # ---- per connection -------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.active.add(task)
            task.add_done_callback(self.active.discard)
        acc = Access(kind)
        try:
            await self._handle(reader, writer, acc)
        except HTTPError as e:
            acc.status, acc.note = e.status, e.message
            page_ = error_page(e)
            acc.nbytes = len(page_)
            with contextlib.suppress(Exception):
                writer.write(page_)
                await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError) as e:
            # Without a request this is a liveness probe (or an idle client): not worth a line.
            if acc.req is not None:
                acc.note = f"connection ended: {type(e).__name__}"
        except Exception as e:
            log.exception("error handling %s", acc.line())
            acc.status, acc.note = 500, f"proxy error: {e!r}"
            with contextlib.suppress(Exception):
                writer.write(error_page(HTTPError(500, "Internal Server Error", "web-ui proxy error; see its log")))
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            if acc.req is not None or acc.status is not None:
                access_log.info("%s", acc.line())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, acc: Access) -> None:
        kind = acc.kind
        self.reload()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except asyncio.LimitOverrunError:
            raise HTTPError(431, "Request Header Fields Too Large", "request head too large")
        req = Request.parse(head)
        acc.req = req
        # Check the peer once it has sent a request: it is still connected, waiting
        # for the reply, so its socket is surely still in /proc/net/tcp.
        if self.cfg.check_uid:
            sock = writer.get_extra_info("socket")
            uid = peer_uid(sock) if sock is not None else None
            log.debug("%s connection from uid %s", kind, uid)
            if uid != self.uid:
                raise HTTPError(403, "Forbidden", f"connection from uid {uid}; this proxy serves uid {self.uid} only")
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
        log.debug("%s %s %s%s -> %s", kind, req.method, host_header, req.target, target)
        if remote:
            acc.backend = f"tunnel:{target.host}"
            breader, bwriter = await self.connect_remote(target.host)
        elif target.service is None:
            acc.backend = "landing"
            page_ = await self.landing(req, host_header, target.host or self.cfg.hostname)
            acc.status, acc.nbytes = 200, len(page_)
            writer.write(page_)
            await writer.drain()
            return
        else:
            acc.backend = f"{target.service}/{target.instance}"
            breader, bwriter = await self.connect_local(target, req)

        if not req.is_upgrade:
            for h in HOP_BY_HOP:
                req.set(h, None)
            req.set("Connection", "close")
        head = req.encode()
        bwriter.write(head)
        await self.splice(reader, writer, breader, bwriter, acc)
        if acc.nbytes == 0 and remote and not has_body(req) and await self.revive_remote(target.host):
            breader, bwriter = await self.connect_remote(target.host)
            bwriter.write(head)
            await self.splice(reader, writer, breader, bwriter, acc)
        if acc.nbytes == 0:  # nothing reached the client yet, so it can still get an error page
            if remote:
                raise HTTPError(502, "Bad Gateway", f"{target.host}'s web-ui proxy did not answer through the tunnel",
                                f"Start it: ssh {self.cfg.host(target.host).ssh} web-ui serve --detach.  "
                                f"Or stop the tunnel (systemctl --user stop {tunnel.unit_name(target.host)}) "
                                "and the next visit reopens it.")  # fmt: skip
            raise HTTPError(502, "Bad Gateway", f"{acc.backend} closed the connection without answering",
                            f"See: web-ui logs {target.service} {target.instance}")  # fmt: skip

    def check_origin(self, req: Request, host_header: str) -> None:
        """Refuse cross-origin state changes and WebSockets (CSRF, rebinding)."""
        if not self.cfg.strict_origin:
            return
        if req.method in SAFE_METHODS and not req.is_upgrade:
            return
        origin = req.get("origin")
        if origin is None:
            site = req.get("sec-fetch-site")
            if req.is_upgrade or (site or "same-origin") in ("same-origin", "none"):
                log.debug("no Origin, Sec-Fetch-Site %s: allowed", site)
                return  # non-browser client, or a browser that says it is same-origin
            raise HTTPError(403, "Forbidden", f"cross-site request refused (Sec-Fetch-Site: {site})")
        parts = urlsplit(origin)
        if origin == "null" or parts.netloc.lower() != host_header.strip().lower():
            raise HTTPError(403, "Forbidden", f"cross-origin request from {origin} refused")
        log.debug("Origin %s matches", origin)

    # ---- backends -------------------------------------------------------

    async def connect_local(self, target: Target, req: Request):
        cfg = self.cfg
        svc_name, name = target.service, target.instance
        assert svc_name
        svc = cfg.services.get(svc_name)
        ep = instances.endpoint(cfg, svc_name, name)
        log.debug("%s/%s endpoint %s", svc_name, name, ep)
        if svc and svc.singleton and name != DEFAULT_INSTANCE:
            raise HTTPError(404, "Not Found", f"{svc_name} has one instance on {cfg.hostname}",
                            f"Use {hostname_for(target.host or cfg.hostname, svc_name, DEFAULT_INSTANCE, cfg.domain)}")  # fmt: skip
        if not instances.probe(ep):
            inst = instances.get(cfg, svc_name, name)
            if inst is None and svc and svc.singleton and name == DEFAULT_INSTANCE:
                inst = instances.Instance(svc_name, name)
            if inst is None or svc is None:
                hint = f"Start it with: web-ui up {svc_name}" + ("" if name == DEFAULT_INSTANCE else f" DIR --name {name}")
                if svc is None:
                    hint = f"{svc_name!r} is not a configured service on {cfg.hostname}."
                raise HTTPError(404, "Not Found", f"no instance {name!r} of {svc_name!r} on {cfg.hostname}", hint)
            if not svc.managed:
                raise HTTPError(502, "Bad Gateway", f"{svc_name} is not running on {cfg.hostname}",
                                f"web-ui connects to it at {svc.connect} but does not start it.")  # fmt: skip
            if not (self.autostart and svc.autostart):
                raise HTTPError(502, "Bad Gateway", f"{svc_name} {name} is not running", f"web-ui up {svc_name} --name {name}")
            async with self.locks[f"{svc_name}/{name}"]:
                if not instances.probe(instances.endpoint(cfg, svc_name, name)):
                    log.debug("autostarting %s/%s", svc_name, name)
                    if instances.get(cfg, svc_name, name) is None:
                        instances.save(cfg, inst)
                    try:
                        await asyncio.to_thread(instances.start, cfg, inst)
                    except (instances.StartError, RunnerError) as e:
                        raise HTTPError(502, "Bad Gateway", f"could not start {svc_name} {name}: {e}")
                    except Exception as e:
                        log.exception("starting %s %s", svc_name, name)
                        raise HTTPError(502, "Bad Gateway", f"could not start {svc_name} {name}: {e}")
            ep = instances.endpoint(cfg, svc_name, name)
        kind, addr = ep
        try:
            if kind == "unix":
                conn = await asyncio.open_unix_connection(addr)
            else:
                conn = await asyncio.open_connection(*addr)
        except OSError as e:
            log.error("cannot connect to %s %s at %s: %s", svc_name, name, addr, e)
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
                    await asyncio.to_thread(self._start_tunnel, hcfg)
        try:
            return await asyncio.open_unix_connection(str(path))
        except OSError as e:
            log.error("cannot connect to %s's tunnel at %s: %s", host, path, e)
            raise HTTPError(502, "Bad Gateway", f"cannot connect to {host}'s tunnel: {e}")

    def _start_tunnel(self, hcfg) -> None:
        log.info("starting tunnel to %s (ssh %s)", hcfg.name, hcfg.ssh)
        runner = get_runner(self.cfg)
        unit = tunnel.unit_name(hcfg.name)
        t0 = time.monotonic()
        try:
            runner.start(tunnel.launch(self.cfg, hcfg))
        except RunnerError as e:
            raise HTTPError(502, "Bad Gateway", f"cannot start the tunnel to {hcfg.name}: {e}")
        path = str(self.cfg.host_socket(hcfg.name))
        deadline = t0 + 30
        while time.monotonic() < deadline:
            if instances.probe(("unix", path)):
                log.info("tunnel to %s is up after %.1fs", hcfg.name, time.monotonic() - t0)
                return
            if not runner.is_active(unit):
                log.error("tunnel to %s exited; see: web-ui logs --tunnel %s", hcfg.name, hcfg.name)
                break
            time.sleep(0.2)
        else:
            log.error("tunnel to %s did not come up within 30s", hcfg.name)
        raise HTTPError(502, "Bad Gateway", f"tunnel to {hcfg.name} did not come up",
                        f"Check: web-ui logs --tunnel {hcfg.name}")  # fmt: skip

    async def revive_remote(self, host: str) -> bool:
        """Get HOST's proxy answering through the tunnel again.  True if it does.

        First ask the host to start its proxy (it may have stopped while the
        tunnel stayed up), then, if that is not enough, restart the tunnel.
        """
        cfg = self.cfg
        hcfg = cfg.hosts.get(host)
        if not (self.autostart and hcfg and hcfg.autotunnel):
            return False
        async with self.locks[f"tunnel/{host}"]:
            if await asyncio.to_thread(tunnel.remote_answers, cfg, host):
                return True  # another request revived it meanwhile
            log.warning("%s's proxy does not answer through the tunnel; reviving it", host)
            if hcfg.ensure_remote:
                argv = tunnel.ensure_remote_argv(hcfg)
                log.debug("running %s", tunnel.describe(argv))
                try:
                    r = await asyncio.to_thread(subprocess.run, argv, timeout=60, stdin=subprocess.DEVNULL,
                                                capture_output=True, text=True)  # fmt: skip
                    log.debug("exit %d, output %r, errors %r", r.returncode, r.stdout, r.stderr)
                except subprocess.TimeoutExpired:
                    log.error("timed out starting the proxy on %s", host)
                if await asyncio.to_thread(tunnel.remote_answers, cfg, host):
                    log.info("%s's proxy answers again", host)
                    return True
            runner = get_runner(cfg)
            unit = tunnel.unit_name(host)
            if not runner.is_active(unit):
                log.error("%s still does not answer, and its tunnel is not one web-ui started", host)
                return False
            log.info("restarting the tunnel to %s", host)
            await asyncio.to_thread(runner.stop, unit)
            await asyncio.to_thread(self._start_tunnel, hcfg)
        ok = await asyncio.to_thread(tunnel.remote_answers, cfg, host)
        if not ok:
            log.error("%s's proxy still does not answer through the new tunnel", host)
        return ok

    @staticmethod
    async def splice(creader, cwriter, breader, bwriter, acc: Access | None = None) -> None:
        acc = acc or Access("-")

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, response: bool) -> None:
            try:
                while data := await src.read(65536):
                    if response:
                        if acc.status is None:  # the status line is at the start of the first chunk
                            parts = data[:32].split(b" ", 2)
                            if len(parts) > 1 and parts[0].startswith(b"HTTP/") and parts[1].isdigit():
                                acc.status = int(parts[1])
                        acc.nbytes += len(data)
                    dst.write(data)
                    await dst.drain()
                # A response that never started is left open, for an error page or a retry.
                if dst.can_write_eof() and not (response and acc.nbytes == 0):
                    dst.write_eof()
            except (ConnectionError, OSError) as e:
                log.debug("%s %s: %r", acc.backend, "response" if response else "request", e)

        up = asyncio.create_task(pump(creader, bwriter, False))
        down = asyncio.create_task(pump(breader, cwriter, True))
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

    def status(self, port: int | None, label: str | None = None, probe_remote: bool = False) -> dict:
        """What runs here.  ``label`` is the name the client calls this host by.

        With ``probe_remote``, also ask each tunneled host's proxy whether it
        answers.  Not for status.json, which other hosts' proxies ask for:
        two seats tunneled to each other would ask each other forever.
        """
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
                    "service": svc.name, "instance": DEFAULT_INSTANCE, "dir": None,
                    "up": instances.is_up(cfg, svc.name, DEFAULT_INSTANCE),
                    "url": "http://" + hostname_for(label, svc.name, DEFAULT_INSTANCE, cfg.domain) + (f":{port}" if port else "") + "/",
                })  # fmt: skip
        insts.sort(key=lambda d: (d["service"], d["instance"]))
        hosts = set(cfg.hosts)
        if cfg.hosts_dir.is_dir():
            hosts |= {p.stem for p in cfg.hosts_dir.glob("*.sock")}
        host_list = [
            {
                "host": h,
                "tunnel": (tun := instances.probe(("unix", str(cfg.host_socket(h))))),
                "answers": tun and probe_remote and tunnel.remote_answers(cfg, h, timeout=3),
                "url": "http://" + hostname_for(h, domain=cfg.domain) + (f":{port}" if port else "") + "/",
            }
            for h in sorted(hosts - {cfg.hostname, label})
        ]
        return {"host": label, "instances": insts, "hosts": host_list}

    async def landing(self, req: Request, host_header: str, label: str) -> bytes:
        _, port = split_host_header(host_header)
        path = urlsplit(req.target).path
        if path == "/ping":
            return response(200, "OK", "ok\n", "text/plain")
        if path not in ("/", "/status.json"):
            raise HTTPError(404, "Not Found", f"no such page {path}")
        st = await asyncio.to_thread(self.status, port, label, path == "/")
        if path == "/status.json":
            return response(200, "OK", json.dumps(st, indent=2) + "\n", "application/json")
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
                if h["answers"]:
                    state = "<span class=up>● connected</span>"
                elif h["tunnel"]:
                    state = "<span class=down>◐ tunnel up, its proxy not answering (opening the host revives it)</span>"
                else:
                    state = "<span class=down>○ not connected</span>"
                hrows.append(f'<tr><td><a href="{esc(h["url"])}">{esc(h["host"])}</a></td><td>{state}</td></tr>')
            body += "<h2>Other hosts</h2><table>" + "".join(hrows) + "</table>"
        return response(200, "OK", page(f"web-ui {st['host']}", body))


def run(cfg: Config, port: int | None = None, autostart: bool = True) -> None:
    asyncio.run(Proxy(cfg, port, autostart).serve_forever())
