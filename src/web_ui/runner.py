"""Start and stop background processes (service instances, tunnels, the proxy).

Two runners share one interface:

- ``systemd`` starts a transient user unit with ``systemd-run --user``, so the
  process is supervised, logs go to the journal, and systemd sandboxing
  properties (``ProtectHome=``, ``BindReadOnlyPaths=``, ...) apply.
- ``process`` starts a detached child, writes a pid file under the runtime
  dir and logs to ``$XDG_STATE_HOME/web-ui/logs``.  It works anywhere.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from web_ui.config import Config, ensure_private_dir

log = logging.getLogger(__name__)


@dataclass
class Launch:
    unit: str  # unit name, e.g. web-ui-maggie-projx (no .service suffix)
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    properties: list[str] = field(default_factory=list)  # systemd only
    description: str = ""


class RunnerError(Exception):
    pass


class SystemdRunner:
    name = "systemd"

    def _systemctl(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)

    def is_active(self, unit: str) -> bool:
        r = self._systemctl("is-active", "--quiet", f"{unit}.service")
        return r.returncode == 0

    def start(self, launch: Launch) -> None:
        if self.is_active(launch.unit):
            log.debug("%s is already active", launch.unit)
            return
        # A failed transient unit lingers until reset; clear it so the name is reusable.
        self._systemctl("reset-failed", f"{launch.unit}.service")
        cmd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={launch.unit}"]
        if launch.description:
            cmd.append(f"--description={launch.description}")
        if launch.cwd:
            cmd.append(f"--working-directory={launch.cwd}")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **launch.env}
        cmd += [f"--setenv={k}={v}" for k, v in env.items()]
        cmd += [f"--property={p}" for p in launch.properties]
        cmd += ["--", *launch.argv]
        log.debug("running %s", cmd)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log.error("systemd-run failed for %s (exit %d): %s", launch.unit, r.returncode, r.stderr.strip())
            raise RunnerError(f"systemd-run failed for {launch.unit}: {r.stderr.strip()}")
        log.debug("started unit %s.service", launch.unit)

    def stop(self, unit: str) -> bool:
        if not self.is_active(unit):
            self._systemctl("reset-failed", f"{unit}.service")
            log.debug("%s is not active", unit)
            return False
        r = self._systemctl("stop", f"{unit}.service")
        if r.returncode != 0:
            log.error("systemctl stop %s failed (exit %d): %s", unit, r.returncode, r.stderr.strip())
        log.debug("stopped unit %s.service", unit)
        return True

    def logs_command(self, unit: str, follow: bool = False, lines: int = 200) -> list[str]:
        cmd = ["journalctl", "--user", "-u", f"{unit}.service", "-n", str(lines), "--no-pager"]
        return cmd + (["-f"] if follow else [])


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a live process: a zombie (exited, unreaped by its parent) is not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] != "Z"
    except (OSError, IndexError):
        return True


class ProcessRunner:
    name = "process"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _pidfile(self, unit: str) -> Path:
        return self.cfg.pid_dir / f"{unit}.pid"

    def _logfile(self, unit: str) -> Path:
        return self.cfg.log_dir / f"{unit}.log"

    def _pid(self, unit: str) -> int | None:
        try:
            pid = int(self._pidfile(unit).read_text().strip())
        except (OSError, ValueError):
            return None
        try:  # reap it if it is our exited child, otherwise its zombie looks alive
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return None
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            log.debug("%s: pid %d is gone", unit, pid)
            return None
        except PermissionError:
            log.debug("%s: pid %d now belongs to another user", unit, pid)
            return None  # pid reused by someone else's process
        if not _alive(pid):
            log.debug("%s: pid %d has exited", unit, pid)
            return None
        return pid

    def is_active(self, unit: str) -> bool:
        return self._pid(unit) is not None

    def start(self, launch: Launch) -> None:
        if self.is_active(launch.unit):
            log.debug("%s is already running", launch.unit)
            return
        if launch.properties:
            log.warning("%s: systemd properties ignored by the process runner: %s", launch.unit, launch.properties)
        ensure_private_dir(self.cfg.pid_dir)
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(self._logfile(launch.unit), "ab")
        log.debug("running %s in %s with env %s, output to %s", launch.argv, launch.cwd, launch.env, logf.name)
        try:
            proc = subprocess.Popen(
                launch.argv,
                cwd=launch.cwd,
                env={**os.environ, **launch.env},
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            log.error("cannot start %s for %s: %s", launch.argv[0], launch.unit, e)
            raise RunnerError(f"cannot start {launch.argv[0]}: {e}") from e
        finally:
            logf.close()
        self._pidfile(launch.unit).write_text(f"{proc.pid}\n")
        log.debug("started %s as pid %d", launch.unit, proc.pid)

    def stop(self, unit: str) -> bool:
        pid = self._pid(unit)
        self._pidfile(unit).unlink(missing_ok=True)
        if pid is None:
            log.debug("%s is not running", unit)
            return False
        log.debug("stopping %s (pid %d)", unit, pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        for _ in range(50):
            time.sleep(0.1)
            # reap if it is our child (it is, when started from this process)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if not _alive(pid):  # gone, or a zombie its parent (say the proxy) will reap
                return True
        log.warning("%s (pid %d) ignored SIGTERM; killing it", unit, pid)
        os.killpg(pid, signal.SIGKILL)
        return True

    def logs_command(self, unit: str, follow: bool = False, lines: int = 200) -> list[str]:
        return ["tail", "-n", str(lines), *(["-F"] if follow else []), str(self._logfile(unit))]


def systemd_available() -> bool:
    if not shutil.which("systemd-run"):
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return r.stdout.strip() in ("running", "degraded")


def get_runner(cfg: Config) -> SystemdRunner | ProcessRunner:
    if cfg.runner == "systemd" or (cfg.runner == "auto" and systemd_available()):
        runner: SystemdRunner | ProcessRunner = SystemdRunner()
    else:
        runner = ProcessRunner(cfg)
    log.debug("runner: %s (configured %s)", runner.name, cfg.runner)
    return runner
