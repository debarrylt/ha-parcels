"""button.parcels_slot_N_received / _follow_up: act on whichever parcel is in row N (dashboards)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SLOTS
from .entity import ParcelsEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    m = entry.runtime_data
    add([b for n in range(1, SLOTS + 1) for b in (Received(m, n), FollowUp(m, n))])


class _SlotButton(ParcelsEntity, ButtonEntity):
    def __init__(self, manager, n: int, kind: str, label: str) -> None:
        super().__init__(manager, f"slot_{n}_{kind}")
        self.n = n
        self._attr_name = f"Slot {n} {label}"

    @property
    def available(self) -> bool:
        return len(self.manager.rows()) >= self.n


class Received(_SlotButton):
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, manager, n: int) -> None:
        super().__init__(manager, n, "received", "received")

    async def async_press(self) -> None:
        await self.manager.async_received(self.manager.resolve(slot=self.n))


class FollowUp(_SlotButton):
    _attr_icon = "mdi:flag-outline"

    def __init__(self, manager, n: int) -> None:
        super().__init__(manager, n, "follow_up", "follow up")

    async def async_press(self) -> None:
        self.manager.follow_up(self.manager.resolve(slot=self.n))
