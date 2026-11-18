"""SSH tunnels: one forwarded Unix socket per remote host.

``$XDG_RUNTIME_DIR/web-ui/hosts/<host>.sock`` here is forwarded to the remote
host's ``front.sock``, so every service and instance on that host rides one
connection.
"""

from __future__ import annotations

import shlex
import sys

from web_ui.config import Config, Host, runtime_root
from web_ui.runner import Launch

SSH_BASE = [
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StreamLocalBindUnlink=yes",
    "-o", "StreamLocalBindMask=0177",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    # Our own connection, not a ControlMaster mux client: its lifetime is the tunnel's.
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
]  # fmt: skip


def remote_front_socket(host: Host) -> str:
    """Where the remote front socket is assumed to be, absent a report from the remote.

    The default assumes the same uid there as here (true under FreeIPA with
    the same login name); set remote_runtime_dir otherwise.
    """
    base = host.remote_runtime_dir or str(runtime_root())
    return f"{base}/web-ui/front.sock"


def parse_front_socket(output: str) -> str | None:
    """The path printed by the remote's 'serve --detach --print-socket'."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("/") and line.endswith("/web-ui/front.sock"):
            return line
    return None


def ssh_options(host: Host, batch: bool) -> list[str]:
    return SSH_BASE + (["-o", "BatchMode=yes"] if batch else []) + list(host.ssh_options)


def forward_argv(cfg: Config, host: Host, batch: bool = False, remote: str | None = None) -> list[str]:
    local = cfg.host_socket(host.name)
    remote = remote or remote_front_socket(host)
    return ["ssh", "-N", "-T", *ssh_options(host, batch), "-L", f"{local}:{remote}", host.ssh]


def ensure_remote_argv(host: Host, batch: bool = True) -> list[str]:
    """Ask the remote host to start its proxy if needed, and where its socket is."""
    return ["ssh", "-T", *ssh_options(host, batch), host.ssh, f"{host.web_ui} serve --detach --print-socket"]


def unit_name(host: str) -> str:
    return f"web-ui-tunnel-{host}"


def launch(cfg: Config, host: Host) -> Launch:
    """Run ``web-ui tunnel --batch HOST`` in the background."""
    return Launch(
        unit=unit_name(host.name),
        argv=[sys.executable, "-m", "web_ui", "tunnel", "--batch", host.name],
        description=f"web-ui tunnel to {host.ssh}",
    )


def ssh_config_snippet(cfg: Config) -> str:
    """~/.ssh/config blocks doing what ``web-ui tunnel`` does, for plain ssh.

    They apply only to ``ssh -P web-ui -N HOST``, so ordinary sessions to the
    same host don't fight over the forwarded socket.
    """
    out = [
        "# web-ui forwards: used by `ssh -P web-ui -N HOST`, ignored by plain `ssh HOST`.",
        "# Put these before other blocks: for most options the first value found wins.",
        "# The remote path is /run/user/<uid on HOST>/web-ui/front.sock; check it with",
        "#   ssh HOST web-ui serve --detach --print-socket",
        "Match tagged web-ui",
        "  ControlMaster no",
        "  ControlPath none",
        "  SessionType none",
        "  ExitOnForwardFailure yes",
        "  StreamLocalBindUnlink yes",
        "  StreamLocalBindMask 0177",
        "  ServerAliveInterval 30",
    ]
    for name in sorted(cfg.hosts) or ["HOST"]:
        remote = remote_front_socket(cfg.hosts[name]) if name in cfg.hosts else "/run/user/%i/web-ui/front.sock"
        out += [
            f"Match tagged web-ui originalhost {name}",
            f"  LocalForward /run/user/%i/web-ui/hosts/%n.sock {remote}",
        ]
    return "\n".join(out) + "\n"


def describe(argv: list[str]) -> str:
    return shlex.join(argv)
