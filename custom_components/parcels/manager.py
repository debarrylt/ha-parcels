"""ParcelsManager: mailbox scanning, AI processing with retry, 17TRACK sync, follow-ups, actions.

All state lives in one JSON store (HA .storage/parcels.store). Work is serialised by one lock; a 5-minute timer
(and, in legacy mode, the IMAP integration's `imap_content` event) triggers scans. Email bodies are never stored, only
references (UID + Message-ID) so pending emails can be fetched again when the AI is back.

Two modes. Own credentials (entries made by the config flow): the entry names the Home Assistant `imap`
entry that provides the mailbox; the 17TRACK login and AI settings are in the entry. Legacy (entry with empty data): the loaded `imap`, `ollama` and `seventeentrack`
integrations' config entries are borrowed. The switch is `uses_own_credentials(entry.data)`.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import llm, mailbox, model
from .const import (AI_NONE, AI_OLLAMA, AI_TASK, CONF_AI_ENTITY, CONF_AI_MODE, CONF_OLLAMA_KEY, CONF_OLLAMA_MODEL,
                    CONF_OLLAMA_URL, CONF_SHIP24_KEY, CONF_T17_PASSWORD, CONF_T17_USERNAME, DEFAULT_OLLAMA_MODEL,
                    uses_own_credentials, LAST_MILE_EVERY, SHIP24_URL, AI_CHECK_INTERVAL, CARRIER_FIX_AFTER, EMAIL_RECORD_DAYS, EVENT, FIRST_RUN_DAYS, HOUSEKEEPING_INTERVAL,
                    SCAN_INTERVAL, SIGNAL_UPDATE, SLOTS, STORE_KEY, STORE_VERSION, TRACK_INTERVAL)
from .parser import (Extraction, extract, is_home, normalise_tracking, rules,
                     pick_links)

_LOGGER = logging.getLogger(__name__)
MAX_AI_FAILS = 3        # unusable AI answers for one email before its rule-parser result is kept as final


class ParcelsManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.data: dict[str, Any] = model.new_store()
        self.own = uses_own_credentials(entry.data)     # True: own credentials; False: legacy (borrowed entries)
        self.ai_online = False
        self._t17_client = None                          # own mode: logged-in pyseventeentrack client
        self._t17_login_failed = False
        self.last_scan: datetime | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._scan_later = None
        self.last_mile: str | None = None      # outcome of the last 17TRACK detail lookup (sensor attribute)

    # ---------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        self.data = await self._store.async_load() or model.new_store()
        e = self.entry
        e.async_on_unload(async_track_time_interval(self.hass, self._tick_scan, SCAN_INTERVAL))
        e.async_on_unload(async_track_time_interval(self.hass, self._tick_ai, AI_CHECK_INTERVAL))
        e.async_on_unload(async_track_time_interval(self.hass, self._tick_track, TRACK_INTERVAL))
        e.async_on_unload(async_track_time_interval(self.hass, self._tick_housekeeping, HOUSEKEEPING_INTERVAL))
        if not self.own:    # legacy: HA's imap integration wakes us up; otherwise the 5-minute scan is the trigger
            e.async_on_unload(self.hass.bus.async_listen("imap_content", self._on_imap))
        e.async_on_unload(async_at_started(self.hass, self._on_started))
        e.async_on_unload(self._cancel_scan_later)

    @callback
    def _cancel_scan_later(self) -> None:
        if self._scan_later:
            self._scan_later()
            self._scan_later = None

    async def async_flush(self) -> None:
        await self._store.async_save(self.data)

    async def _on_started(self, _hass: HomeAssistant) -> None:
        await self._tick_ai(None)
        self._background(self._first_run(), "parcels first scan")

    async def _first_run(self) -> None:
        """At start: mail, then 17TRACK straight away (not 15 minutes later)."""
        await self.async_scan()
        await self.async_track()

    def _background(self, coro, name: str) -> None:
        self.entry.async_create_background_task(self.hass, coro, name)

    @callback
    def _on_imap(self, event: Event) -> None:
        if self._imap_entry() and event.data.get("entry_id") == self._imap_entry().entry_id:
            self._cancel_scan_later()
            self._scan_later = async_call_later(self.hass, 5, self._on_imap_later)

    async def _on_imap_later(self, _now) -> None:
        self._scan_later = None
        await self.async_scan()

    async def _tick_scan(self, _now) -> None:
        await self.async_scan()

    async def _tick_ai(self, _now) -> None:
        was = self.ai_online
        self.ai_online = await self._ai_check()
        if self.ai_online != was:
            self._changed(save=False)
            if self.ai_online and self._pending():
                self._background(self.async_reprocess(), "parcels reprocess")

    async def _tick_track(self, _now) -> None:
        await self.async_track()

    async def _tick_housekeeping(self, _now) -> None:
        async with self._lock:
            self._housekeeping()
        if self.ai_online and self._pending():
            await self.async_reprocess()
        self._changed()

    # ---------------------------------------------------------------- sources
    def _loaded(self, domain: str) -> ConfigEntry | None:
        for e in self.hass.config_entries.async_entries(domain):
            if e.state is ConfigEntryState.LOADED:
                return e
        return None

    def _imap_entry(self) -> ConfigEntry | None:
        """The loaded Home Assistant `imap` entry that provides the mailbox (the one we name, or the first, legacy)."""
        e = mailbox.imap_entry(self.hass, self.entry.data)
        return e if e is not None and e.state is ConfigEntryState.LOADED else None

    def _ollama(self) -> tuple[str | None, str | None, str]:
        """Ollama URL, API key and model: from our settings (own mode) or the loaded ollama entry (legacy)."""
        if self.own:
            o = {**self.entry.data, **self.entry.options}
            return (o.get(CONF_OLLAMA_URL) or None, o.get(CONF_OLLAMA_KEY) or None,
                    o.get(CONF_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL)
        e = self._loaded("ollama")
        return (e.data.get("url"), e.data.get("api_key"), DEFAULT_OLLAMA_MODEL) if e else (None, None, DEFAULT_OLLAMA_MODEL)

    @property
    def ai_mode(self) -> str:
        """none | ai_task | ollama. Legacy entries always use Ollama."""
        return (self.entry.options.get(CONF_AI_MODE) or AI_NONE) if self.own else AI_OLLAMA

    @property
    def ai_enabled(self) -> bool:
        """False = rules only: emails are final after the rule parser and never wait for an AI."""
        return self.ai_mode != AI_NONE

    async def _ai_check(self) -> bool:
        """Is the configured AI reachable right now?"""
        mode = self.ai_mode
        if mode == AI_TASK:
            st = self.hass.states.get(self.entry.options.get(CONF_AI_ENTITY) or "")
            return st is not None and st.state != "unavailable"
        if mode == AI_OLLAMA:
            url, key, mdl = self._ollama()
            return bool(url) and await llm.online(async_get_clientsession(self.hass), url, key, mdl)
        return False

    async def _ai_extract(self, ex: Extraction, text: str) -> Extraction:
        if self.ai_mode == AI_TASK:
            return await llm.extract_ai_task(self.hass, self.entry.options.get(CONF_AI_ENTITY), ex, text)
        url, key, mdl = self._ollama()
        return await llm.extract_ollama(async_get_clientsession(self.hass), url, key, ex, text, mdl)

    async def _t17(self):
        """The 17TRACK profile (a pyseventeentrack Profile), or None when not configured / not reachable."""
        if not self.own:
            e = self._loaded("seventeentrack")
            return e.runtime_data.client.profile if e and getattr(e, "runtime_data", None) else None
        user, pw = self.entry.data.get(CONF_T17_USERNAME), self.entry.data.get(CONF_T17_PASSWORD)
        if not user or not pw:
            return None                               # no 17TRACK account: parcels still come from emails
        if self._t17_client is None:
            from pyseventeentrack import Client
            from pyseventeentrack.errors import SeventeenTrackError
            client = Client(session=async_create_clientsession(self.hass))
            try:
                if not await client.profile.login(user, pw):
                    raise SeventeenTrackError("login refused")
            except Exception as err:  # noqa: BLE001
                if not self._t17_login_failed:        # log once, not every 15 minutes
                    _LOGGER.warning("Parcels: 17TRACK login failed (%s); check the account in the Parcels settings", err)
                self._t17_login_failed = True
                return None
            self._t17_login_failed = False
            self._t17_client = client
        return self._t17_client.profile

    def _t17_reset(self) -> None:
        """Drop the cached client so the next sync logs in again (session expired, auth error)."""
        self._t17_client = None

    # ---------------------------------------------------------------- email
    async def async_scan(self) -> None:
        entry = self._imap_entry()
        if not entry:
            self.last_error = "IMAP integration not loaded"
            _LOGGER.warning("Parcels: the Home Assistant IMAP entry is missing or not loaded; skipping this scan")
            return
        async with self._lock:
            last = int(self.data.get("last_uid", 0))
            try:
                mails = await mailbox.fetch_new(entry.data, last,
                                                since=(dt_util.now() - FIRST_RUN_DAYS).date() if last == 0 else None)
            except Exception as err:  # noqa: BLE001 - network / server errors: retry on the next tick
                self.last_error = f"Mailbox: {err}"
                _LOGGER.warning("Parcels: mailbox scan failed: %r", err)
                return
            for mail in mails:
                try:
                    await self._process(mail)
                except Exception:  # noqa: BLE001 - one bad email must not stall the mailbox
                    _LOGGER.exception("Parcels: could not process email uid %s (%s)", mail.uid, mail.subject[:60])
                    self.data["emails"][mail.key] = {"uid": mail.uid, "date": model._iso(dt_util.now()),
                                                     "stage": "error", "parcel": None, "pending": False}
                self.data["last_uid"] = max(int(self.data.get("last_uid", 0)), mail.uid)
            self.last_scan, self.last_error = dt_util.now(), None
            self._housekeeping()
            await self._backfill_links(entry)
            await self._backfill_ship_to(entry)
            await self._backfill_notes(entry)
            await self._recheck_delivered(entry)
            await self._recheck_rule_statuses(entry)
            await self._undo_user_deliveries(entry)
            await self._backfill_lines(entry)
            await self._mark_read(entry)
        self._changed()
        if mails:
            await self.async_track()

    async def _backfill_links(self, entry: ConfigEntry) -> None:
        """Look once through the emails of parcels recorded before their links were kept. A failure retries next scan."""
        todo = [p for p in self.data["parcels"].values() if model.active(p) and not p.get("links_checked")]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails for their links: %r", err)
            return
        for p in todo:
            for e in p["emails"]:
                mail = mails.get(e.get("uid"))
                if mail:
                    order, track = pick_links(mail.links, p["tracking"], p.get("carrier"))
                    p["order_url"] = p.get("order_url") or order
                    p["carrier_url"] = p.get("carrier_url") or track
            p["links_checked"] = True

    def _set_ship_to(self, p: dict, ship_to: str | None) -> None:
        """Keep the address the latest email names; `elsewhere` = it is not home (not counted as
        arriving today). Home = the postcode / names in the settings; without them nothing is marked elsewhere."""
        if not ship_to:
            return
        names = [n for n in (self.entry.options.get("home_names") or "").split(",") if n.strip()]
        p["ship_to"] = ship_to[:120]
        p["elsewhere"] = is_home(ship_to, self.entry.options.get("home_postcode"), names) is False

    async def _backfill_ship_to(self, entry: ConfigEntry) -> None:
        """Look once through the emails of active parcels recorded before addresses were read (Nov 2026)."""
        todo = [p for p in self.data["parcels"].values() if model.active(p) and not p.get("ship_to_checked")]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails for their address: %r", err)
            return
        for p in todo:
            for e in sorted(p["emails"], key=lambda e: e.get("date") or ""):
                mail = mails.get(e.get("uid"))
                if mail:
                    self._set_ship_to(p, extract(mail.subject, mail.sender, mail.text, mail.sent_at, mail.links).ship_to)
            p["ship_to_checked"] = True

    async def _backfill_notes(self, entry: ConfigEntry) -> None:
        """Once per active parcel recorded before emails had a note ("Delayed · new date Fri 20 Nov"): read it with the
        rules. A failure retries next scan."""
        todo = [p for p in self.data["parcels"].values() if model.active(p) and not p.get("notes_checked")]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid") and "note" not in e})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails for their notes: %r", err)
            return
        for p in todo:
            for e in p["emails"]:
                mail = mails.get(e.get("uid"))
                if mail and "note" not in e:
                    e["note"] = model.email_note(extract(mail.subject, mail.sender, mail.text, mail.sent_at, mail.links))
            p["notes_checked"] = True

    async def _backfill_lines(self, entry: ConfigEntry, limit: int = 3) -> None:
        """Once per parcel recorded before order lines were kept: re-read its emails with the AI for the lines only.
        A few parcels per scan (the AI is slow); an AI failure retries on a later scan."""
        if not self.ai_online:
            return
        todo = [p for p in self.data["parcels"].values() if not p.get("lines_checked") and not p.get("merged_into")]
        if not todo:
            return
        todo = todo[:limit]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails for their order lines: %r", err)
            return
        for p in todo:
            lines = []
            for e in p["emails"]:
                mail = mails.get(e.get("uid"))
                if mail is None or mail.key != e.get("key"):
                    continue
                ex = extract(mail.subject, mail.sender, mail.text, mail.sent_at or dt_util.now(), mail.links)
                try:
                    lines = model.merge_lines(lines, (await self._ai_extract(ex, mail.text)).lines)
                except Exception as err:  # noqa: BLE001 - AI offline: try again later
                    _LOGGER.info("Parcels: order lines for %s not read yet (%s)", model.title(p), err)
                    return
            p["lines"] = model.merge_lines(p.get("lines"), lines)
            p["lines_checked"] = True
            _LOGGER.info("Parcels: %s has %d order line(s)", model.title(p), len(p["lines"]))

    async def _recheck_delivered(self, entry: ConfigEntry) -> None:
        """Once per parcel: re-read emails behind a "delivered" from before Amazon's progress bar was understood."""
        for p in self.data["parcels"].values():    # the first version of this check wrongly undid a real delivery
            if model.active(p) and p.get("delivered_rechecked"):
                model.restore_carrier_delivery(p)
        todo = [p for p in self.data["parcels"].values() if model.active(p) and p.get("status") == "delivered"
                and p.get("status_source") == "email"
                and not (p.get("delivered_rechecked") and p.get("delivered_v4_checked"))]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails to check a delivery: %r", err)
            return
        for p in todo:
            found = [mails.get(e.get("uid")) for e in p["emails"]]
            # Only the emails with the bar can have misled the rules. An email that's gone can't be replayed. Without the
            # bar the delivery may have come from the AI, which the rules alone can't confirm: leave those parcels alone.
            # Same when the email that set "delivered" said "is due to be delivered today" (read as delivered until
            # 2026-11-11). Only that email: a later real delivery email keeps the parcel delivered.
            misread = [m for m in found if m and model._dt(p.get("status_at")) == m.sent_at
                       and rules().to_be_delivered.search(f"{m.subject}\n{m.text}")]
            if found and all(found) and (any(rules().progress_bar.search(m.text) for m in found) or misread):
                exs = [extract(m.subject, m.sender, m.text, m.sent_at, m.links) for m in found]
                if model.rebuild_status(p, exs):
                    _LOGGER.info("Parcels: %s was not delivered after all", model.title(p))
            p["delivered_rechecked"] = p["delivered_v4_checked"] = True

    async def _recheck_rule_statuses(self, entry: ConfigEntry) -> None:
        """Once per parcel: replay the rules after they stopped reading "will notify you once it's on the way" as shipped.

        Only parcels whose status came from emails, and only when the rules find a status in every one of them: the AI
        only fills a status the rules leave empty, so then the stored status is the rules' own and can be redone.
        """
        todo = [p for p in self.data["parcels"].values() if model.active(p) and p.get("status_source") == "email"
                and p.get("status") != "delivered" and not (p.get("t17") or {}).get("status")
                and not p.get("rules_v3_checked")]
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails to check a status: %r", err)
            return
        for p in todo:
            found = [mails.get(e.get("uid")) for e in p["emails"]]
            if found and all(found):
                exs = [extract(m.subject, m.sender, m.text, m.sent_at, m.links) for m in found]
                old = p.get("status")
                if all(ex.status for ex in exs) and model.rebuild_status(p, exs) and p.get("status") != old:
                    _LOGGER.info("Parcels: %s is %s, not %s", model.title(p), p.get("status"), old)
            p["rules_v3_checked"] = True

    async def _undo_user_deliveries(self, entry: ConfigEntry | None, todo: list[dict] | None = None) -> None:
        """Marking a parcel received also marks it delivered (status_source "user"); when "received" is undone (an
        accidental tap on a dashboard, Nov 2026: a parcel just ordered stayed "Delivered, confirm?" and its "Dispatched"
        email could not move it back), the status comes from its emails again. Parcels whose emails are gone keep it."""
        if todo is None:
            todo = [p for p in self.data["parcels"].values() if model.active(p) and p.get("status") == "delivered"
                    and p.get("status_source") == "user" and p.get("user_delivery_checked") != p.get("status_at")]
        if not todo or entry is None:
            return
        uids = sorted({e["uid"] for p in todo for e in p["emails"] if e.get("uid")})
        try:
            mails = await mailbox.fetch_uids(entry.data, uids) if uids else {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Parcels: could not re-read emails to undo a delivery: %r", err)
            return
        for p in todo:
            found = [mails.get(e.get("uid")) for e in p["emails"]]
            if found and all(found):
                exs = [extract(m.subject, m.sender, m.text, m.sent_at, m.links) for m in found]
                if model.rebuild_status(p, exs):
                    model.restore_carrier_delivery(p)
                    _LOGGER.info("Parcels: %s is %s again (received was undone)", model.title(p), p.get("status"))
            p["user_delivery_checked"] = p.get("status_at")

    def _pending(self) -> list[str]:
        return [k for k, e in self.data["emails"].items() if e.get("pending")]

    async def async_reprocess(self) -> None:
        entry = self._imap_entry()
        if not entry or not self.ai_online:
            return
        async with self._lock:
            keys = sorted(self._pending(), key=lambda k: self.data["emails"][k]["uid"])
            if not keys:
                return
            try:
                mails = await mailbox.fetch_uids(entry.data, [self.data["emails"][k]["uid"] for k in keys])
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Parcels: re-fetch failed: %s", err)
                return
            for k in keys:
                rec = self.data["emails"][k]
                mail = mails.get(rec["uid"])
                if mail is None or mail.key != k:     # deleted from the mailbox: keep the regex result
                    rec["pending"] = False
                    continue
                try:
                    await self._process(mail, force_llm=True)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Parcels: reprocessing uid %s failed", rec["uid"])
                    rec["pending"] = False
                if not self.ai_online:
                    break
            self._refresh_pending_flags()
            await self._mark_read(entry)
        self._changed()

    async def _mark_read(self, entry: ConfigEntry) -> None:
        """Mark fully processed emails as read; those still waiting for the AI stay unread. Failures retry next scan."""
        recs = [e for e in self.data["emails"].values() if not e.get("pending") and not e.get("seen")][:200]
        if not recs:
            return
        try:
            await mailbox.mark_seen(entry.data, [e["uid"] for e in recs])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Parcels: could not mark emails as read: %s", err)
            return
        for e in recs:
            e["seen"] = True

    async def _process(self, mail: mailbox.Mail, force_llm: bool = False) -> None:
        rec = self.data["emails"].get(mail.key)
        if rec and rec.get("stage") == "llm" and not force_llm:
            return
        if rec and rec.get("stage") == "regex" and not force_llm and not rec.get("pending"):
            return
        now = dt_util.now()
        ex = extract(mail.subject, mail.sender, mail.text, mail.sent_at or now, mail.links)
        hinted = ex.is_shipping or bool(rules().shipping_hint.search(ex.subject))
        rec = rec or {"uid": mail.uid, "date": model._iso(ex.sent_at or now), "stage": None, "parcel": None}
        self.data["emails"][mail.key] = rec
        if not hinted:
            rec.update(stage="ignored", pending=False)
            return
        stage = "regex"
        if self.ai_online:
            try:
                ex = await self._ai_extract(ex, mail.text)
                stage = "llm"
            except Exception as err:  # noqa: BLE001 - AI offline, model loading, bad JSON: retry later
                if isinstance(err, ValueError):      # the AI answered, but not with usable JSON: give up after a few tries
                    rec["ai_fails"] = rec.get("ai_fails", 0) + 1
                _LOGGER.info("Parcels: AI extraction failed (%s); will retry", err)
                self.ai_online = False
                self._changed(save=False)
        parcel, events = model.apply_extraction(
            self.data, ex, {"key": mail.key, "uid": mail.uid, "date": rec["date"], "subject": ex.subject[:120]},
            now, stage)
        waiting = self.ai_enabled and stage != "llm" and rec.get("ai_fails", 0) < MAX_AI_FAILS
        rec.update(stage=stage if parcel or stage == "llm" else "ignored", pending=waiting,
                   parcel=parcel["id"] if parcel else rec.get("parcel"))
        if parcel:
            parcel["details_pending"] = waiting
            self._set_ship_to(parcel, ex.ship_to)
            parcel["ship_to_checked"] = True
        self._fire(events)

    def _refresh_pending_flags(self) -> None:
        pending = {e.get("parcel") for e in self.data["emails"].values() if e.get("pending")}
        for p in self.data["parcels"].values():
            p["details_pending"] = p["id"] in pending

    # ---------------------------------------------------------------- 17TRACK
    async def async_track(self) -> None:
        profile = await self._t17()
        if not profile:
            return
        now = dt_util.now()
        try:
            for p in [p for p in list(self.data["parcels"].values()) if model.active(p) and p["tracking"]]:
                if p.get("carrier") == "amazon" or p["tracking"][0].startswith("TBA"):
                    continue
                for number in p["tracking"]:       # every number: a merged parcel has the export + last-mile ones
                    if number not in model.t17_registered(p):
                        await self._t17_add(profile, p, number)
            packages = await profile.packages(show_archived=False)
        except Exception as err:  # noqa: BLE001 - cloud API hiccups: next tick
            _LOGGER.warning("Parcels: 17TRACK sync failed: %s", err)
            self._t17_reset()                  # own mode: log in again next time (expired session)
            return
        rows = await self._last_mile_rows(now)
        by_number = {}
        for pk in packages:
            at = pk.timestamp if isinstance(pk.timestamp, datetime) else (
                dt_util.parse_datetime(str(pk.timestamp)) if pk.timestamp else None)
            if at and at.tzinfo is None:
                at = at.replace(tzinfo=dt_util.UTC)
            by_number[normalise_tracking(pk.tracking_number)] = SimpleNamespace(   # the library's packages are frozen
                status=pk.status, info_text=pk.info_text, location=pk.location, timestamp=at)
        async with self._lock:
            events = []
            for p in list(self.data["parcels"].values()):
                if not model.active(p):
                    continue
                for number in p["tracking"]:       # already in the account: re-adding it only returns an error code
                    if number in by_number and number not in model.t17_registered(p):
                        self._t17_registered_now(p, number)
                picked = model.pick_t17(p, by_number)     # the most recently updated of the parcel's numbers
                if not picked:
                    continue
                number, pk = picked
                if p["t17"].get("number") != number:
                    _LOGGER.info("Parcels: %s follows 17TRACK number %s", p["id"], number)
                    p["t17"]["number"] = number
                    p["t17"].pop("at", None)             # take the new number's event even if it is not newer
                events += model.apply_t17(p, str(pk.status), pk.info_text, pk.timestamp, pk.location, now)
                if str(pk.status).lower() == "not found" and not p["t17"].get("carrier_set") \
                        and now - model._dt(p["t17"].get("added") or p["created"]) > CARRIER_FIX_AFTER:
                    await self._t17_set_carrier(profile, p, number)
            for src, dst in model.last_mile_links(self.data, rows):
                _LOGGER.info("Parcels: Ship24 links %s to %s: merged",
                             self.data["parcels"][src]["tracking"][0], self.data["parcels"][dst]["tracking"][0])
                model.merge(self.data, src, dst, now, "last_mile")
            self._fire(events)
        self._changed()

    async def _last_mile_rows(self, now: datetime) -> list[dict]:
        """Ship24's record of each candidate parcel, raw: {"number": number, "detail": json text}.

        When an export carrier hands a parcel to a local one (4PX -> Evri, Nov 2026), Ship24 lists both numbers on the
        shipment ("Handled by"; API: shipment.trackingNumbers). 17TRACK knows it too but only shows it on its website
        (signed requests) or its paid API. The record is searched whole for other parcels' numbers by
        model.last_mile_links(). Only when a carrier email named no shop (model.export_candidates). Needs a Ship24
        API key in the Parcels settings (free plan: 10 parcels a month; a parcel looked up again is the same tracker). Each candidate at most every LAST_MILE_EVERY after a lookup that
        answered; the outcome is in sensor.parcels_in_flight (attribute last_mile)."""
        cands = [p for p in model.export_candidates(self.data, now)    # only while a shop-less carrier parcel waits
                 if not p.get("last_mile_at") or now - model._dt(p["last_mile_at"]) > LAST_MILE_EVERY]
        if not cands:
            return []
        key = (self.entry.data.get(CONF_SHIP24_KEY) or self.entry.options.get(CONF_SHIP24_KEY) or "").strip()
        if not key:
            self.last_mile = "no Ship24 API key (Parcels settings)"
            return []
        session = async_get_clientsession(self.hass)
        rows, errors = [], []
        for p in cands:
            number = p["tracking"][0]
            try:
                async with session.post(f"{SHIP24_URL}/trackers/track", json={"trackingNumber": number}, timeout=30,
                                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as r:
                    body = await r.json(content_type=None)
                    if r.status >= 400:
                        raise HomeAssistantError(f"HTTP {r.status} {json.dumps((body or {}).get('errors'))[:100]}")
            except Exception as err:  # noqa: BLE001 - cloud API: retried at the next sync (15 min)
                errors.append(f"{number}: {str(err)[:100]}")
                continue
            rows.append({"number": number, "detail": json.dumps(body)})
            p["last_mile_at"] = model._iso(now)       # only a lookup that answered counts
        self.last_mile = f"Ship24: {len(rows)}/{len(cands)} read {now:%d %b %H:%M}" + (f"; {errors[0]}" if errors else "")
        if errors:
            _LOGGER.warning("Parcels: Ship24 lookup failed: %s", "; ".join(errors))
        return rows

    def _t17_registered_now(self, p: dict, number: str) -> None:
        p["t17"]["numbers"] = sorted(model.t17_registered(p) | {number})
        p["t17"].update(registered=True, added=p["t17"].get("added") or model._iso(dt_util.now()))

    async def _t17_add(self, profile, p: dict, number: str) -> None:
        code = self._carrier_code(p, number)
        kwargs = {"first_carrier": code} if code and "first_carrier" in inspect.signature(profile.add_package).parameters else {}
        try:
            await profile.add_package(number, model.title(p)[:40], **kwargs)
        except Exception as err:  # noqa: BLE001
            if "exist" not in str(err).lower():     # network / quota: retry on the next sync (max 10 tries)
                p["t17"]["add_failures"] = p["t17"].get("add_failures", 0) + 1
                if p["t17"]["add_failures"] < 10:
                    _LOGGER.info("Parcels: 17TRACK add %s failed: %s", number, err)
                    return
            kwargs = {}                              # already in the account: carrier fixed later if needed
        self._t17_registered_now(p, number)
        if number == model.main_number(p):
            p["t17"]["carrier_set"] = bool(kwargs)

    async def _t17_set_carrier(self, profile, p: dict, number: str) -> None:
        code = self._carrier_code(p, number)
        p["t17"]["carrier_set"] = True
        if not code or not hasattr(profile, "set_carrier_by_tracking_number"):
            return
        try:
            await profile.set_carrier_by_tracking_number(number, first_carrier=code)
            _LOGGER.info("Parcels: 17TRACK carrier for %s set to %s", number, code)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Parcels: 17TRACK set carrier %s failed: %s", number, err)

    @staticmethod
    def _carrier_code(p: dict, number: str) -> int | None:
        """The parcel carrier's 17TRACK code, only for a number in that carrier's format: a merged parcel's export
        number (4PX...) is not the last-mile carrier's (Evri), and 17TRACK errors when told it is."""
        if p.get("carrier") not in rules().carriers or not model.fits_carrier(p["carrier"], number):
            return None
        return rules().carriers[p["carrier"]][1]

    async def _t17_archive(self, p: dict) -> None:
        profile = await self._t17()
        if not profile or not model.t17_registered(p):
            return
        for number in p["tracking"]:
            try:
                await profile.archive_package(number)
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("Parcels: 17TRACK archive %s: %s", number, err)

    # ---------------------------------------------------------------- housekeeping
    def _housekeeping(self) -> None:
        now = dt_util.now()
        events = []
        for p in self.data["parcels"].values():
            if model.check_follow_up(p, now.date(), now):
                events.append(("follow_up", p))
        model.backfill_merges(self.data)
        for src, dst in model.merge_order_twins(self.data, now):
            _LOGGER.info("Parcels: %s joined %s (same order)", src, dst)
        model.purge(self.data, now)
        cutoff = now - timedelta(days=EMAIL_RECORD_DAYS)
        for k in [k for k, e in self.data["emails"].items() if (model._dt(e.get("date")) or now) < cutoff]:
            del self.data["emails"][k]
        self._fire(events)

    # ---------------------------------------------------------------- views
    def rows(self) -> list[dict]:
        today = dt_util.now().date()
        return [model.view(p, today) for p in sorted((p for p in self.data["parcels"].values() if model.active(p)),
                                                    key=lambda p: model.sort_key(p, today))]

    def received_recent(self) -> list[dict]:
        today = dt_util.now().date()
        return [model.view(p, today) for p in self.data["parcels"].values()
                if p.get("received_at") and not p.get("merged_into")]

    def summary(self) -> dict[str, Any]:
        today = dt_util.now().date().isoformat()
        act = [p for p in self.data["parcels"].values() if model.active(p)]
        home = [p for p in act if not p.get("elsewhere")]     # parcels going to another address don't count here
        return {
            # still coming to the door today (dashboards): out for delivery (even without a date) or
            # due today; not delivered, received or waiting at a pickup point
            "arriving_today": sum(1 for p in home if not p.get("received_at") and p.get("status") not in ("delivered", "pickup")
                                  and (p.get("status") == "out_for_delivery" or p.get("eta_date") == today)),
            # carrier said so, or with the carrier and due today (no "out for delivery" email on the day)
            "out_for_delivery": sum(1 for p in home if model.out_today(p, dt_util.now().date())),
            "delivered_to_confirm": sum(1 for p in act if p.get("status") == "delivered"),
            "follow_up": sum(1 for p in act if p.get("flagged")),
            "details_pending": sum(1 for p in act if p.get("details_pending")),
            "more": max(0, len(act) - SLOTS),
            # for the welcome-back page: what was delivered when (14 days)
            "delivered_log": [{"title": model.title(p), "at": p["delivered_at"]} for p in self.data["parcels"].values()
                              if p.get("delivered_at") and not p.get("merged_into")],
            "last_scan": model._iso(self.last_scan), "last_error": self.last_error, "last_mile": self.last_mile,
        }

    def delivered(self, since) -> list[dict]:
        return model.delivered_since(self.data, since)

    # ---------------------------------------------------------------- actions
    def resolve(self, parcel_id: str | None = None, slot: int | None = None) -> dict:
        if parcel_id and parcel_id in self.data["parcels"]:
            return self.data["parcels"][parcel_id]
        if slot:
            rows = self.rows()
            if 1 <= slot <= len(rows):
                return self.data["parcels"][rows[slot - 1]["parcel_id"]]
        raise HomeAssistantError(f"No parcel {parcel_id or f'in slot {slot}'}")

    async def async_received(self, p: dict, received: bool = True) -> None:
        now = dt_util.now()
        p.update(received_at=model._iso(now) if received else None, flagged=False if received else p["flagged"],
                 updated=model._iso(now))
        if received and p.get("status") != "delivered":
            model.set_status(p, "delivered", now, "user")
        self._fire([("received" if received else "unreceived", p)])
        if not received and p.get("status_source") == "user":
            await self._undo_user_deliveries(self._imap_entry(), [p])
        self._changed()
        if received:
            await self._t17_archive(p)

    def follow_up(self, p: dict, flag: bool | None = None, reason: str | None = None) -> None:
        flag = not p.get("flagged") if flag is None else flag
        now = dt_util.now()
        p.update(flagged=flag, flag_reason=((reason or "").strip()[:60] or p.get("flag_reason") or "Follow up") if flag else None,
                 flag_cleared_at=None if flag else model._iso(now), updated=model._iso(now))
        self._changed()

    def merge(self, source: dict, target: dict) -> None:
        model.merge(self.data, source["id"], target["id"], dt_util.now())
        self._changed()

    async def async_remove(self, p: dict) -> None:
        del self.data["parcels"][p["id"]]
        self._changed()
        await self._t17_archive(p)

    def update_fields(self, p: dict, title: str | None = None, due: date | None = None,
                      tracking: str | None = None) -> None:
        if title is not None:
            p["title"] = title.strip() or None
        if due is not None:
            p.update(eta_date=due.isoformat(), eta_window=None, eta_at=model._iso(dt_util.now()))
        if tracking:
            t = normalise_tracking(tracking)
            if t not in p["tracking"]:
                p["tracking"].append(t)                # added to 17TRACK at the next sync (t17_registered)
        p["updated"] = model._iso(dt_util.now())
        self._changed()

    def add_manual(self, title: str, due: date | None, tracking: str | None) -> dict:
        now = dt_util.now()
        p = model.new_parcel(now)
        p.update(title=title.strip() or "Parcel", manual=True)
        self.data["parcels"][p["id"]] = p
        self.update_fields(p, due=due, tracking=tracking)
        return p

    # ---------------------------------------------------------------- plumbing
    def _fire(self, events: list[tuple[str, dict]]) -> None:
        today = dt_util.now().date()
        for kind, p in events:
            v = model.view(p, today)
            self.hass.bus.async_fire(EVENT, {"kind": kind, "parcel_id": p["id"], "title": v["title"], "line": v["line"],
                                             "status": v["status"], "flag_reason": p.get("flag_reason"), "url": v["url"],
                                             "elsewhere": v["elsewhere"]})

    @callback
    def _changed(self, save: bool = True) -> None:
        if save:
            self._store.async_delay_save(lambda: self.data, 2)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
