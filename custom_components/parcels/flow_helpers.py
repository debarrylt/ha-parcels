"""Pure helpers for the config and options flows (no Home Assistant imports, unit-tested).

The flows collect everything in one flat `state` dict. Credentials end up in `entry.data`, everything else in
`entry.options`; both flows use `split_data_options` so the split is defined once.
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

from .const import (AI_NONE, AI_OLLAMA, AI_TASK, CONF_AI_ENTITY, CONF_AI_MODE, CONF_HOME_NAMES,
                    CONF_IMAP_ENTRY, SHIP24_URL,
                    CONF_OLLAMA_KEY, CONF_OLLAMA_MODEL, CONF_OLLAMA_URL, CONF_T17_PASSWORD,
                    CONF_T17_USERNAME, DATA_KEYS, DEFAULT_OLLAMA_MODEL, OPTION_KEYS)

EMAIL_KEYS = (CONF_IMAP_ENTRY,)


def merge_secret(new: Any, old: Any) -> str:
    """A password field left blank keeps the current value."""
    new = (new or "").strip() if isinstance(new, str) else new
    return new if new else (old or "")


def state_from_entry(data: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """The flat state an options flow starts from."""
    return {**data, **options}


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def split_data_options(state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(entry.data, entry.options) from the flow state.

    Credentials (the chosen IMAP entry, 17TRACK login, Ship24 and Ollama API keys) go to data, the rest to options. Settings that
    do not belong to the chosen AI mode, or to a missing 17TRACK login, are dropped; empty values are dropped.
    """
    s = {k: _clean(v) for k, v in state.items()}
    mode = s.get(CONF_AI_MODE) or AI_NONE
    s[CONF_AI_MODE] = mode
    if mode != AI_TASK:
        s.pop(CONF_AI_ENTITY, None)
    if mode != AI_OLLAMA:
        for k in (CONF_OLLAMA_URL, CONF_OLLAMA_MODEL, CONF_OLLAMA_KEY):
            s.pop(k, None)
    if not (s.get(CONF_T17_USERNAME) and s.get(CONF_T17_PASSWORD)):
        s.pop(CONF_T17_USERNAME, None)
        s.pop(CONF_T17_PASSWORD, None)
    if mode == AI_OLLAMA:
        s[CONF_OLLAMA_MODEL] = s.get(CONF_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL
    data = {k: s[k] for k in DATA_KEYS if s.get(k) not in (None, "")}
    options = {k: s[k] for k in OPTION_KEYS if s.get(k) not in (None, "")}
    return data, options


def normalise_url(url: str) -> str:
    """http://host:11434/ -> http://host:11434 (a scheme is added when missing)."""
    url = (url or "").strip().rstrip("/")
    return url if "://" in url or not url else "http://" + url


def home_names(options: Mapping[str, Any]) -> list[str]:
    return [n.strip() for n in (options.get(CONF_HOME_NAMES) or "").split(",") if n.strip()]



SHIP24_TIMEOUT = 15


async def check_ship24_key(session: Any, key: str) -> str | None:
    """Test a Ship24 API key with GET /couriers (answers 401 to a bad key, costs no tracker quota).

    Returns None when the key works, "invalid_ship24_key" for 401/403 and "ship24_unreachable" for any other answer,
    a timeout or a network error. `session` is an aiohttp client session. The key is never logged.
    """
    async def call() -> int:
        async with session.get(f"{SHIP24_URL}/couriers", headers={"Authorization": f"Bearer {key}"}) as resp:
            return resp.status

    try:
        status = await asyncio.wait_for(call(), SHIP24_TIMEOUT)
    except Exception:  # noqa: BLE001 - timeout, DNS, TLS, connection reset: all "unreachable"
        return "ship24_unreachable"
    if status == 200:
        return None
    return "invalid_ship24_key" if status in (401, 403) else "ship24_unreachable"
