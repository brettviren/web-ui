"""Configuration and well-known paths.

The config file is TOML at ``$WEB_UI_CONFIG`` or
``$XDG_CONFIG_HOME/web-ui/config.toml``.  Everything has a default, so a
missing file is fine.  See ``examples/config.toml``.
"""

from __future__ import annotations

import os
import shlex
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from web_ui.names import check_label


class ConfigError(Exception):
    pass


def _xdg(var: str, fallback: str) -> Path:
    val = os.environ.get(var)
    return Path(val) if val else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "web-ui"


def config_file() -> Path:
    val = os.environ.get("WEB_UI_CONFIG")
    return Path(val) if val else config_dir() / "config.toml"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "web-ui"


def runtime_root() -> Path:
    """The per-user runtime dir, ``$XDG_RUNTIME_DIR`` or ``/run/user/<uid>``."""
    val = os.environ.get("XDG_RUNTIME_DIR")
    return Path(val) if val else Path(f"/run/user/{os.getuid()}")


def short_hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


@dataclass
class Service:
    name: str
    command: list[str]
    listen: str = "unix"  # "unix": service listens on {socket}; "tcp": on 127.0.0.1:{port}
    marker: str | None = None  # e.g. ".beads": instance dir is found by searching upward
    singleton: bool = False  # one instance ("default"), no directory
    rewrite_host: bool = False  # send Host: 127.0.0.1:<port> (or localhost) to the backend
    properties: list[str] = field(default_factory=list)  # systemd properties, {dir} etc. expanded
    env: dict[str, str] = field(default_factory=dict)
    start_timeout: float = 20.0
    autostart: bool = True
    hosts: list[str] = field(default_factory=list)  # only offered on these hosts (default: all)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> Service:
        cmd = d.get("command")
        if cmd is None:
            raise ConfigError(f"service {name!r} has no 'command'")
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        listen = d.get("listen", "unix")
        if listen not in ("unix", "tcp"):
            raise ConfigError(f"service {name!r}: listen must be 'unix' or 'tcp', not {listen!r}")
        known = {f for f in cls.__dataclass_fields__} - {"name"}
        unknown = set(d) - known
        if unknown:
            raise ConfigError(f"service {name!r}: unknown keys {sorted(unknown)}")
        kw = {k: v for k, v in d.items() if k in known}
        kw["command"] = list(cmd)
        return cls(name=name, **kw)


@dataclass
class Host:
    name: str  # the label used in URLs
    ssh: str  # ssh destination
    remote_runtime_dir: str | None = None  # default: same path as the local runtime dir
    autotunnel: bool = True
    web_ui: str = "web-ui"  # how to run web-ui on the remote host
    ensure_remote: bool = True  # run '<web_ui> serve --detach' on the remote before tunneling
    ssh_options: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> Host:
        known = {f for f in cls.__dataclass_fields__} - {"name"}
        unknown = set(d) - known
        if unknown:
            raise ConfigError(f"host {name!r}: unknown keys {sorted(unknown)}")
        kw = {k: v for k, v in d.items() if k in known}
        kw.setdefault("ssh", name)
        return cls(name=name, **kw)


@dataclass
class Config:
    hostname: str = field(default_factory=short_hostname)
    aliases: list[str] = field(default_factory=list)
    domain: str = "localhost"
    port: int = 8484  # seat-side TCP listener on loopback; 0 disables it
    runner: str = "auto"  # systemd | process | auto
    check_uid: bool = True  # only serve connections from our own uid
    strict_origin: bool = True  # reject cross-origin unsafe requests and websockets
    bin_dirs: list[str] = field(default_factory=list)  # prepended to PATH for launched services
    services: dict[str, Service] = field(default_factory=dict)  # those offered on this host
    hosts: dict[str, Host] = field(default_factory=dict)
    # Instances the config declares for this host: (service, name) -> directory.
    declared: dict[tuple[str, str], str] = field(default_factory=dict)
    # Services configured only for other hosts: name -> those hosts.
    elsewhere: dict[str, list[str]] = field(default_factory=dict)
    path: Path | None = None

    # ---- paths -------------------------------------------------------------

    @property
    def runtime_dir(self) -> Path:
        return runtime_root() / "web-ui"

    @property
    def front_socket(self) -> Path:
        return self.runtime_dir / "front.sock"

    @property
    def hosts_dir(self) -> Path:
        return self.runtime_dir / "hosts"

    def host_socket(self, host: str) -> Path:
        return self.hosts_dir / f"{host}.sock"

    def service_dir(self, service: str) -> Path:
        return self.runtime_dir / service

    def instance_socket(self, service: str, instance: str) -> Path:
        return self.service_dir(service) / f"{instance}.sock"

    def instance_portfile(self, service: str, instance: str) -> Path:
        return self.service_dir(service) / f"{instance}.port"

    @property
    def instances_dir(self) -> Path:
        return config_dir() / "instances"

    @property
    def log_dir(self) -> Path:
        return state_dir() / "logs"

    @property
    def pid_dir(self) -> Path:
        return self.runtime_dir / "pids"

    # ---- names -------------------------------------------------------------

    def is_self(self, label: str) -> bool:
        return label == self.hostname or label in self.aliases

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError:
            if name in self.elsewhere:
                raise ConfigError(f"service {name!r} is only configured for {', '.join(self.elsewhere[name])}")
            known = ", ".join(sorted(self.services)) or "none configured"
            raise ConfigError(f"unknown service {name!r} (known: {known}); add [services.{name}] to {config_file()}")

    def host(self, name: str) -> Host:
        """Config for a remote host; hosts not in the config get defaults."""
        return self.hosts.get(name) or Host(name=name, ssh=name, autotunnel=False)


def load(path: Path | None = None) -> Config:
    path = path or config_file()
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
    services = {n: Service.from_dict(n, d) for n, d in data.pop("services", {}).items()}
    hosts = {n: Host.from_dict(n, d) for n, d in data.pop("hosts", {}).items()}
    declared = data.pop("instances", {})
    known = {f for f in Config.__dataclass_fields__} - {"services", "hosts", "path", "declared", "elsewhere"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown top-level keys {sorted(unknown)}")
    cfg = Config(hosts=hosts, path=path, **data)
    for name, svc in services.items():
        if not svc.hosts or any(cfg.is_self(h) for h in svc.hosts):
            cfg.services[name] = svc
        else:
            cfg.elsewhere[name] = svc.hosts
    cfg.declared = _declared(cfg, declared, services)
    if cfg.runner not in ("auto", "systemd", "process"):
        raise ConfigError(f"runner must be auto, systemd or process, not {cfg.runner!r}")
    for n in list(services) + [cfg.hostname] + cfg.aliases + list(hosts):
        check_label(n)
    return cfg


def _declared(cfg: Config, table: dict, services: dict[str, Service]) -> dict[tuple[str, str], str]:
    """Parse ``[instances.<host>.<service>] <name> = "<dir>"``, keeping this host's.

    One config file can then be shared by every host, each picking its own.
    A directory naming the service's marker (``.../.beads``) means its parent.
    """
    out = {}
    for host, by_service in table.items():
        check_label(host)
        if not isinstance(by_service, dict):
            raise ConfigError(f"[instances.{host}] must be a table of services")
        for sname, names in by_service.items():
            if sname not in services:
                raise ConfigError(f"[instances.{host}.{sname}]: no service {sname!r}")
            if not isinstance(names, dict):
                raise ConfigError(f"[instances.{host}.{sname}] must map instance names to directories")
            for iname, directory in names.items():
                check_label(iname)
                if not cfg.is_self(host):
                    continue
                if sname not in cfg.services:
                    raise ConfigError(f"[instances.{host}.{sname}]: {sname} is not offered on {host}")
                d = Path(str(directory)).expanduser()
                marker = services[sname].marker
                if marker and d.name == marker:
                    d = d.parent
                out[(sname, iname)] = str(d)
    return out


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path
