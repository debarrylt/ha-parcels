"""text.parcels_slot_N_tracking: type the tracking number of the parcel in row N (while it has none)."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SLOTS
from .entity import ParcelsEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([SlotTracking(entry.runtime_data, n) for n in range(1, SLOTS + 1)])


class SlotTracking(ParcelsEntity, TextEntity):
    _attr_icon = "mdi:barcode-scan"
    _attr_native_min = 0
    _attr_native_max = 60

    def __init__(self, manager, n: int) -> None:
        super().__init__(manager, f"slot_{n}_tracking")
        self.n = n
        self._attr_name = f"Slot {n} tracking"

    def _row(self) -> dict | None:
        rows = self.manager.rows()
        return rows[self.n - 1] if len(rows) >= self.n else None

    @property
    def available(self) -> bool:
        row = self._row()
        return bool(row and row["needs_tracking"])

    @property
    def native_value(self) -> str:
        return ""

    async def async_set_value(self, value: str) -> None:
        # the rows may have moved since the dashboard was drawn: add_tracking refuses a parcel that already has a number
        if value.strip():
            await self.manager.async_add_tracking(self.manager.resolve(slot=self.n), value)
        self.async_write_ha_state()
