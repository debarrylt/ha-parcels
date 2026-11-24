"""Parcels: parcels in flight from shipping emails (through Home Assistant's IMAP integration, optional AI) reconciled with 17TRACK.

Entries made by the config flow name the Home Assistant IMAP entry to read and hold their own 17TRACK / AI settings. legacy entries (empty data)
borrow the imap, ollama and seventeentrack integrations' config entries instead; see manager.py.
"""
from __future__ import annotations

try:
    import probatio as vol  # HA 2026.11+ (voluptuous replacement)
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import mailbox
from .const import DOMAIN, uses_own_credentials
from .manager import ParcelsManager
from .parser import extract, find_links, rules, set_rules
from .rules import RulesError, load_rules

_LOGGER = logging.getLogger(__name__)
RULES_FILE = "parcels_rules.yaml"      # the user's additions to rules.yaml, in the Home Assistant config folder
RULES_ISSUE = "rules_invalid"

PLATFORMS = [Platform.TODO, Platform.SENSOR, Platform.BUTTON, Platform.BINARY_SENSOR, Platform.TEXT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
TARGET = {vol.Exclusive("parcel_id", "target"): cv.string, vol.Exclusive("slot", "target"): vol.All(int, vol.Range(1, 99))}


def _manager(hass: HomeAssistant) -> ParcelsManager:
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not entries:
        raise HomeAssistantError("Parcels is not loaded")
    return entries[0].runtime_data


async def _load_rules(hass: HomeAssistant, keep_previous: bool) -> None:
    """Load rules.yaml plus <config>/parcels_rules.yaml. A wrong file raises a repair issue and leaves the built-in rules
    (setup) or the rules that were active (reload) in force."""
    try:
        new = await hass.async_add_executor_job(load_rules, hass.config.path(RULES_FILE))
    except RulesError as err:
        _LOGGER.error("Parcels: rules not loaded: %s", err)
        ir.async_create_issue(hass, DOMAIN, RULES_ISSUE, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                              translation_key=RULES_ISSUE, translation_placeholders={"error": str(err)})
        if not keep_previous:
            set_rules(await hass.async_add_executor_job(load_rules, None))
        return
    ir.async_delete_issue(hass, DOMAIN, RULES_ISSUE)
    set_rules(new)


def _extraction_dict(ex) -> dict:
    r = rules()
    return {"is_shipping": ex.is_shipping, "status": ex.status, "problem": ex.problem, "carrier": ex.carrier,
            "carrier_name": r.carriers[ex.carrier][0] if ex.carrier in r.carriers else None,
            "tracking": ex.tracking, "merchant": ex.merchant, "order": ex.order,
            "eta_date": ex.eta_date.isoformat() if ex.eta_date else None, "eta_window": ex.eta_window,
            "ship_to": ex.ship_to, "order_url": ex.order_url, "carrier_url": ex.carrier_url,
            "sender": ex.sender, "sender_domain": ex.sender_domain, "subject": ex.subject,
            "sent_at": ex.sent_at.isoformat() if ex.sent_at else None}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _load_rules(hass, keep_previous=False)

    async def reload_rules(call: ServiceCall) -> None:
        await _load_rules(hass, keep_previous=True)

    async def test_email(call: ServiceCall) -> ServiceResponse:
        """Run the rule parser on a made-up email so rule changes can be tried (Developer tools > Actions)."""
        body = call.data["body"]
        sent = call.data.get("date") or dt_util.now()
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=dt_util.get_default_time_zone())
        is_html = "<html" in body[:2000].lower() or "</div>" in body.lower() or "</a>" in body.lower()
        links = find_links(body if is_html else None, None if is_html else body)
        return _extraction_dict(extract(call.data["subject"], call.data["sender"], body, sent, links))

    async def received(call: ServiceCall) -> None:
        m = _manager(hass)
        await m.async_received(m.resolve(call.data.get("parcel_id"), call.data.get("slot")), call.data["received"])

    async def follow_up(call: ServiceCall) -> None:
        m = _manager(hass)
        m.follow_up(m.resolve(call.data.get("parcel_id"), call.data.get("slot")), call.data.get("flag"), call.data.get("reason"))

    async def merge(call: ServiceCall) -> None:
        m = _manager(hass)
        m.merge(m.resolve(call.data["parcel_id"]), m.resolve(call.data["into"]))

    async def remove(call: ServiceCall) -> None:
        m = _manager(hass)
        await m.async_remove(m.resolve(call.data.get("parcel_id"), call.data.get("slot")))

    async def add_tracking(call: ServiceCall) -> None:
        m = _manager(hass)
        await m.async_add_tracking(m.resolve(call.data.get("parcel_id"), call.data.get("slot")), call.data["tracking"])

    async def scan(call: ServiceCall) -> None:
        m = _manager(hass)
        await m.async_scan()
        await m.async_reprocess()
        await m.async_track()

    async def delivered(call: ServiceCall) -> ServiceResponse:
        """Delivered parcels with their order lines, for clients that catch up (stock keeping, spreadsheets, ...)."""
        since = call.data.get("since") or dt_util.now() - timedelta(days=30)
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt_util.get_default_time_zone())
        return {"parcels": _manager(hass).delivered(since), "now": dt_util.now().isoformat()}

    hass.services.async_register(DOMAIN, "reload_rules", reload_rules, vol.Schema({}))
    hass.services.async_register(DOMAIN, "test_email", test_email,
                                 vol.Schema({vol.Required("subject"): cv.string, vol.Required("sender"): cv.string,
                                             vol.Required("body"): cv.string, vol.Optional("date"): cv.datetime}),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "delivered", delivered, vol.Schema({vol.Optional("since"): cv.datetime}),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "received", received,
                                 vol.Schema({**TARGET, vol.Optional("received", default=True): cv.boolean}))
    hass.services.async_register(DOMAIN, "follow_up", follow_up,
                                 vol.Schema({**TARGET, vol.Optional("flag"): cv.boolean,
                                             vol.Optional("reason"): cv.string}))
    hass.services.async_register(DOMAIN, "merge", merge,
                                 vol.Schema({vol.Required("parcel_id"): cv.string, vol.Required("into"): cv.string}))
    hass.services.async_register(DOMAIN, "remove", remove, vol.Schema(TARGET))
    hass.services.async_register(DOMAIN, "add_tracking", add_tracking,
                                 vol.Schema({**TARGET, vol.Required("tracking"): cv.string}))
    hass.services.async_register(DOMAIN, "scan", scan, vol.Schema({}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if uses_own_credentials(entry.data):     # legacy entries never needed a particular imap entry
        imap = mailbox.imap_entry(hass, entry.data)
        if imap is None or imap.state is not ConfigEntryState.LOADED:
            raise ConfigEntryNotReady("The Home Assistant IMAP entry Parcels reads its mail from is missing or not "
                                      "loaded; fix it under Settings > Devices & services > IMAP")
    manager = ParcelsManager(hass, entry)
    entry.runtime_data = manager
    if manager.own:      # settings changed in the options flow: start again with the new login / AI
        entry.async_on_unload(entry.add_update_listener(_reload_on_update))
    await manager.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_flush()
    return ok
