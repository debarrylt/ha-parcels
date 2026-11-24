"""Shop order page check and the tracking box, inside Home Assistant (no network)."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parcels import model, shop
from custom_components.parcels.const import DOMAIN

from test_flow_and_setup import FETCH_NEW, add_imap

CHECK = "custom_components.parcels.shop.check"
ON = {"shop_check": True, "shop_check_start_days": 3, "shop_check_every_days": 2, "shop_check_stop_days": 30}


async def _setup(hass, options):
    add_imap(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Parcels", version=1, data={}, options=options)
    entry.add_to_hass(hass)
    with patch(FETCH_NEW, AsyncMock(return_value=[])), patch(CHECK, AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    m = entry.runtime_data
    p = model.new_parcel(dt_util.now() - timedelta(days=5))
    p.update(merchant="Acme", order="A1", status="ordered", shop_order_url="https://shop.example.com/x")
    m.data["parcels"][p["id"]] = p
    m._changed()
    await hass.async_block_till_done()
    return entry, m, p


async def _run(m, *results):
    with patch(CHECK, AsyncMock(side_effect=results)) as c:
        for _ in results or (None,):
            await m.async_shop_check()
    return c.await_count


async def test_off_by_default(hass):
    _, m, p = await _setup(hass, {})
    assert await _run(m) == 0 and "shop_check" not in p


async def test_found(hass):
    events = []
    hass.bus.async_listen("parcels_update", lambda e: events.append(e.data["kind"]))
    _, m, p = await _setup(hass, ON)
    assert await _run(m, shop.Result(shop.FOUND, "ZS00000000042")) == 1
    assert p["tracking"] == ["ZS00000000042"] and p["status"] == "shipped" and not p["flagged"]
    assert p["shop_check"]["done"] and p["shop_check"]["tries"] == 1 and await _run(m) == 0
    await hass.async_block_till_done()
    assert "follow_up" not in events


async def test_unreadable_flags_with_an_event_and_stops(hass):
    events = []
    hass.bus.async_listen("parcels_update", lambda e: events.append(e.data["kind"]))
    _, m, p = await _setup(hass, ON)
    assert await _run(m, shop.Result(shop.UNREADABLE, reason="login needed")) == 1
    await hass.async_block_till_done()
    assert p["flagged"] and events.count("follow_up") == 1 and await _run(m) == 0


async def test_not_yet_waits_for_the_next_check(hass):
    _, m, p = await _setup(hass, ON)
    assert await _run(m, shop.Result(shop.NOT_YET)) == 1
    assert not p["flagged"] and await _run(m) == 0
    p["shop_check"]["at"] = (dt_util.now() - timedelta(days=2, minutes=1)).isoformat()
    assert await _run(m, shop.Result(shop.NOT_YET)) == 1 and p["shop_check"]["tries"] == 2


async def test_giving_up_does_not_visit(hass):
    _, m, p = await _setup(hass, ON)
    p.update(created=(dt_util.now() - timedelta(days=31)).isoformat(),
             shop_check={"at": (dt_util.now() - timedelta(days=3)).isoformat(), "tries": 9})
    assert await _run(m) == 0 and p["flagged"]


async def test_an_email_that_arrives_during_the_visit_wins(hass):
    _, m, p = await _setup(hass, ON)

    async def visit(_url):
        p["tracking"].append("REAL12345678")
        return shop.Result(shop.UNREADABLE)
    with patch(CHECK, visit):
        await m.async_shop_check()
    assert p["tracking"] == ["REAL12345678"] and not p["flagged"]


async def test_tracking_box_and_action(hass):
    _, m, p = await _setup(hass, {})
    assert hass.states.get("text.parcels_slot_1_tracking").state == ""
    assert hass.states.get("text.parcels_slot_2_tracking").state == "unavailable"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "add_tracking", {"slot": 1, "tracking": "nope"}, blocking=True)
    await hass.services.async_call("text", "set_value", {"entity_id": "text.parcels_slot_1_tracking", "value": "zs00000000042"}, blocking=True)
    await hass.async_block_till_done()
    assert p["tracking"] == ["ZS00000000042"]
    assert hass.states.get("text.parcels_slot_1_tracking").state == "unavailable"


async def test_legacy_options_have_the_settings(hass):
    entry, m, p = await _setup(hass, {"home_postcode": "AB1 2CD"})
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["step_id"] == "legacy"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {**ON, "home_postcode": "AB1 2CD", "shop_check_start_days": 40})
    assert r["errors"] == {"shop_check_stop_days": "shop_stop_before_start"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {**ON, "home_postcode": "AB1 2CD"})
    assert r["type"] is FlowResultType.CREATE_ENTRY and entry.options["shop_check"] is True
    assert entry.options["shop_check_every_days"] == 2 and entry.options["home_postcode"] == "AB1 2CD"
