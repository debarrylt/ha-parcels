"""Config flow, options flow and entry setup inside Home Assistant, with the IMAP connection mocked (no network)."""
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parcels.const import DOMAIN

CONNECT = "homeassistant.components.imap.coordinator.connect_to_server"
FETCH_NEW = "custom_components.parcels.mailbox.fetch_new"
CHECK_SHIP24 = "custom_components.parcels.config_flow.check_ship24_key"


def fake_client():
    client = MagicMock()
    client.logout = AsyncMock()
    return client


def add_imap(hass, state=ConfigEntryState.LOADED, title="Shipping mailbox"):
    entry = MockConfigEntry(domain="imap", title=title, data={"server": "imap.example.com", "folder": "INBOX"})
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _flow_to_entry(hass, imap):
    r = await _start(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    with patch(CONNECT, AsyncMock(return_value=fake_client())):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": imap.entry_id})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "tracking", r
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["step_id"] == "ai", r
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ai_mode": "none"})
    assert r["step_id"] == "home", r
    with patch(FETCH_NEW, AsyncMock(return_value=[])):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"home_postcode": "AB1 2CD"})
        await hass.async_block_till_done()
    return r


async def test_no_imap_entry_aborts(hass):
    r = await _start(hass)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_imap"


async def test_imap_entry_not_loaded_shows_error(hass):
    imap = add_imap(hass, ConfigEntryState.SETUP_RETRY)
    r = await _start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": imap.entry_id})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    assert r["errors"] == {"imap_entry_id": "imap_not_loaded"}


async def test_connect_failure_shows_cannot_connect(hass):
    imap = add_imap(hass)
    r = await _start(hass)
    with patch(CONNECT, AsyncMock(side_effect=OSError("refused"))):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": imap.entry_id})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}


async def test_full_flow_creates_entry_and_sets_up(hass):
    imap = add_imap(hass)
    r = await _flow_to_entry(hass, imap)
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["imap_entry_id"] == imap.entry_id
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.parcels_in_flight") is not None
    assert hass.states.get("sensor.parcels_slot_1") is not None
    # the options flow starts at the tracking page (the mailbox is the IMAP entry)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "tracking", r
    # a second instance is refused
    r = await _start(hass)
    assert r["type"] is FlowResultType.ABORT
    with patch(FETCH_NEW, AsyncMock(return_value=[])):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_retries_when_the_imap_entry_is_not_loaded(hass):
    imap = add_imap(hass)
    await _flow_to_entry(hass, imap)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    with patch(FETCH_NEW, AsyncMock(return_value=[])):
        assert await hass.config_entries.async_unload(entry.entry_id)
    imap.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_bad_ship24_key_shows_error(hass):
    imap = add_imap(hass)
    r = await _start(hass)
    with patch(CONNECT, AsyncMock(return_value=fake_client())):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": imap.entry_id})
    with patch(CHECK_SHIP24, AsyncMock(return_value="invalid_ship24_key")):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ship24_api_key": "bad"})
    assert r["step_id"] == "tracking" and r["errors"] == {"ship24_api_key": "invalid_ship24_key"}
    with patch(CHECK_SHIP24, AsyncMock(return_value=None)) as check:
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ship24_api_key": "good"})
    assert r["step_id"] == "ai" and check.await_args.args[1] == "good"


async def test_options_keep_secrets_when_blank(hass):
    imap = add_imap(hass)
    r = await _start(hass)
    with patch(CONNECT, AsyncMock(return_value=fake_client())), patch(CHECK_SHIP24, AsyncMock(return_value=None)), \
         patch(FETCH_NEW, AsyncMock(return_value=[])):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": imap.entry_id})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ship24_api_key": "apik_secret"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"ai_mode": "none"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["ship24_api_key"] == "apik_secret"

    r = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CHECK_SHIP24, AsyncMock(return_value=None)) as check, patch(FETCH_NEW, AsyncMock(return_value=[])):
        steps = []
        for _ in range(6):
            if r["type"] is not FlowResultType.FORM:
                break
            steps.append(r["step_id"])
            user = {"tracking": {}, "ai": {"ai_mode": "none"}, "home": {"home_postcode": "ZZ9 9ZZ"}}[r["step_id"]]
            r = await hass.config_entries.options.async_configure(r["flow_id"], user)
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY, (steps, r)
    assert steps == ["tracking", "ai", "home"]
    assert not check.await_count                      # the key was left blank = unchanged: not checked again
    assert entry.data["ship24_api_key"] == "apik_secret", entry.data
    assert entry.data["imap_entry_id"] == imap.entry_id
    assert entry.options.get("home_postcode") == "ZZ9 9ZZ", entry.options
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_picks_another_imap_entry(hass):
    imap = add_imap(hass)
    await _flow_to_entry(hass, imap)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    other = add_imap(hass, title="Second mailbox")
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reconfigure"
    with patch(CONNECT, AsyncMock(return_value=fake_client())), patch(FETCH_NEW, AsyncMock(return_value=[])):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"imap_entry_id": other.entry_id})
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful", r
    assert entry.data["imap_entry_id"] == other.entry_id


async def test_existing_legacy_entry_loads_without_migration(hass):
    """An entry from the first private version (version 1, empty data, home options) loads as legacy mode."""
    add_imap(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Parcels", version=1, data={},
                            options={"home_postcode": "AB1 2CD", "home_names": "Sam", "ship24_api_key": "k"})
    entry.add_to_hass(hass)
    with patch(FETCH_NEW, AsyncMock(return_value=[])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 1 and entry.data == {}
    assert hass.states.get("sensor.parcels_in_flight") is not None
