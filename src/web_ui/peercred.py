"""Who is on the other end of a local connection?

For Unix sockets the kernel tells us (``SO_PEERCRED``).  For loopback TCP we
find the client's socket in ``/proc/net/tcp{,6}``, whose ``uid`` column is
the owner of that socket: this is how the seat-side TCP listener refuses
other local users even though any user can connect to a loopback port.
"""

from __future__ import annotations

import ipaddress
import socket
import struct


def unix_peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _decode(hexaddr: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    """Decode a /proc/net/tcp address like ``0100007F:1F90``."""
    host, port = hexaddr.split(":")
    raw = bytes.fromhex(host)
    # The kernel prints each 32-bit word in host (little-endian) order.
    words = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    addr = ipaddress.ip_address(words)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr, int(port, 16)


def _norm(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    a = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a


def tcp_peer_uid(sock: socket.socket, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> int | None:
    """The uid owning the client end of an accepted loopback TCP connection."""
    try:
        ours = sock.getsockname()[:2]
        theirs = sock.getpeername()[:2]
    except OSError:
        return None
    # The client's socket has local=their address and remote=ours.
    want = ((_norm(theirs[0]), theirs[1]), (_norm(ours[0]), ours[1]))
    port_hex = f":{theirs[1]:04X}"
    for table in tables:
        try:
            with open(table) as f:
                next(f)
                for line in f:
                    fields = line.split()
                    if len(fields) < 8 or not fields[1].endswith(port_hex):
                        continue
                    if (_decode(fields[1]), _decode(fields[2])) == want:
                        return int(fields[7])
        except OSError:
            continue
    return None


def peer_uid(sock: socket.socket) -> int | None:
    if sock.family == socket.AF_UNIX:
        return unix_peer_uid(sock)
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        return tcp_peer_uid(sock)
    return None
