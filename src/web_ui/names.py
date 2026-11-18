"""Hostname scheme: ``[<instance>.]<service>.<host>.<domain>[:port]``.

Each name is a DNS label so browsers treat it as a separate origin and
resolve ``*.localhost`` to loopback without any DNS setup.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

DEFAULT_INSTANCE = "default"

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class NameError_(ValueError):
    pass


def check_label(name: str) -> str:
    if not _LABEL.match(name):
        raise NameError_(f"{name!r} is not a valid name: use lowercase letters, digits and '-' (max 63)")
    return name


def slugify(text: str, maxlen: int = 40) -> str:
    """Turn arbitrary text (a directory name) into a DNS label."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:maxlen].strip("-")
    return s or "x"


def short_hash(text: str, n: int = 6) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


@dataclass(frozen=True)
class Target:
    """What a request's Host header names.

    ``service`` is None for a host's landing page; ``host`` is None when the
    name has no host label (bare ``localhost``).
    """

    host: str | None
    service: str | None = None
    instance: str = DEFAULT_INSTANCE


def split_host_header(value: str) -> tuple[str, int | None]:
    """Split a Host header into a lowercase name and an optional port."""
    value = value.strip().lower()
    if value.startswith("["):  # IPv6 literal
        end = value.find("]")
        if end < 0:
            raise NameError_(f"bad Host header {value!r}")
        name, rest = value[: end + 1], value[end + 1 :]
        port = rest[1:] if rest.startswith(":") else None
    else:
        name, _, port = value.partition(":")
        port = port or None
    if port is not None:
        if not port.isdigit():
            raise NameError_(f"bad port in Host header {value!r}")
        return name.rstrip("."), int(port)
    return name.rstrip("."), None


LOOPBACK_NAMES = {"127.0.0.1", "[::1]"}


def parse_host(value: str, domain: str = "localhost") -> Target:
    """Parse a Host header into a Target, or raise NameError_.

    Rejecting every name outside our scheme is what defeats DNS rebinding:
    an attacker's page can reach the proxy, but its requests carry the
    attacker's hostname.
    """
    name, _ = split_host_header(value)
    if name == domain or name in LOOPBACK_NAMES:
        return Target(host=None)
    suffix = "." + domain
    if not name.endswith(suffix):
        raise NameError_(f"{name!r} is not under .{domain}")
    labels = name[: -len(suffix)].split(".")
    for label in labels:
        check_label(label)
    if len(labels) == 1:
        return Target(host=labels[0])
    if len(labels) == 2:
        return Target(host=labels[1], service=labels[0])
    if len(labels) == 3:
        return Target(host=labels[2], service=labels[1], instance=labels[0])
    raise NameError_(f"{name!r} has too many labels; expected [instance.]service.host.{domain}")


def hostname_for(host: str, service: str | None = None, instance: str = DEFAULT_INSTANCE, domain: str = "localhost") -> str:
    parts = []
    if service:
        if instance != DEFAULT_INSTANCE:
            parts.append(instance)
        parts.append(service)
    parts += [host, domain]
    return ".".join(parts)


def url_for(host: str, service: str | None = None, instance: str = DEFAULT_INSTANCE, domain: str = "localhost", port: int | None = None) -> str:
    name = hostname_for(host, service, instance, domain)
    return f"http://{name}{f':{port}' if port and port != 80 else ''}/"
