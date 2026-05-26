"""SSRF-safe RSS feed fetcher.

User-submitted URLs are arbitrary. We restrict them to public http(s),
resolve DNS up front so a hostname can't point at a private/loopback IP,
enforce a small body cap, and use a tight timeout. The fetched bytes
are returned for ``feedparser.parse(bytes)`` to consume — never let
feedparser dereference user URLs directly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_TIMEOUT_SECONDS = 10.0


class FeedFetchError(Exception):
    """Raised on any fetch validation or transport failure."""


def _validate_public_http_url(url: str) -> tuple[str, str]:
    if not url:
        raise FeedFetchError("empty URL")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise FeedFetchError("only http/https URLs are allowed")
    host = parts.hostname
    if not host:
        raise FeedFetchError("URL has no hostname")
    return parts.scheme.lower(), host


def _is_private_or_disallowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or addr == "169.254.169.254"
    )


def _validate_hostname(host: str) -> None:
    # Reject literal local hostnames before DNS resolution; some
    # resolvers happily map "localhost" to 127.0.0.1.
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise FeedFetchError("URL points at localhost")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FeedFetchError(f"DNS lookup failed: {exc}") from exc
    if not infos:
        raise FeedFetchError("hostname did not resolve")
    for info in infos:
        addr = info[4][0]
        if _is_private_or_disallowed(addr):
            raise FeedFetchError(
                f"hostname {host} resolves to disallowed address {addr}"
            )


async def fetch_public_feed(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch a public RSS feed, returning raw bytes for parsing.

    Raises :class:`FeedFetchError` on any validation or transport
    failure so callers can return a single error to the operator.
    """
    _, host = _validate_public_http_url(url)
    await asyncio.get_running_loop().run_in_executor(None, _validate_hostname, host)

    chunks: list[bytes] = []
    size = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": "Podwash/1.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                # If a redirect bounced us to a private host, refuse.
                final_host = response.url.host
                if final_host and final_host != host:
                    await asyncio.get_running_loop().run_in_executor(
                        None, _validate_hostname, final_host
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FeedFetchError("feed too large")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"fetch failed: {exc}") from exc
    return b"".join(chunks)
