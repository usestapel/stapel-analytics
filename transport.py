"""Outbound HTTPS POST for the built-in ``webhook`` fan-out adapter.

The collector URL of an analytics adapter comes from SETTINGS, not from a
row somebody created over the API — so this is a far less hostile dial than
stapel-webhooks' subscription targets. It still carries the IP guard, for
one reason: a settings typo (or a copied dev config) is precisely how a
background job ends up POSTing a deployment's whole event stream at
``169.254.169.254``, and a guard that only runs against attackers does not
run on the day it is needed.

* **https only** — no confession switch. An analytics mirror is not worth a
  plaintext egress path, and a host that needs one names its own adapter
  handler (that is what the registry is for).
* **DNS -> IP validation** through ``stapel_core.net.safe_fetch.
  ip_is_forbidden`` — the single fleet definition of "not a routable public
  address". Imported, never restated: the IP policy is the part that must
  never drift between modules.
* **anti-rebinding** — resolve, validate, then connect to that exact IP with
  the real hostname for SNI, certificate validation and ``Host``.
* **redirects are not followed** — a 3xx is an error, not an address.
* **capped response read** and a **total deadline**.

The POST shape around core's GET-only ``fetch_bytes`` is duplicated from
stapel-webhooks' ``transport.py``; both are waiting on the same follow-up,
a ``post_bytes`` in ``stapel_core.net`` (MODULE.md §10).
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from stapel_core.net.safe_fetch import ip_is_forbidden

#: A collector's response body is diagnostics, never data.
MAX_RESPONSE_BYTES = 2048


class TransportError(Exception):
    """A POST that never produced a usable 2xx."""


def _validated_ip(host: str, port: int):
    """Resolve *host*, refusing if ANY answer is a non-public address.

    "Any", not "the first": a name answering with a mix of public and
    private records is hostile, not "pick the good one".
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransportError(f"cannot resolve {host!r}: {exc}") from exc
    if not infos:
        raise TransportError(f"no addresses for {host!r}")
    first = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip_is_forbidden(ip):
            raise TransportError(f"{host!r} resolves to non-public address {ip}")
        if first is None:
            first = ip
    return first


def post_json(url: str, body: bytes, headers: dict, *, timeout: float = 10.0) -> int:
    """POST *body* to *url*; return the status code or raise TransportError."""
    started = time.monotonic()
    parsed = urlsplit(url)
    if (parsed.scheme or "").lower() != "https":
        raise TransportError(f"refusing non-https collector url {url!r}")
    host = parsed.hostname
    if not host:
        raise TransportError(f"collector url {url!r} has no host")
    port = parsed.port or 443
    ip = _validated_ip(host, port)

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:  # pragma: no cover - resolution consumed the budget
        raise TransportError("deadline exceeded before connect")

    conn = _connect(host, str(ip), port, timeout=remaining)
    try:
        conn.request("POST", path, body=body, headers={"Host": host, **headers})
        response = conn.getresponse()
        status = response.status
        payload = response.read(MAX_RESPONSE_BYTES) or b""
        if status in (301, 302, 303, 307, 308):
            raise TransportError(
                f"collector answered {status}; a redirect is not an address"
            )
        if status >= 400:
            raise TransportError(
                f"collector answered {status}: "
                f"{payload.decode('utf-8', 'replace')[:200]}"
            )
        return status
    except TransportError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def _connect(host: str, ip: str, port: int, *, timeout: float):
    """Open the TLS connection, pinning TCP to *ip* with SNI for *host*.

    Its own function so a test — or an egress-proxy adapter — substitutes
    the network without re-implementing the URL policy above.
    """
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    conn.sock = sock
    return conn


__all__ = ["MAX_RESPONSE_BYTES", "TransportError", "post_json"]
