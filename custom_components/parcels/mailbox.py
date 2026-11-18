"""Read the parcels mailbox through Home Assistant's own `imap` integration (its login, folder and aioimaplib client).

Fetches every message with a UID above the last one processed (UID SEARCH / UID FETCH BODY.PEEK[]: reading changes
nothing). `mark_seen` marks them as read once fully processed, so unread = still waiting for the AI.

The functions take the IMAP config entry's `data` (see `imap_entry`); the folder is that entry's folder setting.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from .const import CONF_IMAP_ENTRY
from .parser import find_links, html_to_text

_LOGGER = logging.getLogger(__name__)
TIMEOUT = 60
CONNECT_TIMEOUT = 15


class MailboxError(Exception):
    """Base class of the errors the config flow shows on its form."""


class CannotConnect(MailboxError):
    pass


@dataclass
class Mail:
    uid: int
    key: str              # Message-ID (or uid-N when missing)
    subject: str
    sender: str
    sent_at: datetime | None
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)


def parse(uid: int, raw: bytes) -> Mail:
    msg = message_from_bytes(bytes(raw), policy=policy.default)
    try:
        sent = parsedate_to_datetime(msg["date"]) if msg["date"] else None
    except (TypeError, ValueError):
        sent = None
    if sent and sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    plain = html = ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        try:
            if part.get_content_type() == "text/plain" and not plain:
                plain = part.get_content()
            elif part.get_content_type() == "text/html" and not html:
                html = part.get_content()
        except (LookupError, ValueError):
            continue
    text = plain if len(plain.strip()) > 200 or not html else html_to_text(html)
    return Mail(uid=uid, key=(msg["message-id"] or f"uid-{uid}").strip(), subject=str(msg["subject"] or ""),
                sender=str(msg["from"] or ""), sent_at=sent, text=text, links=find_links(html, plain))


class ImapUnavailable(MailboxError):
    """The Home Assistant IMAP entry is missing or not loaded."""


def _is_loaded(entry) -> bool:
    return getattr(entry.state, "value", entry.state) == "loaded"


def imap_entry(hass, data: Mapping[str, Any]):
    """The Home Assistant `imap` config entry that provides the mailbox, or None when it is gone.

    A Parcels entry made by the config flow names it (`imap_entry_id`); a legacy entry (empty data) uses the first loaded
    `imap` entry, else the first one."""
    if CONF_IMAP_ENTRY in data:
        entry = hass.config_entries.async_get_entry(data[CONF_IMAP_ENTRY])
        return entry if entry is not None and entry.domain == "imap" else None
    entries = hass.config_entries.async_entries("imap")
    return next((e for e in entries if _is_loaded(e)), entries[0] if entries else None)


async def _connect(imap_data: Mapping[str, Any]):
    """Connect, log in and select the folder the way Home Assistant's IMAP integration does."""
    try:
        from homeassistant.components.imap.coordinator import connect_to_server
    except ImportError as err:
        raise MailboxError("Parcels needs `connect_to_server` from Home Assistant's imap integration, which this "
                           "Home Assistant version does not provide; update Parcels or Home Assistant") from err
    return await connect_to_server(imap_data)


async def check_imap_entry(hass, entry_id: str) -> None:
    """Config flow validation. Raises ImapUnavailable (missing / not loaded) or CannotConnect."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != "imap" or not _is_loaded(entry):
        raise ImapUnavailable(entry_id)
    try:
        client = await asyncio.wait_for(_connect(entry.data), CONNECT_TIMEOUT)
    except MailboxError:
        raise
    except Exception as err:  # noqa: BLE001 - login, folder, network, timeout
        _LOGGER.debug("IMAP connection check failed", exc_info=True)
        raise CannotConnect(str(err) or type(err).__name__) from err
    await _logout(client)


async def _uids(client, first: int, since: date | None = None) -> list[int]:
    criteria = f"UID {first}:*" + (f" SINCE {since.strftime('%d-%b-%Y')}" if since else "")
    res = await asyncio.wait_for(client.uid_search(criteria, charset=None), TIMEOUT)
    if res.result != "OK" or not res.lines:
        return []
    return sorted(int(x) for x in res.lines[0].split() if x.isdigit() and int(x) >= first)


async def _fetch(client, uid: int) -> bytes | None:
    res = await asyncio.wait_for(client.uid("fetch", str(uid), "(BODY.PEEK[])"), TIMEOUT)
    if res.result != "OK":
        return None
    for line in res.lines:           # aioimaplib returns the message literal as a bytearray
        if isinstance(line, bytearray):
            return bytes(line)
    for line in res.lines:
        if isinstance(line, bytes) and len(line) > 200 and b"\n" in line:
            return bytes(line)
    return None


async def mark_seen(data: Mapping[str, Any], uids: list[int]) -> None:
    """Mark these messages as read."""
    client = await _connect(data)
    try:
        res = await asyncio.wait_for(client.uid("store", ",".join(str(u) for u in uids), "+FLAGS", "(\\Seen)"), TIMEOUT)
        if res.result != "OK":
            raise RuntimeError(f"STORE returned {res.result}")
    finally:
        await _logout(client)


async def fetch_new(data: Mapping[str, Any], last_uid: int, limit: int = 50, since: date | None = None) -> list[Mail]:
    """Messages with UID > last_uid (oldest first, at most `limit`; first run: only those since `since`)."""
    client = await _connect(data)
    try:
        out = []
        for uid in (await _uids(client, last_uid + 1, since))[:limit]:
            raw = await _fetch(client, uid)
            if raw:
                out.append(parse(uid, raw))
            else:
                out.append(Mail(uid, f"uid-{uid}", "", "", None, ""))   # unreadable: skip but advance
        return out
    finally:
        await _logout(client)


async def fetch_uids(data: Mapping[str, Any], uids: list[int]) -> dict[int, Mail | None]:
    """Fetch specific messages again (pending LLM processing). None = no longer in the mailbox."""
    client = await _connect(data)
    try:
        return {uid: (parse(uid, raw) if (raw := await _fetch(client, uid)) else None) for uid in uids}
    finally:
        await _logout(client)


async def _logout(client) -> None:
    try:
        await asyncio.wait_for(client.logout(), 10)
    except Exception:  # noqa: BLE001 - best effort
        _LOGGER.debug("IMAP logout failed", exc_info=True)
