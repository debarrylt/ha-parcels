"""sensor.parcels_in_flight (count + summary) and sensor.parcels_slot_1..N (one row each, "none" when empty)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SLOTS
from .entity import ParcelsEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    m = entry.runtime_data
    add([InFlight(m)] + [Slot(m, n) for n in range(1, SLOTS + 1)])


class InFlight(ParcelsEntity, SensorEntity):
    _attr_name = "In flight"
    _attr_icon = "mdi:truck-delivery-outline"
    _attr_native_unit_of_measurement = "parcels"

    def __init__(self, manager) -> None:
        super().__init__(manager, "in_flight")

    @property
    def native_value(self) -> int:
        return len(self.manager.rows())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.manager.summary()


class Slot(ParcelsEntity, SensorEntity):
    _attr_icon = "mdi:package-variant"
    _unrecorded_attributes = frozenset({"url", "order_url", "carrier_url", "shop_url", "t17_url", "emails"})

    def __init__(self, manager, n: int) -> None:
        super().__init__(manager, f"slot_{n}")
        self.n = n
        self._attr_name = f"Slot {n}"

    def _row(self) -> dict | None:
        rows = self.manager.rows()
        return rows[self.n - 1] if len(rows) >= self.n else None

    @property
    def native_value(self) -> str:
        row = self._row()
        return row["title"][:250] if row else "none"

    @property
    def icon(self) -> str:
        row = self._row()
        if not row:
            return "mdi:package-variant"
        if row["flagged"]:
            return "mdi:flag"
        if row["delivered"]:
            return "mdi:package-variant-closed-check"
        return "mdi:satellite-variant" if row["live"] else "mdi:email-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._row() or {}
