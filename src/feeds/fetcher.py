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

# Large publisher feeds (The Daily, Ezra Klein) can be 7-18 MB because
# Simplecast includes years of back-catalog metadata. Keep this below a
# genuinely dangerous response size, but high enough for real podcast feeds.
DEFAULT_MAX_BYTES = 25_000_000
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5


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
    max_redirects: int = MAX_REDIRECTS,
) -> bytes:
    """Fetch a public RSS feed, returning raw bytes for parsing.

    Redirects are followed manually: each Location target is validated
    (scheme + DNS) before the next connection. ``httpx`` auto-redirect
    would let the redirected request happen before we got a chance to
    inspect the new host. Raises :class:`FeedFetchError` on any
    validation or transport failure.
    """
    current = url
    for _ in range(max_redirects + 1):
        _, host = _validate_public_http_url(current)
        await asyncio.get_running_loop().run_in_executor(
            None, _validate_hostname, host
        )
        next_url = await _fetch_one(
            current, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )
        if isinstance(next_url, bytes):
            return next_url
        current = next_url
    raise FeedFetchError(f"too many redirects (> {max_redirects})")


async def _fetch_one(
    url: str, *, max_bytes: int, timeout_seconds: float
) -> bytes | str:
    """Fetch one hop. Returns body bytes on 2xx or the redirect target URL."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": "Podwash/1.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise FeedFetchError(
                            f"redirect without Location header at {url}"
                        )
                    return str(response.url.join(location))
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FeedFetchError("feed too large")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"fetch failed: {exc}") from exc


def fetch_public_feed_sync(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
) -> bytes:
    """Sync wrapper used by the APScheduler poll job (which runs in a
    background thread, not the FastAPI event loop)."""
    return asyncio.run(
        fetch_public_feed(
            url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        )
    )
