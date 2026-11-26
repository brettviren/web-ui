# web-ui design

web-ui puts the web UIs you run on several hosts (maggie, `kb web`,
agentsview, ...) behind one name scheme. You can reach each of them from
whichever seat (workstation) you're at. It does this without a TCP port per
service, and without one tunnel per service, host and seat.

## Names

```
http://[INSTANCE.]SERVICE.HOST.localhost:8484/
       xero.maggie.wcgpu0.localhost:8484      maggie on ~/dune/xero/.beads, on wcgpu0
       kb.wckb.haiku.localhost:8484           kb web in ~/kb, on haiku
       agentsview.wcgpu0.localhost:8484       a singleton: instance "default" is omitted
       localhost:8484, wcgpu0.localhost:8484  landing pages: what runs where
```

- **Service** is a kind of UI, defined once in the config by a command
  template.
- **Instance** is one running copy of a service, usually for one directory.
- **Host** is the machine it runs on.

Browsers resolve every `*.localhost` name to loopback themselves, so no DNS
or `/etc/hosts` setup is needed. Each name is also its own browser origin, so
the UIs can't read each other's cookies or storage.

## Pieces

```
 seat (haiku)                                           host (wcgpu0)
 ┌──────────────────────────────────────┐               ┌───────────────────────────────────┐
 │ browser                              │               │                                   │
 │   │ tcp 127.0.0.1:8484               │               │                                   │
 │   ▼                                  │      ssh      │                                   │
 │ web-ui proxy ── hosts/wcgpu0.sock ═══╪═══════════════╪══▶ front.sock ── web-ui proxy     │
 │   │                                  │  one per host │                    ├─ maggie/xero │
 │   └─ wckb/kb.sock ── kb web (~/kb)   │               │                    ├─ wckb/xero   │
 │                                      │               │                    └─ agentsview  │
 └──────────────────────────────────────┘               └───────────────────────────────────┘
        all sockets live in $XDG_RUNTIME_DIR/web-ui/ (mode 0700)
```

**Instances.** Each instance listens on a Unix socket,
`$XDG_RUNTIME_DIR/web-ui/<service>/<instance>.sock`. Only your user can
reach it, and nothing can collide with it. Some services can only listen on
TCP (maggie, agentsview). Each of those gets a fresh ephemeral loopback port
on every start, recorded in `<instance>.port`. Nobody has to know or choose
that port.

**Instances are defined** in two places:

- the config, as `[instances.<host>.<service>] name = "dir"`. One file can
  then be shared by every host, and each host picks out its own;
- `web-ui up SERVICE [DIR]`, which records them in
  `~/.config/web-ui/instances/`.

**The proxy** (`web-ui serve`) runs one per machine. It listens in two
places:

- `front.sock`, a Unix socket. Everything that arrives here is for this
  host, and the host label is ignored. This is what other machines reach
  through ssh.
- `127.0.0.1:8484` (and `[::1]`), only on a machine where you sit at a
  browser. Here the proxy routes on the host label: `*.<this host>` goes to
  local instances and `*.<other>` goes to `hosts/<other>.sock`.

For each connection, the proxy:

1. reloads its config if the file changed since the last request (one
   `stat`; a file that doesn't parse is logged and the previous config
   kept);
2. reads the request headers;
3. checks them (see Security);
4. connects to the backend;
5. copies bytes both ways until either side closes.

Because it copies raw bytes, server-sent events (maggie, agentsview) and
WebSockets pass through unchanged. Each request gets its own backend
connection, because the proxy sends `Connection: close`. That way every
request goes through the checks.

**Processes** are started on demand. The first request for a known instance
that isn't running starts it, and so does the first request for a singleton
service. The proxy waits until the instance accepts connections, then
forwards the request. Processes run as:

- transient systemd user units (`systemd-run --user`), with logs in the
  journal and optional sandboxing `properties`; or
- plain detached children (`runner = "process"`), with logs in
  `~/.local/state/web-ui/logs/`.

A service can also be started by something else (syncthing, under
systemd). Its config gives a fixed `connect` address instead of a
command: a Unix socket path, or a loopback port. web-ui routes to it while
it is up, and never starts or stops it.

web-ui's own log goes to one sink per process (`-L`: a file, `stdout` or
`stderr`), by default `~/.local/state/web-ui/web-ui.log`, shared by the CLI,
the proxy and tunnels. At the default `info` level it holds the proxy's
access log and the starts and stops of instances and tunnels. `-l debug`
adds what is needed to trace a problem. Processes that web-ui starts in the
background inherit the level and sink.

## The network and ssh

The proxy has exactly one network listener: the loopback port on a seat,
which only that machine can reach. Nothing listens on a real network
interface. Instances listen on Unix sockets, or on ephemeral loopback ports
for TCP-only services.

All traffic between machines goes over ssh. ssh already does the
authentication (keys, agent, or Kerberos/GSSAPI under FreeIPA) and the
encryption, so web-ui has none of its own. A seat needs one ssh connection
per remote host, whatever the number of services or instances there. That
turns the N_services × N_hosts × N_seats problem into N_hosts tunnels per
seat.

`web-ui tunnel HOST` sets one up in two steps:

1. `ssh HOST web-ui serve --detach --print-socket` starts the host's proxy
   if it isn't running, and prints the path of its `front.sock`. The path
   includes your uid on that host, which can differ from your uid here (for
   example, a different login name).
2. `ssh -N -L $XDG_RUNTIME_DIR/web-ui/hosts/HOST.sock:<that path> HOST`
   forwards a local Unix socket to that path, for as long as ssh runs.

The tunnel uses its own ssh connection, not a ControlMaster shared with your
interactive sessions, so it ends when its ssh ends.

When the seat's proxy gets a request for a host in `[hosts.*]` and no tunnel
is up, it starts `web-ui tunnel --batch HOST` itself. `--batch` turns on ssh
`BatchMode`, so this only works if ssh needs no prompt (an agent or Kerberos
ticket). You can also run the tunnel with plain ssh; see
`examples/ssh_config`.

The tunnel can outlive the host's proxy: if that proxy stops, ssh still
accepts on the local socket, then drops each connection without a byte. When
a request gets no response at all from a host, the seat's proxy revives it
and sends the request again, if it has no body. To revive the host, it first
runs `web-ui serve --detach` there, then, if that isn't enough, restarts the
tunnel. If the request has a body, or the revival fails, the seat's proxy
returns a 502 that says what happened. The landing page and `web-ui hosts`
check that each host's proxy answers (`GET /ping`), not just that the tunnel
is up.

No connection ever goes from a host back to a seat. Even status is pulled:
the landing page and `web-ui ls -a` read `http://HOST.localhost/status.json`
through the same tunnel.

## Security

Your browser is the main exposure, not other users. Any web page you visit
can send requests to localhost ports, and DNS rebinding can make those
requests look same-origin. The proxy handles this with three checks:

- **Host.** A request whose `Host` is not `*.localhost` or a loopback
  address gets 421. This defeats DNS rebinding, because a rebinding page's
  requests carry the attacker's hostname.
- **Origin.** A POST, PUT or similar, or a WebSocket upgrade, from any other
  origin gets 403. So does a request carrying `Sec-Fetch-Site: cross-site`
  and no `Origin` header. Each instance is its own origin, so even another
  web-ui service's page can't make changes through it.
- **Peer uid.** On `front.sock` the kernel reports the peer (`SO_PEERCRED`).
  On the loopback port the proxy looks the client's socket up in
  `/proc/net/tcp{,6}`, whose owner column gives its uid. Anyone else's
  connection gets 403. This is what makes a seat shared with other users
  safe.

Remaining exposure: another local user can connect straight to a TCP-only
service's ephemeral port, bypassing the proxy. Prefer Unix sockets when a
service supports them.

## Files

| path | what |
|------|------|
| `~/.config/web-ui/config.toml` | services, hosts, declared instances |
| `~/.config/web-ui/instances/<svc>/<name>.json` | instances added by `web-ui up` |
| `$XDG_RUNTIME_DIR/web-ui/front.sock` | this host's proxy |
| `$XDG_RUNTIME_DIR/web-ui/<svc>/<name>.sock` or `.port` | an instance's endpoint |
| `$XDG_RUNTIME_DIR/web-ui/hosts/<host>.sock` | tunnel to another host's `front.sock` |
| `~/.local/state/web-ui/web-ui.log` | web-ui's own log (default `-L` sink) |
| `~/.local/state/web-ui/logs/` | output of background processes, with `runner = "process"` |

## Running under systemd

`web-ui install` makes the proxy a socket-activated user service:
`web-ui-proxy.socket` listens on `front.sock` (mode 0600, in a 0700
directory) and on the loopback port, and passes them to
`web-ui-proxy.service` (`LISTEN_FDS`).  Because systemd owns the listening
sockets, a restart (an upgrade, a changed unit) queues new connections
instead of refusing them; the stopping proxy stops accepting, lets open
connections finish for up to 5 s, then exits.  The service is `Type=notify`
(READY=1 once it serves) and reloads its config on SIGHUP (`systemctl --user
reload web-ui-proxy`).  Instances started by the systemd runner are
separate transient units and keep running across proxy restarts; with the
process runner they are the proxy's children, so the unit gets
`KillMode=process`.  A changed `port` needs `web-ui install` again, to
rewrite the socket unit.

## Limits

- Nothing stops an idle instance: it runs until `web-ui down`.
- A service started with a `{url}` placeholder (agentsview's
  `--public-url`) is told the URL under its own host's name and the
  configured port. That URL is only right if seats use that name and port.
- Over ssh, systemd user units belong to your user manager, which may stop
  when your last login ends. Run `loginctl enable-linger` on hosts where
  instances should keep running.
