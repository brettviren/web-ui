"""Service instances: a registry, how to launch them and how to reach them.

An instance is one running copy of a service, e.g. maggie for one project's
``.beads/``.  Its record lives in ``~/.config/web-ui/instances/<service>/<name>.json``
and holds the directory it serves.  Its endpoint is either a Unix socket at
``$XDG_RUNTIME_DIR/web-ui/<service>/<name>.sock`` or, for services that can
only listen on TCP, an ephemeral loopback port recorded next to it in
``<name>.port``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from web_ui.config import Config, ConfigError, Service, ensure_private_dir
from web_ui.names import DEFAULT_INSTANCE, check_label, short_hash, slugify, url_for
from web_ui.runner import Launch, get_runner


@dataclass
class Instance:
    service: str
    name: str
    dir: str | None = None
    created: float = 0.0


# ---- registry ---------------------------------------------------------------


def _record(cfg: Config, service: str, name: str) -> Path:
    return cfg.instances_dir / service / f"{name}.json"


def get(cfg: Config, service: str, name: str) -> Instance | None:
    try:
        d = json.loads(_record(cfg, service, name).read_text())
    except (OSError, ValueError):
        return None
    return Instance(**{k: d.get(k) for k in ("service", "name", "dir", "created")})


def save(cfg: Config, inst: Instance) -> None:
    path = _record(cfg, inst.service, inst.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(inst), indent=2) + "\n")


def forget(cfg: Config, service: str, name: str) -> bool:
    path = _record(cfg, service, name)
    if path.exists():
        path.unlink()
        return True
    return False


def registered(cfg: Config) -> list[Instance]:
    out = []
    if cfg.instances_dir.is_dir():
        for rec in sorted(cfg.instances_dir.glob("*/*.json")):
            inst = get(cfg, rec.parent.name, rec.stem)
            if inst:
                out.append(inst)
    return out


def discovered(cfg: Config) -> list[Instance]:
    """Registered instances plus any socket or port file found in the runtime dir.

    The latter lets a service you start by hand (listening on the canonical
    socket path) show up and be routed to without registering it.
    """
    found = {(i.service, i.name): i for i in registered(cfg)}
    if cfg.runtime_dir.is_dir():
        for p in cfg.runtime_dir.glob("*/*"):
            if p.suffix in (".sock", ".port") and p.parent.name not in ("hosts", "pids"):
                found.setdefault((p.parent.name, p.stem), Instance(p.parent.name, p.stem))
    return [found[k] for k in sorted(found)]


# ---- naming -----------------------------------------------------------------


def find_marker(start: Path, marker: str) -> Path | None:
    """The nearest directory at or above ``start`` that contains ``marker``."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / marker).exists():
            return d
    return None


def resolve_dir(svc: Service, directory: str | None) -> Path | None:
    if svc.singleton:
        return Path(directory).resolve() if directory else None
    base = Path(directory or os.getcwd())
    if not base.is_dir():
        raise ConfigError(f"{base} is not a directory")
    if svc.marker:
        if base.name == svc.marker:  # given the marker dir itself, e.g. path/to/.beads
            base = base.parent
        found = find_marker(base, svc.marker)
        if found is None:
            raise ConfigError(f"no {svc.marker} found at or above {base}")
        return found
    return base.resolve()


def choose_name(cfg: Config, svc: Service, directory: Path | None, name: str | None = None) -> str:
    """Reuse the name of an instance already serving ``directory``, else derive one.

    A derived name is a slug of the directory's basename, with a short hash
    of the full path appended if that slug is already taken.
    """
    if name:
        return check_label(name)
    if svc.singleton or directory is None:
        return DEFAULT_INSTANCE
    for inst in registered(cfg):
        if inst.service == svc.name and inst.dir == str(directory):
            return inst.name
    slug = slugify(directory.name)
    other = get(cfg, svc.name, slug)
    if other is None or other.dir == str(directory):
        return slug
    return f"{slug[:32]}-{short_hash(str(directory))}"


def unit_name(service: str, instance: str) -> str:
    return f"web-ui-{service}-{instance}"


# ---- endpoints --------------------------------------------------------------


def endpoint(cfg: Config, service: str, name: str) -> tuple[str, object] | None:
    """('unix', path) or ('tcp', (host, port)), or None if nothing is known."""
    svc = cfg.services.get(service)
    sock = cfg.instance_socket(service, name)
    portfile = cfg.instance_portfile(service, name)
    if svc and svc.listen == "tcp" or (not svc and portfile.exists()):
        try:
            return ("tcp", ("127.0.0.1", int(portfile.read_text().strip())))
        except (OSError, ValueError):
            return None
    if svc or sock.exists():
        return ("unix", str(sock))
    return None


def probe(ep: tuple[str, object] | None, timeout: float = 0.5) -> bool:
    if ep is None:
        return False
    kind, addr = ep
    fam = socket.AF_UNIX if kind == "unix" else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(addr)
        except OSError:
            return False
    return True


def is_up(cfg: Config, service: str, name: str) -> bool:
    return probe(endpoint(cfg, service, name))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def expand(template: str, values: dict[str, str]) -> str:
    """Replace ``{key}`` for known keys only, leaving any other braces alone."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def prepare_launch(cfg: Config, inst: Instance) -> Launch:
    """Pick the endpoint, clear stale files and expand the service's command.

    Placeholders: {socket} {port} {dir} {instance} {service} {host} {url} {runtime}.
    """
    svc = cfg.service(inst.service)
    ensure_private_dir(cfg.service_dir(inst.service))
    sock = cfg.instance_socket(inst.service, inst.name)
    portfile = cfg.instance_portfile(inst.service, inst.name)
    port = ""
    if svc.listen == "tcp":
        port = str(free_port())
        portfile.write_text(port + "\n")
    else:
        sock.unlink(missing_ok=True)  # stale from a previous run; we checked it is not live
    values = {
        "socket": str(sock),
        "port": port,
        "dir": inst.dir or "",
        "instance": inst.name,
        "service": inst.service,
        "host": cfg.hostname,
        "url": url_for(cfg.hostname, inst.service, inst.name, cfg.domain, cfg.port).rstrip("/"),
        "runtime": str(cfg.runtime_dir),
    }
    return Launch(
        unit=unit_name(inst.service, inst.name),
        argv=[expand(a, values) for a in svc.command],
        cwd=inst.dir or None,
        env={k: expand(v, values) for k, v in svc.env.items()},
        properties=[expand(p, values) for p in svc.properties],
        description=f"web-ui {inst.service} {inst.name}" + (f" ({inst.dir})" if inst.dir else ""),
    )


class StartError(Exception):
    pass


def start(cfg: Config, inst: Instance, wait: bool = True) -> None:
    """Start an instance unless it is already reachable, then wait until it is."""
    if is_up(cfg, inst.service, inst.name):
        return
    runner = get_runner(cfg)
    unit = unit_name(inst.service, inst.name)
    if runner.is_active(unit):
        # Running but not reachable (yet, or wedged): restart so we know its endpoint.
        runner.stop(unit)
    runner.start(prepare_launch(cfg, inst))
    if not wait:
        return
    timeout = cfg.service(inst.service).start_timeout
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_up(cfg, inst.service, inst.name):
            return
        if not runner.is_active(unit):
            raise StartError(f"{unit} exited during startup; see: web-ui logs {inst.service} {inst.name}")
        time.sleep(0.1)
    raise StartError(f"{unit} did not start listening within {timeout:g}s")


def stop(cfg: Config, service: str, name: str) -> bool:
    stopped = get_runner(cfg).stop(unit_name(service, name))
    cfg.instance_portfile(service, name).unlink(missing_ok=True)
    sock = cfg.instance_socket(service, name)
    if sock.exists() and not probe(("unix", str(sock))):
        sock.unlink(missing_ok=True)
    return stopped
