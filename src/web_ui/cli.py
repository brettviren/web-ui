"""The ``web-ui`` command line."""

from __future__ import annotations

import http.client
import json
import logging
import os
import shlex
import socket
import sys
import time
from pathlib import Path

import click

from web_ui import __version__, instances, systemd, tunnel
from web_ui import config as config_mod
from web_ui import log as log_mod
from web_ui.config import Config, ConfigError
from web_ui.names import DEFAULT_INSTANCE, NameError_, url_for
from web_ui.runner import Launch, RunnerError, get_runner

PROXY_UNIT = "web-ui-proxy"

log = logging.getLogger(__name__)


class Ctx:
    def __init__(self, cfg_path: Path | None):
        self.cfg_path = cfg_path
        self._cfg: Config | None = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            try:
                self._cfg = config_mod.load(self.cfg_path)
            except (ConfigError, NameError_) as e:
                raise click.ClickException(str(e))
            c = self._cfg
            log.debug("config %s: hostname=%s services=%s hosts=%s runner=%s declared=%s",
                      c.path, c.hostname, sorted(c.services), sorted(c.hosts), c.runner,
                      {f"{s}/{n}": d for (s, n), d in sorted(c.declared.items())})  # fmt: skip
        return self._cfg

    def web_ui_argv(self) -> list[str]:
        """How to run web-ui again, with this config and logging, in a child process."""
        argv = [sys.executable, "-m", "web_ui"]
        if self.cfg_path:
            argv += ["--config", str(self.cfg_path)]
        return argv + log_mod.argv()


class Group(click.Group):
    """Logs a command's failure before click reports it."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except click.exceptions.Exit:
            raise
        except click.Abort:
            log.error("aborted")
            raise
        except click.ClickException as e:
            log.error("%s: %s", ctx.invoked_subcommand, e.format_message())
            raise
        except Exception:
            log.critical("%s failed", ctx.invoked_subcommand, exc_info=True)
            raise


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(cls=Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-c", "--config", "cfg_path", type=click.Path(path_type=Path), envvar="WEB_UI_CONFIG",
              help="Config file (default ~/.config/web-ui/config.toml).")  # fmt: skip
@click.option("-l", "--log-level", type=click.Choice(log_mod.LEVELS, case_sensitive=False),
              default=log_mod.DEFAULT_LEVEL, show_default=True, envvar="WEB_UI_LOG_LEVEL",
              help="Least severe level to log.")  # fmt: skip
@click.option("-L", "--log-sink", metavar="SINK", envvar="WEB_UI_LOG_SINK",
              help="'stdout', 'stderr' or a file (default $XDG_STATE_HOME/web-ui/web-ui.log).")  # fmt: skip
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def main(ctx: click.Context, cfg_path: Path | None, log_level: str, log_sink: str | None) -> None:
    """One front door per host for local web UIs.

    Each service instance listens on a private Unix socket.  A small proxy
    routes http://[INSTANCE.]SERVICE.HOST.localhost:PORT/ to it, locally or
    through one SSH-forwarded socket per remote host.
    """
    try:
        log_mod.setup(log_level, log_sink)
    except OSError as e:
        raise click.BadParameter(f"cannot open {e.filename}: {e.strerror}", param_hint="'-L' / '--log-sink'")
    log.debug("%s (web-ui %s, python %s)", shlex.join(sys.argv), __version__, sys.version.split()[0])
    ctx.obj = Ctx(cfg_path)


def _fail(e: Exception) -> click.ClickException:
    return click.ClickException(str(e))


def _resolve(cfg: Config, service: str, directory: str | None, name: str | None):
    """Service config, project dir and instance name for SERVICE [DIR] [--name]."""
    try:
        svc = cfg.service(service)
        if svc.singleton and name and name != DEFAULT_INSTANCE:
            raise ConfigError(f"{service} has one instance; give no --name")
        if name and not directory:
            existing = instances.get(cfg, service, name)
            if existing:
                return svc, existing.dir, existing.name
        d = instances.resolve_dir(svc, directory)
        return svc, (str(d) if d else None), instances.choose_name(cfg, svc, d, name)
    except (ConfigError, NameError_) as e:
        raise _fail(e)


def _url(cfg: Config, service: str, name: str, host: str | None = None) -> str:
    return url_for(host or cfg.hostname, service, name, cfg.domain, cfg.port)


def _unmanaged(cfg: Config, service: str, what: str) -> None:
    """Refuse to start or stop a service web-ui only connects to."""
    svc = cfg.services.get(service)
    if svc and not svc.managed:
        raise click.ClickException(f"{service} is started outside web-ui (connect = {svc.connect!r}); {what} it there")


def _wait_live(cfg: Config) -> None:
    for _ in range(100):
        if _proxy_live(cfg):
            log.debug("detached proxy is live on %s", cfg.front_socket)
            return
        time.sleep(0.1)
    raise click.ClickException("proxy did not start; see: web-ui logs and web-ui logs --proxy")


def _proxy_live(cfg: Config) -> bool:
    return instances.probe(("unix", str(cfg.front_socket)))


# ---- the proxy ---------------------------------------------------------------


@main.command()
@click.option("-p", "--port", type=int, help="Loopback TCP port for browsers (default from config, 8484).")
@click.option("--no-tcp", is_flag=True, help="Only listen on the Unix front socket (a host no browser sits at).")
@click.option("--no-autostart", is_flag=True, help="Do not start instances or tunnels on first request.")
@click.option("-d", "--detach", is_flag=True, help="Start the proxy in the background unless one is running.")
@click.option("--print-socket", is_flag=True, help="With --detach: print the front socket's path (for tunnels).")
@pass_ctx
def serve(obj: Ctx, port: int | None, no_tcp: bool, no_autostart: bool, detach: bool, print_socket: bool) -> None:
    """Run the front-door proxy."""
    cfg = obj.cfg
    if detach:
        if print_socket:
            click.echo(cfg.front_socket)
        if _proxy_live(cfg):
            log.debug("proxy already live on %s", cfg.front_socket)
            return
        if systemd.installed():  # `web-ui install` was run here: start that, not a transient copy
            r = systemd.systemctl("start", f"{PROXY_UNIT}.socket", f"{PROXY_UNIT}.service")
            if r.returncode:
                raise click.ClickException(f"systemctl --user start {PROXY_UNIT}: {(r.stderr or r.stdout).strip()}")
            _wait_live(cfg)
            return
        argv = [*obj.web_ui_argv(), "serve"]
        if port is not None:
            argv += ["--port", str(port)]
        argv += (["--no-tcp"] if no_tcp else []) + (["--no-autostart"] if no_autostart else [])
        runner = get_runner(cfg)
        try:
            runner.start(Launch(unit=PROXY_UNIT, argv=argv, description="web-ui front-door proxy"))
        except RunnerError as e:
            raise _fail(e)
        _wait_live(cfg)
        return
    from web_ui import proxy

    try:
        proxy.run(cfg, port=0 if no_tcp else port, autostart=not no_autostart)
    except RuntimeError as e:
        log.critical("proxy cannot run: %s", e)
        raise _fail(e)


# ---- instances -----------------------------------------------------------------


@main.command()
@click.argument("service")
@click.argument("directory", required=False)
@click.option("-n", "--name", help="Instance name (default: derived from the directory).")
@click.option("--no-start", is_flag=True, help="Only register; the proxy starts it on first visit.")
@click.option("--open", "open_", is_flag=True, help="Open the URL in a browser.")
@pass_ctx
def up(obj: Ctx, service: str, directory: str | None, name: str | None, no_start: bool, open_: bool) -> None:
    """Register and start an instance of SERVICE for DIRECTORY, and print its URL.

    DIRECTORY defaults to the current one.  For a service with a marker (say
    .beads) the nearest directory at or above it holding the marker is used.
    Running it again for the same directory reuses the instance.
    """
    cfg = obj.cfg
    svc, d, inst_name = _resolve(cfg, service, directory, name)
    if not svc.managed:
        url = _url(cfg, service, inst_name)
        click.echo(url)
        if not instances.is_up(cfg, service, inst_name):
            click.echo(f"note: {service} is not running at {svc.connect}; web-ui does not start it", err=True)
        if open_:
            import webbrowser

            webbrowser.open(url)
        return
    existing = instances.get(cfg, service, inst_name)
    if existing and existing.dir != d and directory is not None:
        raise click.ClickException(f"instance {inst_name!r} of {service} already serves {existing.dir}; choose another --name")
    inst = existing or instances.Instance(service, inst_name, d, time.time())
    instances.save(cfg, inst)
    if not existing:
        log.info("registered %s %s for %s", service, inst_name, d)
    if not no_start:
        try:
            instances.start(cfg, inst)
        except (instances.StartError, RunnerError, ConfigError) as e:
            raise _fail(e)
    url = _url(cfg, service, inst_name)
    click.echo(url)
    if not _proxy_live(cfg):
        click.echo("note: no proxy is running here; start one with: web-ui serve --detach", err=True)
    if open_:
        import webbrowser

        webbrowser.open(url)


@main.command()
@click.argument("service")
@click.argument("directory", required=False)
@click.option("-n", "--name", help="Instance name.")
@click.option("--forget", is_flag=True, help="Also remove its registration.")
@pass_ctx
def down(obj: Ctx, service: str, directory: str | None, name: str | None, forget: bool) -> None:
    """Stop an instance (given like 'up': by DIRECTORY or --name)."""
    cfg = obj.cfg
    _unmanaged(cfg, service, "stop")
    _, _, inst_name = _resolve(cfg, service, directory, name)
    if instances.get(cfg, service, inst_name) is None and not instances.is_up(cfg, service, inst_name):
        raise click.ClickException(f"no instance {inst_name!r} of {service}")
    stopped = instances.stop(cfg, service, inst_name)
    msg = "stopped" if stopped else "was not running"
    if forget:
        if instances.forget(cfg, service, inst_name):
            log.info("forgot %s %s", service, inst_name)
            msg += ", forgotten"
        if instances.is_declared(cfg, service, inst_name):
            msg += f" (still declared in {cfg.path})"
    click.echo(f"{service} {inst_name}: {msg}")


@main.command()
@click.argument("service")
@click.argument("directory", required=False)
@click.option("-n", "--name", help="Instance name.")
@click.option("--host", help="Host label for the URL (default: this host).")
@pass_ctx
def url(obj: Ctx, service: str, directory: str | None, name: str | None, host: str | None) -> None:
    """Print an instance's URL without starting it."""
    cfg = obj.cfg
    if host and not cfg.is_self(host):
        click.echo(url_for(host, service, name or DEFAULT_INSTANCE, cfg.domain, cfg.port))
        return
    _, _, inst_name = _resolve(cfg, service, directory, name)
    click.echo(_url(cfg, service, inst_name))


@main.command()
@click.argument("service")
@click.argument("directory", required=False)
@click.option("-n", "--name", help="Instance name.")
@pass_ctx
def run(obj: Ctx, service: str, directory: str | None, name: str | None) -> None:
    """Run an instance in the foreground (for debugging its command)."""
    cfg = obj.cfg
    _unmanaged(cfg, service, "run")
    _, d, inst_name = _resolve(cfg, service, directory, name)
    if instances.is_up(cfg, service, inst_name):
        raise click.ClickException(f"{service} {inst_name} is already running")
    inst = instances.get(cfg, service, inst_name) or instances.Instance(service, inst_name, d, time.time())
    launch = instances.prepare_launch(cfg, inst)
    if launch.properties:
        click.echo(f"note: systemd properties are not applied in the foreground: {launch.properties}", err=True)
    click.echo(f"$ {shlex.join(launch.argv)}", err=True)
    click.echo(f"  {_url(cfg, service, inst_name)}", err=True)
    log.debug("exec %s in %s with env %s", launch.argv, launch.cwd, launch.env)
    if launch.cwd:
        os.chdir(launch.cwd)
    try:
        os.execvpe(launch.argv[0], launch.argv, {**os.environ, **launch.env})
    except OSError as e:
        raise click.ClickException(f"{launch.argv[0]}: {e.strerror}")


def _remote_status(cfg: Config, host: str) -> dict | None:
    class UnixHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(str(cfg.host_socket(host)))

    conn = UnixHTTP(f"{host}.{cfg.domain}")
    try:
        conn.request("GET", "/status.json", headers={"Host": f"{host}.{cfg.domain}:{cfg.port}"})
        resp = conn.getresponse()
        if resp.status != 200:
            log.debug("status of %s: HTTP %d", host, resp.status)
            return None
        return json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException) as e:
        log.debug("status of %s: %s", host, e)
        return None
    finally:
        conn.close()


@main.command("ls")
@click.option("-a", "--all", "all_hosts", is_flag=True, help="Also list instances on hosts with a live tunnel.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@pass_ctx
def ls(obj: Ctx, all_hosts: bool, as_json: bool) -> None:
    """List instances (and with -a, those on tunneled hosts)."""
    from web_ui.proxy import Proxy

    cfg = obj.cfg
    p = Proxy(cfg, autostart=False)
    report = [p.status(cfg.port)]
    if all_hosts:
        for h in report[0]["hosts"]:
            if h["tunnel"]:
                st = _remote_status(cfg, h["host"])
                if st:
                    report.append(st)
    if as_json:
        click.echo(json.dumps(report if all_hosts else report[0], indent=2))
        return
    rows = [(st["host"], i["service"], i["instance"], "up" if i["up"] else "-", i["url"], i["dir"] or "")
            for st in report for i in st["instances"]]  # fmt: skip
    if not rows:
        click.echo("no instances")
    else:
        widths = [max(len(r[c]) for r in rows) for c in range(5)]
        for r in rows:
            click.echo("  ".join(r[c].ljust(widths[c]) for c in range(5)) + "  " + r[5])
    hosts = report[0]["hosts"]
    if hosts and not as_json:
        click.echo("\nhosts: " + ", ".join(f"{h['host']}{'' if h['tunnel'] else ' (no tunnel)'}" for h in hosts))


@main.command()
@click.argument("service", required=False)
@click.argument("instance", required=False)
@click.option("--proxy", "of_proxy", is_flag=True, help="Output of the background proxy process.")
@click.option("--tunnel", "of_tunnel", metavar="HOST", help="Output of the tunnel to HOST (ssh's errors).")
@click.option("-f", "--follow", is_flag=True)
@click.option("-n", "--lines", default=200, show_default=True, help="How many lines to show.")
@pass_ctx
def logs(obj: Ctx, service: str | None, instance: str | None, of_proxy: bool, of_tunnel: str | None,
         follow: bool, lines: int) -> None:  # fmt: skip
    """Show web-ui's log, or the output of an instance, the proxy or a tunnel.

    With no arguments, show web-ui's own log file (the -L sink): the proxy's
    access log and what web-ui did.  SERVICE [INSTANCE], --proxy and --tunnel
    show what that process wrote to its stdout and stderr.
    """
    cfg = obj.cfg
    if of_proxy:
        unit = PROXY_UNIT
    elif of_tunnel:
        unit = tunnel.unit_name(of_tunnel)
    elif service:
        _unmanaged(cfg, service, "find its logs")
        unit = instances.unit_name(service, instance or DEFAULT_INSTANCE)
    else:
        path = log_mod.current_file()
        if path is None:
            raise click.UsageError("the log sink is a stream, not a file; give -L FILE, or SERVICE, --proxy or --tunnel")
        cmd = ["tail", "-n", str(lines), *(["-F"] if follow else []), str(path)]
        os.execvp(cmd[0], cmd)
    cmd = get_runner(cfg).logs_command(unit, follow, lines)
    log.debug("exec %s", cmd)
    os.execvp(cmd[0], cmd)


# ---- hosts -------------------------------------------------------------------


@main.command("tunnel")
@click.argument("host")
@click.option("--batch", is_flag=True, help="Never prompt (ssh BatchMode); used when started by the proxy.")
@click.option("--print", "print_only", is_flag=True, help="Print the ssh commands instead of running them.")
@click.option("--no-ensure", is_flag=True, help="Do not start the remote proxy first.")
@pass_ctx
def tunnel_cmd(obj: Ctx, host: str, batch: bool, print_only: bool, no_ensure: bool) -> None:
    """Forward HOST's front door to hosts/HOST.sock here (runs until ssh exits).

    First runs 'web-ui serve --detach' on HOST so its proxy is up.
    """
    cfg = obj.cfg
    hcfg = cfg.host(host)
    ensure = tunnel.ensure_remote_argv(hcfg, batch) if hcfg.ensure_remote and not no_ensure else None
    log.debug("tunnel to %s: ssh=%s ensure=%s", host, hcfg.ssh, ensure)
    if print_only:
        fwd = tunnel.forward_argv(cfg, hcfg, batch)
        if ensure:
            click.echo(tunnel.describe(ensure))
        click.echo(tunnel.describe(fwd))
        return
    if instances.probe(("unix", str(cfg.host_socket(host)))):
        raise click.ClickException(f"a tunnel to {host} is already up at {cfg.host_socket(host)}")
    config_mod.ensure_private_dir(cfg.runtime_dir)
    config_mod.ensure_private_dir(cfg.hosts_dir)
    remote_sock = None
    if ensure:
        import subprocess

        log.debug("running %s", tunnel.describe(ensure))
        try:
            r = subprocess.run(ensure, timeout=60, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
            log.debug("remote proxy on %s: exit %d, output %r", host, r.returncode, r.stdout)
            if r.returncode != 0:
                log.warning("could not start the proxy on %s (ssh exit %d)", host, r.returncode)
                click.echo(f"warning: could not start the proxy on {host} (exit {r.returncode})", err=True)
            remote_sock = tunnel.parse_front_socket(r.stdout)
        except subprocess.TimeoutExpired:
            log.warning("timed out starting the proxy on %s", host)
            click.echo(f"warning: timed out starting the proxy on {host}", err=True)
        if remote_sock is None:
            log.debug("%s did not report its front socket; assuming %s", host, tunnel.remote_front_socket(hcfg))
    fwd = tunnel.forward_argv(cfg, hcfg, batch, remote_sock)
    log.info("tunnel to %s: %s", host, tunnel.describe(fwd))
    os.execvp(fwd[0], fwd)


@main.command()
@pass_ctx
def hosts(obj: Ctx) -> None:
    """List remote hosts: up, - (no tunnel), or dead (tunnel up, its proxy not answering)."""
    cfg = obj.cfg
    names = set(cfg.hosts)
    if cfg.hosts_dir.is_dir():
        names |= {p.stem for p in cfg.hosts_dir.glob("*.sock")}
    for h in sorted(names):
        live = instances.probe(("unix", str(cfg.host_socket(h))))
        state = ("up" if tunnel.remote_answers(cfg, h, timeout=3) else "dead") if live else "-"
        hc = cfg.host(h)
        auto = " autotunnel" if hc.autotunnel else ""
        click.echo(f"{h:20} {state:4} ssh={hc.ssh}{auto}  {url_for(h, domain=cfg.domain, port=cfg.port)}")


# ---- setup helpers -----------------------------------------------------------


@main.group()
def gen() -> None:
    """Generate config for other tools."""


@gen.command("ssh-config")
@pass_ctx
def gen_ssh_config(obj: Ctx) -> None:
    """An ~/.ssh/config stanza doing what 'web-ui tunnel' does."""
    click.echo(tunnel.ssh_config_snippet(obj.cfg), nl=False)


@gen.command("systemd")
@click.option("-o", "--output", type=click.Path(file_okay=False, path_type=Path),
              help="Write unit files here (e.g. ~/.config/systemd/user) instead of printing them.")  # fmt: skip
@click.option("--no-tcp", is_flag=True, help="The proxy serves only its Unix socket (a host no browser sits at).")
@pass_ctx
def gen_systemd(obj: Ctx, output: Path | None, no_tcp: bool) -> None:
    """Print the user units `web-ui install` would write (or write them to --output).

    web-ui-proxy.socket (front.sock and the loopback port, held by systemd),
    web-ui-proxy.service (Type=notify, socket-activated) and
    web-ui-tunnel@.service.  `web-ui install` also enables and starts them.
    """
    units = systemd.units(obj.cfg, obj.web_ui_argv(), tcp=not no_tcp, process_runner=_process_runner(obj.cfg))
    if output is None:
        for name, text in units.items():
            click.echo(f"# ---- {name}\n{text}")
        return
    output.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (output / name).write_text(text)
        click.echo(output / name)
    click.echo(f"then: systemctl --user daemon-reload && systemctl --user enable --now {PROXY_UNIT}.socket", err=True)


def _process_runner(cfg: Config) -> bool:
    try:
        return get_runner(cfg).name == "process"
    except RunnerError:
        return True


@main.command("install")
@click.option("--no-tcp", is_flag=True, help="The proxy serves only its Unix socket (a host no browser sits at).")
@pass_ctx
def install_cmd(obj: Ctx, no_tcp: bool) -> None:
    """Run the proxy as a persistent systemd user service (safe to repeat).

    Writes web-ui-proxy.socket, web-ui-proxy.service and web-ui-tunnel@.service
    to ~/.local/share/systemd/user, replaces a transient proxy started by
    `serve --detach`, and enables and starts them.  systemd then holds
    front.sock and the loopback port: the proxy starts at login (at boot with
    `loginctl enable-linger`), and while it restarts (`systemctl --user
    restart web-ui-proxy`, or a changed unit) new connections wait rather
    than fail.  Instances keep running across proxy restarts.
    """
    try:
        done = systemd.install(obj.cfg, obj.web_ui_argv(), tcp=not no_tcp, process_runner=_process_runner(obj.cfg))
    except RuntimeError as e:
        raise _fail(e)
    for line in done:
        click.echo(line)
    _wait_live(obj.cfg)
    click.echo(f"{PROXY_UNIT} is running" + (f"; http://localhost:{obj.cfg.port}/" if obj.cfg.port and not no_tcp else ""))


@main.command("uninstall")
@pass_ctx
def uninstall_cmd(obj: Ctx) -> None:
    """Stop the installed proxy service and remove the units `web-ui install` wrote."""
    for line in systemd.uninstall() or ["nothing installed"]:
        click.echo(line)


@main.command("config")
@pass_ctx
def config_cmd(obj: Ctx) -> None:
    """Show the effective configuration and paths."""
    cfg = obj.cfg
    exists = "" if cfg.path and cfg.path.exists() else " (not found; using defaults)"
    click.echo(f"config file:  {cfg.path}{exists}")
    click.echo(f"hostname:     {cfg.hostname}" + (f" (aliases: {', '.join(cfg.aliases)})" if cfg.aliases else ""))
    click.echo(f"browser URL:  http://localhost:{cfg.port}/" if cfg.port else "browser URL:  (no TCP listener)")
    click.echo(f"front socket: {cfg.front_socket}  ({'live' if _proxy_live(cfg) else 'not running'})")
    click.echo(f"instances:    {cfg.instances_dir}")
    click.echo(f"runner:       {get_runner(cfg).name}")
    click.echo(f"services:     {', '.join(sorted(cfg.services)) or '(none)'}")
    for svc in sorted(cfg.services.values(), key=lambda s: s.name):
        if svc.connect:
            how = "started by web-ui" if svc.managed else "started elsewhere"
            click.echo(f"connect:      {svc.name} at {svc.connect} ({how})")
    if cfg.elsewhere:
        click.echo("elsewhere:    " + ", ".join(f"{n} ({', '.join(h)})" for n, h in sorted(cfg.elsewhere.items())))
    click.echo(f"hosts:        {', '.join(sorted(cfg.hosts)) or '(none)'}")
    if cfg.bin_dirs:
        click.echo(f"bin_dirs:     {', '.join(cfg.bin_dirs)}")
    for (svc, name), d in sorted(cfg.declared.items()):
        click.echo(f"declared:     {_url(cfg, svc, name)}  {d}")
