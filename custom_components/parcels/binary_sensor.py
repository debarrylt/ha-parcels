"""binary_sensor.parcels_ai_online: the configured AI (AI Task entity or Ollama) answers, so rich email details are processed."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ParcelsEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([AiOnline(entry.runtime_data)])


class AiOnline(ParcelsEntity, BinarySensorEntity):
    _attr_name = "AI online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, manager) -> None:
        super().__init__(manager, "ai_online")

    @property
    def is_on(self) -> bool:
        return self.manager.ai_online

    @property
    def extra_state_attributes(self) -> dict:
        return {"pending_emails": len(self.manager._pending())}
