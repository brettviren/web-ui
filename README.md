# web-ui

One front door per host for local web UIs (agentsview, maggie, wckb, syncthing, ...),
reached from any seat through one SSH tunnel per host.  See
[docs/design.md](docs/design.md) for how it works and `ideas.md` for the
reasoning.

```
browser ──tcp──▶ seat proxy (127.0.0.1:8484)
                    ├── *.<seat>.localhost  ──▶ $XDG_RUNTIME_DIR/web-ui/<svc>/<inst>.sock
                    └── *.<hostA>.localhost ──▶ web-ui/hosts/hostA.sock ══ssh══▶ hostA:web-ui/front.sock
                                                                                └─▶ hostA proxy ──▶ <svc>/<inst>.sock
```

URLs look like `http://[INSTANCE.]SERVICE.HOST.localhost:8484/`.  Browsers
resolve `*.localhost` to loopback with no DNS setup, and every instance is its
own origin, so cookies and storage stay separate.

## Install

```sh
uv tool install git+https://github.com/brettviren/web-ui
wget -O ~/.config/web-ui/config.toml https://raw.githubusercontent.com/brettviren/web-ui/refs/heads/main/examples/config.toml  # and edit
```

Install it on every host and seat.  One config file can be copied to all of
them: each host takes its name from its hostname and uses the services and
`[instances.<host>]` meant for it.  `examples/ssh_config` shows the
equivalent plain-ssh setup, if you prefer it to `web-ui tunnel`.

## Use

```sh
web-ui install                 # the front door on this machine, as a systemd user service
# (or, without installing it: web-ui serve --detach)
cd ~/proj/foo && web-ui up maggie
#  → http://foo.maggie.hostA.localhost:8484/
web-ui ls -a                   # instances here and on tunneled hosts
web-ui down maggie --forget    # stop it (DIR or --name, like up)
web-ui tunnel hostA            # forward hostA's front door (runs ssh in the foreground)
web-ui logs -f                 # web-ui's own log: proxy access log, starts and stops
web-ui logs maggie foo -f      # an instance's output
```

At a seat, open `http://localhost:8484/` for a landing page that lists the
instances and hosts.  Usually you don't need to start anything by hand:

- **Instances start on first visit.**  A registered instance (from `web-ui up
  --no-start`, or one that has since stopped) and any `singleton` service are
  started when their URL is requested.
- **Tunnels open on first visit** for hosts in `[hosts.*]` with
  `autotunnel = true` (needs non-interactive ssh auth: an agent or Kerberos).
  The tunnel first runs `web-ui serve --detach` on the host.
- **Hosts are revived.**  If a host's proxy stops while its tunnel stays up,
  the next request (without a body) restarts it, and the tunnel if needed,
  and is retried.  `web-ui hosts` says `dead` for such a tunnel.

## How it works

- **Sockets, not ports.**  Each instance listens on a Unix socket under
  `$XDG_RUNTIME_DIR/web-ui/` (mode 0700), so other users can't reach it and
  there is nothing to collide with.  Services that only speak TCP (agentsview)
  get a fresh ephemeral loopback port per start, recorded in `<inst>.port`.
- **One proxy per machine** (`web-ui serve`), pure Python with no
  dependencies.  It reads each request's head, routes on `Host`, then splices
  bytes, so SSE and WebSockets pass through.  It listens on `front.sock`
  (anything arriving there is for this host) and, at a seat, on one loopback
  port (routes by the host label).
- **One SSH forward per remote host**: `hosts/<host>.sock` here → `front.sock`
  there (`web-ui tunnel`, or the stanza from `web-ui gen ssh-config`).
- **Persistent proxy.**  `web-ui install` writes `web-ui-proxy.socket`,
  `web-ui-proxy.service` and `web-ui-tunnel@.service` to
  `~/.local/share/systemd/user` and starts them (safe to repeat; `web-ui
  uninstall` undoes it).  systemd holds `front.sock` and the loopback port,
  so the proxy starts at login (at boot with `loginctl enable-linger`), and
  while it restarts new connections wait rather than fail; a stopping proxy
  lets open requests finish for a few seconds.  It is `Type=notify`, and
  `systemctl --user reload web-ui-proxy` re-reads the config.  Once
  installed, `serve --detach` (and so a tunnel reviving a host) starts the
  installed units instead of a transient copy.
- **Processes** run as transient systemd user units (`systemd-run --user`,
  logs in the journal, sandboxing via `properties`) or, with
  `runner = "process"`, as detached children logging to
  `~/.local/state/web-ui/logs/`.
- **Instances** are declared per host in the config
  (`[instances.<host>.<service>] name = "dir"`) or recorded by `web-ui up`
  in `~/.config/web-ui/instances/<svc>/<name>.json`.
  The name is a slug of the project directory's basename, with a short hash
  appended if another directory already has it.  `up` in the same directory
  reuses the instance.  A service you start yourself on the canonical socket
  path is routed to without registering it.

## Security

- **Host check.**  Requests whose `Host` is not `*.localhost` (or loopback)
  get 421.  This defeats DNS rebinding.
- **Origin check.**  Unsafe methods and WebSocket upgrades from another origin
  get 403, and so does `Sec-Fetch-Site: cross-site` without an `Origin`.
  Each instance is its own origin, so one service's page can't post to
  another.
- **Peer uid.**  The front socket checks `SO_PEERCRED`.  The loopback TCP
  port looks the client socket up in `/proc/net/tcp{,6}` and refuses other
  users' connections, which is what makes a shared seat safe.
- **One request per connection.**  The proxy sends `Connection: close` to
  backends (except upgrades), so every request passes the checks above.
- **TCP-only services** still listen on a loopback port that other local
  users can reach directly.  Prefer services that can listen on a socket.

## Logging

Every command takes `-l/--log-level` (`debug`, `info` (default), `warning`,
`error`, `critical`) and `-L/--log-sink`: `stdout`, `stderr` or a file name.
The default sink is `$XDG_STATE_HOME/web-ui/web-ui.log` (mode 0600), shared
by every web-ui process on the host, so each line carries its pid.  Give the
options before the command (`web-ui -l debug serve`), or set
`WEB_UI_LOG_LEVEL` and `WEB_UI_LOG_SINK`.  A proxy started by `serve
--detach`, and a tunnel the proxy starts, use the level and sink of the
process that started them.

- `info` is the proxy's access log (one line per request: listener, method,
  URL, status, bytes, time, backend, and why it was refused) plus the few
  events that matter: listening, starting and stopping instances and tunnels.
- `debug` traces the rest: config, commands run with their environment,
  endpoints, routing, peer uids, origin checks.
- Failures are `error`, with a traceback for unexpected ones; a command that
  dies on an unexpected exception logs it as `critical`.

`web-ui logs` shows the log file.  What instances, tunnels (ssh) and the
background proxy write to stdout and stderr goes to the journal, or with
`runner = "process"` to `~/.local/state/web-ui/logs/`: see `web-ui logs
SERVICE [INSTANCE]`, `--tunnel HOST` and `--proxy`.

## Config

A running proxy picks up edits to its config file on the next request (or
on `SIGHUP`; `systemctl --user reload web-ui-proxy` with the generated
unit).  It logs what changed.  A file that doesn't parse is logged as an
error and the previous config stays in use.  Changing `port` needs a
restart.  Running instances keep the command they were started with; stop
them (`web-ui down`) to have the next visit start the new one.

See `examples/config.toml`.  `web-ui config` shows the effective settings and
paths.  Command placeholders: `{socket} {port} {dir} {instance} {service}
{host} {url} {runtime}`.

A service that something else starts (systemd, by hand) gets a
`connect` address instead of a `command`: `connect = "unix:~/path/gui.sock"`
or `"127.0.0.1:8384"`.  It is a singleton, routed to while it is up, and
never started or stopped by web-ui.  syncthing is the example: see
`examples/config.toml` and the systemd drop-in
`examples/syncthing.service.d/gui-socket.conf` for running its GUI on a Unix
socket.

## Limits and next steps

- The `{url}` given to a service uses this host's name and the configured
  port.  If seats call a host something else, or use another port, set
  `hostname` in that host's config to match.
- `web-ui tunnel` asks the remote for its socket path, which holds your uid
  there.  With `ensure_remote = false` it assumes the same uid as here,
  unless `remote_runtime_dir` says otherwise.
- No idle shutdown yet: instances run until `web-ui down`.
- The log file is never rotated by web-ui.  It reopens the file if it is
  moved, so `logrotate` (with no `copytruncate`) works.
- With the systemd runner, units started over ssh live in your user manager,
  which may exit when your last session ends.  Run `loginctl enable-linger`
  on hosts where they should outlive your logins.
- `ensure_remote` starts the remote proxy with that host's own config, so it
  also opens its TCP port unless that host's config sets `port = 0`.
- `properties` sandboxing (`ProtectHome=tmpfs`, `BindReadOnlyPaths=`) only
  applies with the systemd runner, and needs unprivileged user namespaces.
  Remember to bind the service's own binary if it lives under `$HOME`.

## Develop

```sh
uv sync && uv run pytest
```
