# web-ui

One front door per host for local web UIs (agentsview, maggie, wckb, ...),
reached from any seat through one SSH tunnel per host.  See `ideas.md` for
the reasoning.

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
uv tool install /path/to/web-ui      # or: uv tool install --editable .
cp examples/config.toml ~/.config/web-ui/config.toml   # and edit
```

Install it on every host and seat, with the same config if you like (each
host picks its own name from its hostname).

## Use

```sh
web-ui serve --detach          # the front door on this machine (or: web-ui gen systemd)
cd ~/proj/foo && web-ui up maggie
#  → http://foo.maggie.hostA.localhost:8484/
web-ui ls -a                   # instances here and on tunneled hosts
web-ui down maggie --forget    # stop it (DIR or --name, like up)
web-ui tunnel hostA            # forward hostA's front door (runs ssh in the foreground)
web-ui logs maggie foo -f
```

At a seat, open `http://localhost:8484/` for a landing page that lists the
instances and hosts.  Usually you don't need to start anything by hand:

- **Instances start on first visit.**  A registered instance (from `web-ui up
  --no-start`, or one that has since stopped) and any `singleton` service are
  started when their URL is requested.
- **Tunnels open on first visit** for hosts in `[hosts.*]` with
  `autotunnel = true` (needs non-interactive ssh auth: an agent or Kerberos).
  The tunnel first runs `web-ui serve --detach` on the host.

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
- **Processes** run as transient systemd user units (`systemd-run --user`,
  logs in the journal, sandboxing via `properties`) or, with
  `runner = "process"`, as detached children logging to
  `~/.local/state/web-ui/logs/`.
- **Instances** are recorded in `~/.config/web-ui/instances/<svc>/<name>.json`.
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

## Config

See `examples/config.toml`.  `web-ui config` shows the effective settings and
paths.  Command placeholders: `{socket} {port} {dir} {instance} {service}
{host} {url} {runtime}`.

## Limits and next steps

- The `{url}` given to a service uses this host's name and the configured
  port.  If seats call a host something else, or use another port, set
  `hostname` in that host's config to match.
- No idle shutdown yet: instances run until `web-ui down`.
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
