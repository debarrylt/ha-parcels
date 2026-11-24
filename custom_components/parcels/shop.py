"""Shop order page check: look for a tracking number on the order page a Shopify shop links in its emails.

Some shops never email the number. Opening the emailed link is done sparingly (Shopify limits visits without a login) and
politely: plain requests, an honest User-Agent, no cookies kept, nothing sent. Only what the page shows as text or links
counts. Data the page hides (scripts, a login, a bot check) is never worked around: the outcome is then UNREADABLE and the
user is asked to look the number up.

The address comes from an email, so it is not trusted: https on the standard port only, every redirect is checked, and
names that resolve to a private address (the home network, Home Assistant itself) are refused.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .parser import fits_tracking_label, find_links, html_to_text

_LOGGER = logging.getLogger(__name__)

FOUND, NOT_YET, UNREADABLE, RETRY = "found", "not_yet", "unreadable", "retry"
USER_AGENT = "HomeAssistant-Parcels (order status check)"
TIMEOUT = 20
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 8
MIN_TEXT = 200            # a page with less text than this is an empty shell that a script fills in
LOGIN_PATH = re.compile(r"/(?:account/)?(?:login|log-in|signin|sign-in|challenge|password)(?:[/?]|$)", re.I)


@dataclass
class Result:
    outcome: str
    number: str | None = None
    reason: str = ""          # short, never contains the address


def allowed_url(url: str) -> bool:
    """https, default port, a host name (no IP address, no login in the address, not a local name)."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return False
    host = (u.hostname or "").rstrip(".").lower()
    if u.scheme != "https" or port not in (None, 443) or u.username or u.password or "." not in host:
        return False
    if host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".arpa")):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return bool(re.search(r"\.(?:[a-z]{2,}|xn--[a-z0-9-]+)$", host))     # not 127.1 or 0x7f.1 (short and hex addresses)
    return False


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    return ip.is_global and not ip.is_multicast


def read_page(status: int, path: str, body: str) -> Result:
    """What an order page says. `path` = the path of the final address."""
    if status in (408, 425, 429) or status >= 500:
        return Result(RETRY, reason=f"HTTP {status}")
    if status != 200:
        return Result(UNREADABLE, reason=f"HTTP {status}")
    if LOGIN_PATH.search(path):
        return Result(UNREADABLE, reason="login needed")
    visible = re.sub(r"(?is)<(script|style|noscript|template|head)\b.*?</\1\s*>", " ", body)
    visible = re.sub(r"(?s)<!--.*?-->", " ", visible)
    text = html_to_text(visible)
    number = fits_tracking_label(text, find_links(visible, None))
    if number:
        return Result(FOUND, number)
    if len(text.strip()) < MIN_TEXT:
        return Result(UNREADABLE, reason="the page needs a browser")
    return Result(NOT_YET, reason="no tracking number on the page")


async def check(url: str) -> Result:
    """Open the order link once. Never raises; never logs the address (it signs in to the order)."""
    try:
        import aiohttp
        return await asyncio.wait_for(_fetch(aiohttp, url), TIMEOUT)
    except Exception as err:  # noqa: BLE001 - DNS, TLS, timeout, reset: try again at the next check
        _LOGGER.debug("Parcels: order page check failed: %s", type(err).__name__)
        return Result(RETRY, reason=type(err).__name__)


async def _fetch(aiohttp, url: str) -> Result:
    from aiohttp.abc import AbstractResolver
    from aiohttp.resolver import ThreadedResolver

    class PublicResolver(AbstractResolver):
        """Checks the addresses that are then connected to, so a name cannot change between check and use."""

        def __init__(self) -> None:
            self._inner = ThreadedResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            found = await self._inner.resolve(host, port, family)
            if not found or not all(public_address(r["host"]) for r in found):
                raise OSError("not a public address")
            return found

        async def close(self) -> None:
            await self._inner.close()

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False),
                                     cookie_jar=aiohttp.CookieJar(), headers=headers, trust_env=False) as session:
        for _ in range(MAX_REDIRECTS + 1):
            if not allowed_url(url):
                return Result(UNREADABLE, reason="address not allowed")
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    url = urljoin(url, resp.headers["Location"])
                    continue
                if resp.status == 200 and "html" not in resp.headers.get("Content-Type", ""):
                    return Result(UNREADABLE, reason="not a web page")
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    raw += chunk
                    if len(raw) > MAX_BYTES:
                        return Result(UNREADABLE, reason="page too large")
                return read_page(resp.status, urlsplit(url).path, raw.decode("utf-8", "replace"))
        return Result(UNREADABLE, reason="too many redirects")
