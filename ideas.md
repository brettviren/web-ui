# The question

I have two or three HTTP services that I use to provide a "local" web UI.  This includes agents-view and maggie (beads web UI) as well as my own things like wckb and messnet.  I sit at either a work or a home Linux workstation and I work either on the localhost or on a remote host accessed via SSH.  I want to be able to run these services across my various hosts and access them across my various "seats".  So far, I have services listen on localhost and then form an SSH tunnel.  This is relatively secure as I mostly trust the users on localhost.  However, they may accidentally access one of the listening ports, and those ports tend to be ones that others use.  I also face chaos of the N_services x N_hosts x N_seats outer product.  These services all need to access some subset of the local file system and some are context dependent.  For example, maggie serves info in a given `.beads/` dir and wckb likewise a given `.kb/` dir.  So I sometimes will want one or more instances of a given service.  What are some strategies to increase the security, reduce taking up ports and in some way tame the combinatorics?

# The answer

The main move is to stop thinking in ports. Give each service instance a Unix socket, put one small reverse proxy in front on each machine, and route by hostname. That fixes the port collisions, the accidental access, and most of the combinatorics.

## The architecture

**1. Unix domain sockets instead of TCP ports.** A socket in `$XDG_RUNTIME_DIR/webui/<service>/<instance>.sock` with mode 0600 is accessible only to you. Nobody can stumble onto it, and there's nothing to collide with. Most frameworks support it (`uvicorn --uds`, Go's `net.Listen("unix", …)`, Node, gunicorn). For services that don't, `socat` or systemd's `systemd-socket-proxyd` can wrap a TCP-only one.

**2. One front door per machine.** Run a reverse proxy (Caddy is the easiest) on each host, listening on one Unix socket. It routes to backend sockets by `Host` header, so N_services × N_instances becomes one thing to forward per host. Use host-based routing rather than path prefixes, because many web UIs assume they live at `/` and break under `/maggie/`. A scheme like

```
<instance>.<service>.<host>.localhost
```

works well because browsers resolve `*.localhost` to loopback with no DNS or `/etc/hosts` edits. Each name is also a separate browser origin, so cookies and localStorage are isolated between services.

**3. One forwarded socket per host, not per service.** OpenSSH can forward Unix sockets, so each seat needs only one forward per remote host. It also runs its own proxy with one TCP listener on 127.0.0.1, since browsers can't talk to a socket directly. That proxy sends `*.hostA.localhost` to the socket forwarded from hostA and handles local names itself. The same URL scheme then works from every seat, and the host-side proxy ignores the `<host>` label.

The tunnels go from N_services × N_hosts × N_seats to N_hosts per seat, with instances and services free. Since FreeIPA gives you the same UID everywhere, `/run/user/<uid>` is the same path on every host, and one `Host *` stanza covers everything:

```
Host *
  StreamLocalBindUnlink yes
  StreamLocalBindMask 0177
  ExitOnForwardFailure yes
Host hostA hostB
  LocalForward /run/user/%i/webui/hosts/%h.sock /run/user/%i/webui/front.sock
```

Check ssh_config(5) for token support on your OpenSSH version. Use `ControlMaster`/`ControlPersist`, or a per-host systemd user unit, to keep the tunnels up.

## Taming instances (maggie per `.beads/`, wckb per `.kb/`)

Use systemd user template units with socket activation. Instances start on first request, and you can add idle timeouts so they exit when unused. Watch the ~108-byte limit on socket paths. Don't use the escaped project path as the instance name. Use a short slug or hash, and have a small helper record the mapping:

```
# ~/.config/webui/maggie/<slug>.env   →  DIR=/path/to/project
# maggie@.socket
[Socket]
ListenStream=%t/webui/maggie/%i.sock
SocketMode=0600
# maggie@.service
[Service]
EnvironmentFile=%E/webui/maggie/%i.env
ExecStart=/path/to/maggie --root ${DIR}
```

A helper like `webui-up maggie [dir]` can write the env file, start the socket unit, and print the URL. It's also worth having the proxy serve a landing page that globs the socket directory, so you can see what's live on each host. For your own services (wckb, messnet), you could instead make the root directory request-scoped, so one daemon serves many `.kb/` dirs and you get no per-instance sockets at all.

The Caddy side, as an untested sketch:

```
# host side: listen on the front socket, route by labels
http://*.localhost {
  bind unix//run/user/1000/webui/front.sock
  reverse_proxy unix/{env.XDG_RUNTIME_DIR}/webui/{labels.2}/{labels.3}.sock
}
# seat side: one stanza per remote host
http://*.hostA.localhost:8484 {
  bind 127.0.0.1
  reverse_proxy unix//run/user/1000/webui/hosts/hostA.sock
}
```

Here `{labels.N}` counts from the right, so in `projx.maggie.hostA.localhost`, labels.2 is the service and labels.3 is the instance.

## Security

Trusting the other users on localhost isn't the main exposure. Your browser is: any web page you visit can make requests to `localhost:PORT`, and DNS rebinding can get past the same-origin policy. For a UI that reads project files or has write endpoints, that's the realistic attack. The proxy fixes much of it by rejecting any `Host` not matching your scheme, which kills rebinding. For state-changing endpoints, also check `Origin` and don't send wildcard CORS headers.

Sockets also let you tighten the sandbox:

- **Socket permissions** give per-user authorization for free. Services can also check `SO_PEERCRED` for the peer UID.
- **`PrivateNetwork=yes`** is possible in the unit once the service only needs its socket. Socket-activated fds are still passed in, so the service has no network at all.
- **Filesystem scope** can be narrowed with `ProtectHome=tmpfs` plus `BindReadOnlyPaths=${DIR}`, so maggie sees only its one `.beads/` project. These options need unprivileged user namespaces, which may be restricted on some of your hosts, so test them.

## Incremental path

You don't have to do it all at once. Moving each service to a socket and adding the host-side proxy is the biggest win, because it removes the port chaos and the browser exposure. Socket-activated templates come next, and the seat-side proxy last. If you ever want direct network access instead of SSH, FreeIPA's CA and Kerberos (GSSAPI auth at the proxy) would give you real SSO. For tunnels, SSH already provides the authentication.

I can write a complete Caddyfile pair and the `webui-up` helper if you'd like. If so, what do agentsview and maggie support for listening (UDS, or only host:port)?
