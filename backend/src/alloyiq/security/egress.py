"""Outbound HTTP egress policy (D046, D052; build prompt section 10).

SafeHttpClient is the only way adapters reach the network:
- HTTPS only, host must be on the allowlist (exact match);
- every resolved address must be public (no private, loopback, link-local, CGNAT,
  multicast, reserved or cloud-metadata ranges);
- redirects are not followed automatically; each hop is re-validated (max 3);
- bounded response size and explicit timeouts.

Neither model output nor browser input can choose a destination: adapters build URLs from
fixed bases. The yfinance library uses its own HTTP stack (ADR-013), so its hosts are
documented but that path is not routed through this client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "www.alphavantage.co",
        "api.tavily.com",
        "api.anthropic.com",
        "api.smith.langchain.com",
        "eu.api.smith.langchain.com",
    }
)

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024

_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "64:ff9b::/96",
        "100::/64",
        "2001:db8::/32",
        "fd00:ec2::254/128",
    )
]


class EgressBlocked(Exception):
    """The destination violates the egress policy. Never retried."""


def is_public_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not any(ip in net for net in _BLOCKED_NETS if net.version == ip.version) and ip.is_global


Resolver = Any  # async (host, port) -> list[str]


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


class EgressPolicy:
    def __init__(
        self,
        allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
        resolver: Resolver = _system_resolve,
    ) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self._resolve = resolver

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise EgressBlocked(f"scheme {parts.scheme!r} not allowed (HTTPS only)")
        host = (parts.hostname or "").lower()
        if not host or host not in self.allowed_hosts:
            raise EgressBlocked(f"host {host!r} is not on the egress allowlist")
        if parts.username or parts.password:
            raise EgressBlocked("credentials in URL are not allowed")
        try:
            ipaddress.ip_address(host)
            raise EgressBlocked("IP-literal hosts are not allowed")
        except ValueError:
            pass
        try:
            addrs = await self._resolve(host, parts.port or 443)
        except OSError as exc:
            raise EgressBlocked(f"cannot resolve {host}") from exc
        if not addrs or not all(is_public_address(a) for a in addrs):
            raise EgressBlocked(f"{host} resolves to a non-public address")


class SafeHttpClient:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        timeout_s: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "AlloyIQ/0.1 (+research; contact via operator)"},
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> httpx.Response:
        for _ in range(MAX_REDIRECTS + 1):
            await self.policy.check(url)
            resp = await self._client.request(
                method, url, params=params, json=json, headers=dict(headers or {})
            )
            if resp.is_redirect and "location" in resp.headers:
                url = urljoin(url, resp.headers["location"])
                params = None  # the Location already carries the query
                continue
            if len(resp.content) > max_bytes:
                raise EgressBlocked(f"response larger than {max_bytes} bytes")
            return resp
        raise EgressBlocked("too many redirects")

    async def aclose(self) -> None:
        await self._client.aclose()
