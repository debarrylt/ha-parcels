"""todo.parcels: parcels in flight plus those received in the last 7 days. Ticking one off = received."""
from __future__ import annotations

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ParcelsEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([ParcelsTodo(entry.runtime_data)])


class ParcelsTodo(ParcelsEntity, TodoListEntity):
    _attr_name = None
    _attr_icon = "mdi:package-variant-closed"
    _attr_supported_features = (TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM
                                | TodoListEntityFeature.DELETE_TODO_ITEM | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
                                | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)

    def __init__(self, manager) -> None:
        super().__init__(manager, "todo")

    @property
    def todo_items(self) -> list[TodoItem]:
        items = []
        for v in self.manager.rows() + self.manager.received_recent():
            p = self.manager.data["parcels"][v["parcel_id"]]
            items.append(TodoItem(uid=v["parcel_id"], summary=v["title"], description=_description(v),
                                  status=TodoItemStatus.COMPLETED if v["received"] else TodoItemStatus.NEEDS_ACTION,
                                  due=_date(p.get("eta_date"))))
        return items

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self.manager.add_manual(item.summary or "Parcel", item.due if not hasattr(item.due, "hour") else item.due.date(),
                                _tracking_from(item.description))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        p = self.manager.resolve(item.uid)
        received = item.status == TodoItemStatus.COMPLETED
        if received != bool(p.get("received_at")):
            await self.manager.async_received(p, received)
        due = item.due.date() if hasattr(item.due, "hour") else item.due
        from .model import view
        from homeassistant.util import dt as dt_util
        generated = _description(view(p, dt_util.now().date()))
        self.manager.update_fields(p, title=item.summary if item.summary and item.summary != _title(p) else None,
                                   due=due if due and due.isoformat() != p.get("eta_date") else None,
                                   tracking=None if item.description == generated else _tracking_from(item.description))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self.manager.async_remove(self.manager.resolve(uid))


def _description(v: dict) -> str:
    desc = " · ".join(x for x in (v["status"], v["carrier"], v["tracking"]) if x)
    if v["flagged"]:
        desc = f"⚑ {v['flag_reason'] or 'Follow up'} · {desc}"
    if v["details_pending"]:
        desc += " · details pending"
    return desc


def _date(value):
    from datetime import date
    return date.fromisoformat(value) if value else None


def _title(p):
    from .model import title
    return title(p)


def _tracking_from(text: str | None) -> str | None:
    """A tracking number typed into the description when adding or editing by hand ("tracking: X" or a bare code)."""
    import re
    if not text:
        return None
    m = re.search(r"\btracking(?:\s*(?:number|no\.?))?\s*[:#]\s*([A-Z0-9][A-Z0-9-]{6,30}[A-Z0-9])\b", text, re.I) \
        or re.fullmatch(r"\s*([A-Z0-9]{8,30})\s*", text, re.I)
    return m.group(1) if m and re.search(r"\d", m.group(1)) else None
