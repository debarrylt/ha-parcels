"""Tests for the AI backends, the legacy-vs-own-credentials branch, the IMAP login checks and the flow data split.

Pure Python: no Home Assistant, no network (aioimaplib and Home Assistant are replaced by fakes).
"""
import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest

from parcels_core import const as C
from parcels_core import flow_helpers as F
from parcels_core import llm as L
from parcels_core import mailbox as MB
from parcels_core import parser as P

EMAIL = "Your order has shipped\nCarrier Evri, tracking number H09XYZ0000000042.\nArriving on 2026-11-06."
ANSWER = ('{"is_shipping": true, "merchant": "Acme Store", "item": "USB-C hub", "lines": [{"name": "USB-C hub", '
          '"variant": null, "quantity": 2}], "tracking_numbers": ["H09XYZ0000000042", "NOTINEMAIL12345"], '
          '"order_number": "A-1", "status": "in_transit", "problem": null, "eta_date": "2026-11-06", "eta_window": null}')


def _ex():
    return P.extract("Your order has shipped", "shop@example.com", EMAIL, datetime(2026, 11, 5, 12, tzinfo=timezone.utc))


def run(coro):
    return asyncio.run(coro)


# --- AI reply parsing -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    ANSWER,
    "```json\n" + ANSWER + "\n```",
    "```\n" + ANSWER + "\n```",
    "Here is the JSON:\n```JSON\n" + ANSWER + "\n```\nHope that helps!",
    "Sure. " + ANSWER + " Done.",
    {"is_shipping": True, "merchant": "Acme Store"},
])
def test_parse_json_answer_accepts_plain_fenced_and_chatty(reply):
    answer = L.parse_json_answer(reply)
    assert answer["is_shipping"] is True and answer["merchant"] == "Acme Store"


@pytest.mark.parametrize("reply", ["", "no json here", "[1, 2]", None, 5, "```json\n{broken\n```"])
def test_parse_json_answer_rejects_unusable(reply):
    with pytest.raises(ValueError):
        L.parse_json_answer(reply)


class FakeServices:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append((domain, service, data, blocking, return_response))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_extract_ai_task_calls_generate_data_and_validates_the_answer():
    hass = types.SimpleNamespace(services=FakeServices({"conversation_id": "x", "data": "```json\n" + ANSWER + "\n```"}))
    out = run(L.extract_ai_task(hass, "ai_task.my_model", _ex(), EMAIL))
    domain, service, data, blocking, ret = hass.services.calls[0]
    assert (domain, service, blocking, ret) == ("ai_task", "generate_data", True, True)
    assert data["entity_id"] == "ai_task.my_model" and data["task_name"] == "parcels"
    assert "Subject: Your order has shipped" in data["instructions"] and '"is_shipping"' in data["instructions"]
    assert out.item == "USB-C hub" and out.merchant == "Acme Store" and out.order == "A-1"
    assert out.lines == [{"name": "USB-C hub", "variant": None, "qty": 2}]
    assert out.tracking == ["H09XYZ0000000042"]          # the invented number is not in the email: dropped


def test_extract_ai_task_errors_are_raised_so_the_email_is_retried():
    for result in ({"data": "I cannot help with that"}, {}, None, RuntimeError("entity unavailable")):
        hass = types.SimpleNamespace(services=FakeServices(result))
        with pytest.raises((ValueError, RuntimeError)):
            run(L.extract_ai_task(hass, "ai_task.x", _ex(), EMAIL))


def test_model_available_matches_with_and_without_tag():
    tags = {"models": [{"name": "qwen3:30b"}, {"name": "llama3:latest"}]}
    assert L.model_available(tags, "qwen3:30b") and L.model_available(tags, "llama3")
    assert not L.model_available(tags, "qwen3:8b") and not L.model_available({}, "x") and not L.model_available(None, "x")


# --- legacy vs v2 entries ---------------------------------------------------------------------------------------------
def test_uses_own_credentials_only_when_the_entry_names_an_imap_entry():
    assert C.uses_own_credentials({C.CONF_IMAP_ENTRY: "abc"})
    assert not C.uses_own_credentials({})                      # empty data: legacy, borrows HA integrations
    assert C.FLOW_VERSION == 1   # existing private entries are version 1: no migration


def test_connect_passes_the_imap_entry_data_to_home_assistant(monkeypatch):
    seen = {}

    async def fake_borrowed(data):
        seen["data"] = dict(data)
        return "client"

    for name in ("homeassistant", "homeassistant.components", "homeassistant.components.imap"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    coord = types.ModuleType("homeassistant.components.imap.coordinator")
    coord.connect_to_server = fake_borrowed
    monkeypatch.setitem(sys.modules, "homeassistant.components.imap.coordinator", coord)
    assert run(MB._connect({"server": "imap.gmail.com", "username": "x"})) == "client"
    assert seen["data"]["server"] == "imap.gmail.com"


def test_connect_names_the_ha_version_when_connect_to_server_is_gone(monkeypatch):
    for name in ("homeassistant", "homeassistant.components", "homeassistant.components.imap"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "homeassistant.components.imap.coordinator", types.ModuleType("x"))
    with pytest.raises(MB.MailboxError, match="Home Assistant version"):
        run(MB._connect({}))


# --- imap_entry selection -------------------------------------------------------------------------------------------
def _hass(*entries):
    by_id = {e.entry_id: e for e in entries}
    cm = types.SimpleNamespace(async_get_entry=by_id.get,
                               async_entries=lambda domain: [e for e in entries if e.domain == domain])
    return types.SimpleNamespace(config_entries=cm)


def _entry(entry_id, state="loaded", domain="imap"):
    return types.SimpleNamespace(entry_id=entry_id, domain=domain, state=types.SimpleNamespace(value=state), data={})


def test_imap_entry_v2_uses_the_stored_id():
    a, b = _entry("a"), _entry("b")
    assert MB.imap_entry(_hass(a, b), {C.CONF_IMAP_ENTRY: "b"}) is b


def test_imap_entry_v2_missing_or_wrong_domain_is_none():
    other = _entry("x", domain="mqtt")
    assert MB.imap_entry(_hass(_entry("a")), {C.CONF_IMAP_ENTRY: "gone"}) is None
    assert MB.imap_entry(_hass(other), {C.CONF_IMAP_ENTRY: "x"}) is None


def test_imap_entry_legacy_takes_the_first_loaded_entry_else_the_first():
    a, b = _entry("a", "setup_error"), _entry("b")
    assert MB.imap_entry(_hass(a, b), {}) is b
    assert MB.imap_entry(_hass(a), {}) is a
    assert MB.imap_entry(_hass(), {}) is None


def test_check_imap_entry(monkeypatch):
    calls = []

    class Client:
        async def logout(self):
            calls.append("logout")

    async def ok(data):
        calls.append("connect")
        return Client()

    monkeypatch.setattr(MB, "_connect", ok)
    run(MB.check_imap_entry(_hass(_entry("a")), "a"))
    assert calls == ["connect", "logout"]
    for hass, eid in ((_hass(_entry("a", "setup_retry")), "a"), (_hass(), "a")):
        with pytest.raises(MB.ImapUnavailable):
            run(MB.check_imap_entry(hass, eid))

    async def bad(data):
        raise OSError("refused")

    monkeypatch.setattr(MB, "_connect", bad)
    with pytest.raises(MB.CannotConnect):
        run(MB.check_imap_entry(_hass(_entry("a")), "a"))


# --- Ship24 key check -----------------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status=200, error=None, hang=False):
        self.status, self.error, self.hang, self.calls = status, error, hang, []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self.error:
            raise self.error
        if self.hang:
            return Hang()
        return FakeResp(self.status)


class Hang(FakeResp):
    async def __aenter__(self):
        await asyncio.sleep(3600)


@pytest.mark.parametrize("status,expected", [(200, None), (401, "invalid_ship24_key"), (403, "invalid_ship24_key"),
                                             (500, "ship24_unreachable"), (429, "ship24_unreachable")])
def test_check_ship24_key_by_status(status, expected):
    session = FakeSession(status)
    assert run(F.check_ship24_key(session, "apik_secret")) == expected
    assert session.calls == [("https://api.ship24.com/public/v1/couriers", {"Authorization": "Bearer apik_secret"})]


def test_check_ship24_key_network_error_and_timeout(monkeypatch):
    assert run(F.check_ship24_key(FakeSession(error=OSError("down")), "k")) == "ship24_unreachable"
    monkeypatch.setattr(F, "SHIP24_TIMEOUT", 0.05)
    assert run(F.check_ship24_key(FakeSession(hang=True), "k")) == "ship24_unreachable"


# --- config flow data / options split -----------------------------------------------------------------------------
FULL = {C.CONF_IMAP_ENTRY: "imap1",
        C.CONF_T17_USERNAME: "me@example.com", C.CONF_T17_PASSWORD: "t17-pw", C.CONF_SHIP24_KEY: "apik_x",
        C.CONF_AI_MODE: "ollama", C.CONF_OLLAMA_URL: "http://host:11434", C.CONF_OLLAMA_MODEL: "qwen3:30b",
        C.CONF_OLLAMA_KEY: "ok", C.CONF_AI_ENTITY: "ai_task.stale", C.CONF_HOME_POSTCODE: "ZZ9 9ZZ",
        C.CONF_HOME_NAMES: "Alex, Sam"}


def test_split_puts_credentials_in_data_and_the_rest_in_options():
    data, options = F.split_data_options(FULL)
    assert set(data) == {C.CONF_IMAP_ENTRY, C.CONF_T17_USERNAME, C.CONF_T17_PASSWORD, C.CONF_SHIP24_KEY, C.CONF_OLLAMA_KEY}
    assert options == {C.CONF_AI_MODE: "ollama", C.CONF_OLLAMA_URL: "http://host:11434", C.CONF_OLLAMA_MODEL: "qwen3:30b",
                       C.CONF_HOME_POSTCODE: "ZZ9 9ZZ", C.CONF_HOME_NAMES: "Alex, Sam"}   # stale ai_task entity dropped
    assert C.uses_own_credentials(data)


def test_split_minimal_rules_only_setup():
    data, options = F.split_data_options({C.CONF_IMAP_ENTRY: "imap1", C.CONF_T17_USERNAME: "u", C.CONF_T17_PASSWORD: "",
                                          C.CONF_SHIP24_KEY: "", C.CONF_HOME_POSTCODE: ""})
    assert data == {C.CONF_IMAP_ENTRY: "imap1"}   # no half-filled 17TRACK login
    assert options == {C.CONF_AI_MODE: "none"}


def test_split_ai_task_mode_keeps_only_the_entity():
    _, options = F.split_data_options({**FULL, C.CONF_AI_MODE: "ai_task", C.CONF_AI_ENTITY: "ai_task.gpt"})
    assert options[C.CONF_AI_ENTITY] == "ai_task.gpt" and C.CONF_OLLAMA_URL not in options


def test_options_flow_round_trip_keeps_blank_passwords():
    data, options = F.split_data_options(FULL)
    state = F.state_from_entry(data, options)
    state[C.CONF_SHIP24_KEY] = F.merge_secret("", state.get(C.CONF_SHIP24_KEY))            # left blank
    state[C.CONF_T17_PASSWORD] = F.merge_secret("new-pw", state.get(C.CONF_T17_PASSWORD))
    state[C.CONF_AI_MODE] = "none"
    data2, options2 = F.split_data_options(state)
    assert data2[C.CONF_SHIP24_KEY] == "apik_x" and data2[C.CONF_T17_PASSWORD] == "new-pw"
    assert options2 == {C.CONF_AI_MODE: "none", C.CONF_HOME_POSTCODE: "ZZ9 9ZZ", C.CONF_HOME_NAMES: "Alex, Sam"}
    assert C.CONF_OLLAMA_KEY not in data2                                                  # AI switched off: key dropped


def test_merge_secret_and_url_helpers():
    assert F.merge_secret("", "old") == "old" and F.merge_secret(None, None) == "" and F.merge_secret(" x ", "old") == "x"
    assert F.normalise_url("host:11434/") == "http://host:11434" and F.normalise_url("https://h/") == "https://h"
    assert F.home_names({C.CONF_HOME_NAMES: " Alex , ,Sam"}) == ["Alex", "Sam"]
