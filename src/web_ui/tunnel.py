"""SSH tunnels: one forwarded Unix socket per remote host.

``$XDG_RUNTIME_DIR/web-ui/hosts/<host>.sock`` here is forwarded to the remote
host's ``front.sock``, so every service and instance on that host rides one
connection.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

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
    base = host.remote_runtime_dir or str(runtime_root())
    return f"{base}/web-ui/front.sock"


def ssh_options(host: Host, batch: bool) -> list[str]:
    return SSH_BASE + (["-o", "BatchMode=yes"] if batch else []) + list(host.ssh_options)


def forward_argv(cfg: Config, host: Host, batch: bool = False) -> list[str]:
    local = cfg.host_socket(host.name)
    return ["ssh", "-N", "-T", *ssh_options(host, batch), "-L", f"{local}:{remote_front_socket(host)}", host.ssh]


def ensure_remote_argv(host: Host, batch: bool = True) -> list[str]:
    """Ask the remote host to start its proxy if it is not already running."""
    return ["ssh", "-T", *ssh_options(host, batch), host.ssh, f"{host.web_ui} serve --detach"]


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
    """An ~/.ssh/config stanza equivalent to ``web-ui tunnel``, for plain ``ssh -N``."""
    names = " ".join(sorted(cfg.hosts)) or "hostA hostB"
    local = Path("/run/user/%i/web-ui/hosts/%n.sock")
    return f"""\
# Forward this seat's per-host socket to the host's web-ui front door.
# %i is your uid (the same on every host under FreeIPA), %n the name you typed.
# Use with:  ssh -N -o ControlMaster=no -o ControlPath=none <host>
Host {names}
  StreamLocalBindUnlink yes
  StreamLocalBindMask 0177
  ExitOnForwardFailure yes
  LocalForward {local} /run/user/%i/web-ui/front.sock
"""


def describe(argv: list[str]) -> str:
    return shlex.join(argv)
