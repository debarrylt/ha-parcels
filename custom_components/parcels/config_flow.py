"""Config flow: email, optional 17TRACK / Ship24, AI, home address; the options flow repeats the same pages.

Steps: user (pick a Home Assistant IMAP entry, validated) -> tracking (17TRACK login validated, Ship24 key) -> ai (none | ai_task | ollama)
-> ai_task or ollama (Ollama validated) -> home. Credentials are stored in `entry.data`, the rest in `entry.options`
(see flow_helpers.split_data_options). A password left blank in the options flow keeps the stored one.

An entry with empty data (made before this flow existed) is the legacy mode that borrows the imap / ollama /
seventeentrack integrations; its options flow keeps the old single page.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import probatio as vol  # HA 2026.11+ (voluptuous replacement)
except ImportError:  # pragma: no cover
    import voluptuous as vol

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (EntitySelector, EntitySelectorConfig, SelectSelector, SelectSelectorConfig, SelectSelectorMode, TextSelector,
                                            TextSelectorConfig, TextSelectorType)

from . import llm, mailbox
from .const import (AI_NONE, AI_OLLAMA, AI_TASK, CONF_AI_ENTITY, CONF_AI_MODE, CONF_HOME_NAMES, CONF_HOME_POSTCODE,
                    CONF_IMAP_ENTRY,
                    CONF_OLLAMA_KEY, CONF_OLLAMA_MODEL, CONF_OLLAMA_URL, CONF_SHIP24_KEY, CONF_T17_PASSWORD,
                    CONF_T17_USERNAME, DEFAULT_OLLAMA_MODEL, DOMAIN, FLOW_VERSION,
                    uses_own_credentials)
from .flow_helpers import EMAIL_KEYS, check_ship24_key, merge_secret, normalise_url, split_data_options, state_from_entry

_LOGGER = logging.getLogger(__name__)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


class _Steps:
    """The pages shared by the config flow and the options flow. `_state` is the flat dict of all answers."""

    _state: dict[str, Any]
    _editing = False          # options flow / reconfigure: secrets may stay blank
    hass: Any

    async def _finish(self) -> ConfigFlowResult:  # pragma: no cover - implemented by the flows
        raise NotImplementedError

    async def _after_email(self) -> ConfigFlowResult:
        return await self.async_step_tracking()

    def _form(self, step_id: str, schema: dict, errors: dict[str, str] | None = None, placeholders: dict | None = None):
        shaped = self.add_suggested_values_to_schema(vol.Schema(schema), {k: v for k, v in self._state.items()
                                                                           if "password" not in k and "api_key" not in k})
        return self.async_show_form(step_id=step_id, data_schema=shaped, errors=errors or {},
                                    description_placeholders=placeholders or {})

    # ------------------------------------------------------------------ email (Home Assistant's IMAP integration)
    async def _email_step(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        imap_entries = self.hass.config_entries.async_entries("imap")
        if not imap_entries:
            return self.async_abort(reason="no_imap")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._state = {**self._state, CONF_IMAP_ENTRY: user_input[CONF_IMAP_ENTRY]}
            try:
                await mailbox.check_imap_entry(self.hass, user_input[CONF_IMAP_ENTRY])
            except mailbox.ImapUnavailable:
                errors[CONF_IMAP_ENTRY] = "imap_not_loaded"
            except mailbox.MailboxError:
                errors["base"] = "cannot_connect"
            else:
                return await self._after_email()
        options = [{"value": e.entry_id, "label": e.title} for e in imap_entries]
        return self._form(step_id, {vol.Required(CONF_IMAP_ENTRY): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))}, errors)

    # ------------------------------------------------------------------ 17TRACK + Ship24
    async def async_step_tracking(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user = (user_input.get(CONF_T17_USERNAME) or "").strip()
            password = merge_secret(user_input.get(CONF_T17_PASSWORD), self._state.get(CONF_T17_PASSWORD))
            old_key = self._state.get(CONF_SHIP24_KEY)
            ship24 = merge_secret(user_input.get(CONF_SHIP24_KEY), old_key)
            self._state = {**self._state, CONF_T17_USERNAME: user, CONF_T17_PASSWORD: password, CONF_SHIP24_KEY: ship24}
            if ship24 and ship24 != old_key:
                if code := await check_ship24_key(async_get_clientsession(self.hass), ship24):
                    errors[CONF_SHIP24_KEY] = code
            if user and not password:
                errors[CONF_T17_PASSWORD] = "invalid_auth"
            elif user and not errors:
                errors.update(await self._check_17track(user, password))
            if not errors:
                return await self.async_step_ai()
        return self._form("tracking", {
            vol.Optional(CONF_T17_USERNAME): TextSelector(TextSelectorConfig(autocomplete="username")),
            vol.Optional(CONF_T17_PASSWORD): PASSWORD,
            vol.Optional(CONF_SHIP24_KEY): PASSWORD,
        }, errors)

    async def _check_17track(self, user: str, password: str) -> dict[str, str]:
        """Log in the way HA's seventeentrack integration does (its own client session)."""
        from pyseventeentrack import Client
        from pyseventeentrack.errors import SeventeenTrackError
        client = Client(session=async_create_clientsession(self.hass))
        try:
            if not await asyncio.wait_for(client.profile.login(user, password), 20):
                return {"base": "invalid_auth"}
        except (SeventeenTrackError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Parcels: 17TRACK login check failed: %s", err)
            return {"base": "cannot_connect"}
        return {}

    # ------------------------------------------------------------------ AI
    async def async_step_ai(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._state = {**self._state, CONF_AI_MODE: user_input[CONF_AI_MODE]}
            return await {AI_TASK: self.async_step_ai_task, AI_OLLAMA: self.async_step_ollama}.get(
                user_input[CONF_AI_MODE], self.async_step_home)()
        self._state.setdefault(CONF_AI_MODE, AI_NONE)
        return self._form("ai", {vol.Required(CONF_AI_MODE): SelectSelector(SelectSelectorConfig(
            options=[AI_NONE, AI_TASK, AI_OLLAMA], translation_key="ai_mode", mode=SelectSelectorMode.LIST))})

    async def async_step_ai_task(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._state = {**self._state, CONF_AI_ENTITY: user_input[CONF_AI_ENTITY]}
            return await self.async_step_home()
        return self._form("ai_task", {vol.Required(CONF_AI_ENTITY): EntitySelector(EntitySelectorConfig(domain="ai_task"))})

    async def async_step_ollama(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalise_url(user_input[CONF_OLLAMA_URL])
            model = (user_input.get(CONF_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL).strip()
            key = merge_secret(user_input.get(CONF_OLLAMA_KEY), self._state.get(CONF_OLLAMA_KEY))
            self._state = {**self._state, CONF_OLLAMA_URL: url, CONF_OLLAMA_MODEL: model, CONF_OLLAMA_KEY: key}
            errors.update(await self._check_ollama(url, key, model))
            if not errors:
                return await self.async_step_home()
        self._state.setdefault(CONF_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)
        return self._form("ollama", {
            vol.Required(CONF_OLLAMA_URL): URL,
            vol.Required(CONF_OLLAMA_MODEL): TextSelector(),
            vol.Optional(CONF_OLLAMA_KEY): PASSWORD,
        }, errors)

    async def _check_ollama(self, url: str, key: str, model: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with async_get_clientsession(self.hass).get(url + "/api/tags", headers=headers,
                                                              timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {CONF_OLLAMA_URL: "cannot_connect"}
                tags = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {CONF_OLLAMA_URL: "cannot_connect"}
        return {} if llm.model_available(tags, model) else {CONF_OLLAMA_MODEL: "model_not_found"}

    # ------------------------------------------------------------------ home address
    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._state = {**self._state, CONF_HOME_POSTCODE: (user_input.get(CONF_HOME_POSTCODE) or "").strip(),
                           CONF_HOME_NAMES: (user_input.get(CONF_HOME_NAMES) or "").strip()}
            return await self._finish()
        return self._form("home", {vol.Optional(CONF_HOME_POSTCODE): TextSelector(),
                                   vol.Optional(CONF_HOME_NAMES): TextSelector()})


class ParcelsConfigFlow(_Steps, ConfigFlow, domain=DOMAIN):
    VERSION = FLOW_VERSION

    def __init__(self) -> None:
        self._state = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._email_step("user", user_input)

    async def _finish(self) -> ConfigFlowResult:
        data, options = split_data_options(self._state)
        return self.async_create_entry(title="Parcels", data=data, options=options)

    # -- reconfigure: only the IMAP entry
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if not uses_own_credentials(entry.data):
            return self.async_abort(reason="legacy_entry")
        if not self._state:
            self._state = state_from_entry(entry.data, entry.options)
            self._editing = True
        return await self._email_step("reconfigure", user_input)

    async def _after_email(self) -> ConfigFlowResult:
        if self.source == "reconfigure":
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(),
                                                      data_updates={k: self._state[k] for k in EMAIL_KEYS})
        return await super()._after_email()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ParcelsOptionsFlow()


class ParcelsOptionsFlow(_Steps, OptionsFlow):
    _editing = True

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if not uses_own_credentials(entry.data):
            return await self.async_step_legacy()
        if not hasattr(self, "_state"):
            self._state = state_from_entry(entry.data, entry.options)
        return await self.async_step_tracking()      # the mailbox is the IMAP entry; change it with Reconfigure

    async def _finish(self) -> ConfigFlowResult:
        data, options = split_data_options(self._state)
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)   # the update listener reloads
        return self.async_create_entry(data=options)

    async def async_step_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Legacy (borrowed integrations) entries: home address check and Ship24 key only."""
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        if user_input is not None:
            new = {k: (user_input.get(k) or "").strip() for k in (CONF_HOME_POSTCODE, CONF_HOME_NAMES, CONF_SHIP24_KEY)}
            if new[CONF_SHIP24_KEY] and new[CONF_SHIP24_KEY] != opts.get(CONF_SHIP24_KEY):
                if code := await check_ship24_key(async_get_clientsession(self.hass), new[CONF_SHIP24_KEY]):
                    errors[CONF_SHIP24_KEY] = code
            if not errors:
                return self.async_create_entry(data=new)
        schema = vol.Schema({
            vol.Optional(CONF_HOME_POSTCODE, default=opts.get(CONF_HOME_POSTCODE, "")): TextSelector(),
            vol.Optional(CONF_HOME_NAMES, default=opts.get(CONF_HOME_NAMES, "")): TextSelector(),
            vol.Optional(CONF_SHIP24_KEY, default=opts.get(CONF_SHIP24_KEY, "")): PASSWORD})
        return self.async_show_form(step_id="legacy", data_schema=schema, errors=errors)
