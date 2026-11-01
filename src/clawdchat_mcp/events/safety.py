"""Callback URL SSRF guards for MCP Events.

Copied from lxyd-ai/clawdchat ``backend/app/core/webhook_safety.py`` (separate repo,
cannot import). Only the timeouts differ: ChatGPT MCP Events uses a 10s budget.

Original notes:

Used when an agent sets ``webhook_url`` and again on every outbound push.
HTTPS only; every resolved IP must be a public unicast address; the HTTP
client then connects to a pinned IP with the original Host / SNI so a later
DNS rebinding cannot steer the TCP connection onto a private address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

WEBHOOK_CONNECT_TIMEOUT_SECONDS = 3.0
WEBHOOK_TOTAL_TIMEOUT_SECONDS = 10.0  # ChatGPT MCP Events: 10s
WEBHOOK_DNS_TIMEOUT_SECONDS = 3.0
WEBHOOK_MAX_BODY_BYTES = 4096

_PUBLIC_MESSAGE = "callback url 未通过安全校验（仅允许公网 https，禁止私网/回环/元数据地址）"

# Explicit networks (in addition to ipaddress flags) so CGNAT / cloud
# metadata stay blocked even on Python versions that do not treat them
# as private.
_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

_BLOCKED_IPS: frozenset[ipaddress._BaseAddress] = frozenset(
    {
        ipaddress.ip_address("0.0.0.0"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("::1"),
        ipaddress.ip_address("::"),
    }
)


class UnsafeWebhookURL(ValueError):
    """Webhook URL failed the SSRF checks. ``str(exc)`` is safe for HTTP 400."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(_PUBLIC_MESSAGE)

    def __str__(self) -> str:
        return _PUBLIC_MESSAGE


@dataclass(frozen=True)
class ResolvedWebhook:
    """A webhook URL whose DNS answers have all been checked."""

    url: str
    hostname: str
    port: int
    ip: str
    ips: tuple[str, ...]


def _strip_zone(raw: str) -> str:
    return raw.split("%", 1)[0]


def parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(_strip_zone(raw))


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if this address must never be used as a webhook target."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return True

    if ip in _BLOCKED_IPS:
        return True
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    if ip.is_private:
        return True
    for network in _BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return True
    return False


def _idna_host(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeWebhookURL(f"hostname idna failed: {exc}") from exc


def parse_webhook_url(url: str) -> tuple[str, int, str]:
    """Return (hostname, port, url). Raises UnsafeWebhookURL on format errors."""
    if not url or not str(url).strip():
        raise UnsafeWebhookURL("empty url")
    raw = str(url).strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise UnsafeWebhookURL("scheme is not https")
    if parts.username is not None or parts.password is not None:
        raise UnsafeWebhookURL("userinfo is not allowed")
    hostname = parts.hostname
    if not hostname:
        raise UnsafeWebhookURL("missing hostname")
    hostname = _idna_host(hostname)
    try:
        parsed_port = parts.port
    except ValueError as exc:
        raise UnsafeWebhookURL("invalid port") from exc
    port = 443 if parsed_port is None else parsed_port
    if port <= 0 or port > 65535:
        raise UnsafeWebhookURL("invalid port")
    return hostname, port, raw


async def resolve_ips(hostname: str, port: int) -> list[str]:
    """DNS lookup used by the validator (monkeypatched in tests)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            ),
            timeout=WEBHOOK_DNS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise UnsafeWebhookURL("dns timeout") from exc
    except OSError as exc:
        raise UnsafeWebhookURL(f"dns failed: {exc}") from exc
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = _strip_zone(sockaddr[0])
        if ip_str not in seen:
            seen.add(ip_str)
            ips.append(ip_str)
    if not ips:
        raise UnsafeWebhookURL("dns returned no addresses")
    return ips


def _reject_if_blocked(ips: Iterable[str]) -> None:
    for raw in ips:
        try:
            addr = parse_ip(raw)
        except ValueError as exc:
            raise UnsafeWebhookURL(f"unparseable ip {raw!r}") from exc
        if is_blocked_ip(addr):
            raise UnsafeWebhookURL(f"blocked ip {addr}")


async def resolve_webhook_url(url: str) -> ResolvedWebhook:
    """Parse + resolve + reject unsafe addresses. Call on set and on send."""
    hostname, port, raw = parse_webhook_url(url)
    try:
        literal = parse_ip(hostname)
    except ValueError:
        ips = await resolve_ips(hostname, port)
    else:
        ips = [str(literal)]
    _reject_if_blocked(ips)
    return ResolvedWebhook(
        url=raw,
        hostname=hostname,
        port=port,
        ip=ips[0],
        ips=tuple(ips),
    )


def format_url_host(ip: str) -> str:
    addr = parse_ip(ip)
    return str(addr)


class PinnedIPAsyncTransport(httpx.AsyncBaseTransport):
    """Connect to a pre-validated IP while presenting original Host and SNI."""

    def __init__(
        self,
        *,
        hostname: str,
        ip: str,
        inner: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.hostname = hostname
        self.ip = ip
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Refuse to dial any host other than the IP we already checked.
        pinned_host = format_url_host(self.ip)
        url = request.url.copy_with(host=pinned_host)
        headers = httpx.Headers(request.headers)
        headers["host"] = self.hostname
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self.hostname
        pinned = httpx.Request(
            request.method,
            url,
            headers=headers,
            extensions=extensions,
            stream=request.stream,
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _read_limited_body(
    resp: httpx.Response, max_bytes: int = WEBHOOK_MAX_BODY_BYTES
) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        if not chunk:
            continue
        room = max_bytes - len(buf)
        if room <= 0:
            break
        buf.extend(chunk[:room])
        if len(buf) >= max_bytes:
            break
    return bytes(buf)
