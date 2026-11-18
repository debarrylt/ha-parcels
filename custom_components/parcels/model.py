"""Parcel records and reconciliation (no Home Assistant imports: unit-tested on its own).

The store is a plain dict (JSON-serialisable) so Home Assistant's Store can persist it:
  {"parcels": {id: Parcel-dict}, "emails": {message_key: {...}}, "last_uid": int}

Rules (docs/plans/package_tracking.md):
- An update matches a parcel by tracking number, then by merchant + order number; otherwise it creates a parcel and,
  if a recent parcel from the same merchant had nothing to match on, records it as a possible duplicate (never merged
  silently).
- Status only moves forward by event time (email Date / carrier scan time); problems override when newer; delivered
  sticks until the user confirms reception.
- Each email is applied once per stage ("regex", "llm"), keyed by Message-ID.
"""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .parser import MISDELIVERED, STATUS_RANK, Extraction, carrier_page, normalise_tracking, order_page, rules, tracking_url

KEEP_RECEIVED = timedelta(days=7)
KEEP_ARCHIVE = timedelta(days=120)    # compact record of delivered parcels, for clients catching up (parcels.delivered)
LATE_AFTER = timedelta(days=1)
NO_MOVEMENT = timedelta(days=3)
NO_MOVEMENT_REASON = "No carrier update for 3 days"
DUPLICATE_WINDOW = timedelta(days=14)
LONG_ORDER = 10        # an order number this long identifies the order whatever the merchant name
STATUS_TEXT = {"ordered": "Ordered", "shipped": "Shipping soon", "in_transit": "In transit",
               "out_for_delivery": "Out for delivery", "pickup": "Ready to collect", "delivered": "Delivered, confirm?"}
# 17TRACK package status -> (status, problem)
T17_STATUS = {"not found": (None, None), "in transit": ("in_transit", None), "pick up": ("pickup", None),
              "ready to be picked up": ("pickup", None), "delivered": ("delivered", None),
              "undelivered": (None, "Delivery failed"), "alert": (None, "Carrier alert"),
              "expired": (None, "Tracking expired"), "returned": (None, "Returned to sender")}


def _iso(d: datetime | date | None) -> str | None:
    return d.isoformat() if d else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def new_store() -> dict[str, Any]:
    return {"parcels": {}, "emails": {}, "last_uid": 0}


def new_parcel(now: datetime) -> dict[str, Any]:
    return {"id": secrets.token_hex(4), "merchant": None, "item": None, "title": None, "carrier": None, "tracking": [],
            "order": None, "status": None, "status_at": None, "status_source": None, "eta_date": None,
            "eta_window": None, "eta_at": None, "problem": None, "problem_at": None, "flagged": False,
            "flag_reason": None, "flag_cleared_at": None, "created": _iso(now), "updated": _iso(now), "emails": [],
            "details_pending": False, "t17": {}, "delivered_at": None, "received_at": None,
            "possible_duplicate_of": None, "manual": False, "order_url": None, "carrier_url": None, "lines": [],
            "lines_checked": True}


def merge_lines(have: list[dict], new: list[dict]) -> list[dict]:
    """Union of order lines. The same line seen in several emails (confirmation, shipped, delivered) is ONE line: it
    keeps its highest quantity, quantities are never added up."""
    out: dict[tuple, dict] = {}
    for x in [*(have or []), *(new or [])]:
        n, v = norm_name(x["name"]), norm_name(x.get("variant"))
        # one email may add trailing text to the variant ("... / 1 kg Filament Bulk Sale -£4.25"): same line
        k = next((k for k in out if k[0] == n and k[1] and v and (v.startswith(k[1]) or k[1].startswith(v))), (n, v))
        if k not in out or x.get("qty", 1) > out[k].get("qty", 1):
            out.pop(k, None)
            out[(n, v)] = dict(x)
    return list(out.values())


def title(p: dict[str, Any]) -> str:
    if p.get("title"):
        return p["title"]
    merchant = p.get("merchant") or (rules().carriers[p["carrier"]][0] + " parcel" if p.get("carrier") in rules().carriers else "Parcel")
    return f"{merchant}: {p['item']}" if p.get("item") else merchant


def active(p: dict[str, Any]) -> bool:
    return not p.get("received_at") and not p.get("merged_into")


def same_tracking(a: str, b: str) -> bool:
    """Equal, or a long number with its first character dropped: carrier staff retype numbers (InPost's back office
    wrote JD0000000000000042 for JJD0000000000000042, 2026-11-09)."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 12 and len(long_) == len(short) + 1 and long_[1:] == short


def find_parcel(store: dict[str, Any], tracking: list[str], merchant: str | None, order: str | None) -> dict | None:
    parcels = [p for p in store["parcels"].values() if not p.get("merged_into")]
    for t in tracking:
        for p in parcels:
            if t in p["tracking"]:
                return p
    for t in tracking:
        for p in parcels:
            if any(same_tracking(t, x) for x in p["tracking"]):
                return p
    if order and (merchant or len(order) >= LONG_ORDER):
        for p in parcels:
            # a long order number is unique on its own: the merchant name may differ between emails (Bambu's own
            # "Bambulab" vs the shipper "Etarget Limited" Royal Mail names)
            if p.get("order") == order and (len(order) >= LONG_ORDER or norm_name(p.get("merchant")) == norm_name(merchant)):
                # another shipment of the same order: only reuse a parcel that has no (other) tracking number yet
                if not tracking or not p["tracking"]:
                    return p
    return None


def merge_order_twins(store: dict[str, Any], now: datetime) -> list[tuple[str, str]]:
    """Join a parcel made only of emails (no tracking number) into the parcel of the same long order number. Repairs
    twins made before find_parcel matched long order numbers without the merchant. Returns (source, target) ids."""
    act = [p for p in store["parcels"].values() if active(p) and len(p.get("order") or "") >= LONG_ORDER]
    out = []
    for src in [p for p in act if not p["tracking"]]:
        dst = next((p for p in act if p is not src and p["order"] == src["order"] and p["tracking"]
                    and not p.get("merged_into")), None)
        if dst and not src.get("merged_into"):
            merge(store, src["id"], dst["id"], now, "order")
            out.append((src["id"], dst["id"]))
    return out


def _possible_duplicate(store: dict[str, Any], merchant: str | None, now: datetime, exclude: str) -> str | None:
    if not merchant:
        return None
    for p in store["parcels"].values():
        if p["id"] != exclude and active(p) and norm_name(p.get("merchant")) == norm_name(merchant) \
                and _dt(p["created"]) > now - DUPLICATE_WINDOW and (not p["tracking"] or not p.get("order")):
            return p["id"]
    return None


def set_status(p: dict[str, Any], status: str | None, at: datetime, source: str) -> str | None:
    """Forward-only status. Returns the new status when it changed."""
    if not status or status not in STATUS_RANK:
        return None
    cur = p.get("status")
    if cur == "delivered":
        return None
    if status == "delivered" and p.get("reopened_at") and at <= _dt(p["reopened_at"]):
        return None                                # the delivery that a later email contradicted (see reopen)
    if cur is None or STATUS_RANK[status] > STATUS_RANK[cur]:
        p.update(status=status, status_at=_iso(at), status_source=source)
        if status == "delivered":
            p["delivered_at"] = _iso(at)
            p["problem"] = None
        return status
    return None


REOPEN_ON = ("out_for_delivery", "pickup")     # carrier news only: a late "shipped" mail must not undo a delivery


def reopen(p: dict[str, Any], status: str | None, at: datetime) -> bool:
    """An email sent AFTER the delivery says the parcel is out for delivery / ready to collect again (DHL, Nov 2026:
    "delivered" on the 6th, "with your driver" on the 9th, "delivered" on the 10th). The later news wins and the parcel
    is flagged so the user checks what happened; the next "delivered" counts with its own date. A parcel the user
    already marked received is only flagged. True when the parcel was flagged."""
    if p.get("status") != "delivered" or status not in REOPEN_ON:
        return False
    delivered = _dt(p.get("delivered_at") or p.get("status_at"))
    if not delivered or at <= delivered:
        return False
    if p.get("flagged") and p.get("reopened_at") and _dt(p["reopened_at"]) >= at:
        return False
    reason = "Delivered " + delivered.strftime("%a %d %b") + ", then " + STATUS_TEXT[status].lower() + " again"
    if not p.get("received_at"):
        p.update(status=status, status_at=_iso(at), status_source="email", delivered_at=None, reopened_at=_iso(at))
    p.update(flagged=True, flag_reason=reason[:60])
    return True


def rebuild_status(p: dict[str, Any], extractions: list[Extraction]) -> bool:
    """Undo a "delivered" that a misread email set, by replaying its emails. False if one of them does say delivered."""
    if any(ex.status == "delivered" for ex in extractions):
        return False
    p.update(status=None, status_at=None, status_source=None, delivered_at=None, eta_date=None, eta_window=None, eta_at=None)
    for ex in extractions:
        if ex.sent_at:
            set_status(p, ex.status, ex.sent_at, "email")
            set_eta(p, ex.eta_date, ex.eta_window, ex.sent_at)
    return True


def restore_carrier_delivery(p: dict[str, Any]) -> bool:
    """Put back a delivery 17TRACK reports but the parcel lost. Silent: it's a correction, not news."""
    t17 = p.get("t17") or {}
    if p.get("status") == "delivered" or (t17.get("status") or "").lower() != "delivered" or not t17.get("at"):
        return False
    return set_status(p, "delivered", _dt(t17["at"]), "17track") == "delivered"


def set_problem(p: dict[str, Any], problem: str | None, at: datetime) -> bool:
    # after "delivered" only a misdelivery counts (lost / left at the wrong place: that is when it is reported)
    if not problem or (p.get("status") == "delivered" and problem != MISDELIVERED):
        return False
    if p.get("problem_at") and _dt(p["problem_at"]) >= at:
        return False
    p.update(problem=problem, problem_at=_iso(at))
    return True


def set_eta(p: dict[str, Any], eta: date | None, window: str | None, at: datetime) -> None:
    if not eta:
        return
    if p.get("eta_at") and _dt(p["eta_at"]) > at:
        return
    p.update(eta_date=eta.isoformat(), eta_window=window, eta_at=_iso(at))


NOTE_STATUS = {"ordered": "Ordered", "shipped": "Shipping soon", "in_transit": "In transit",
               "out_for_delivery": "Out for delivery", "pickup": "Ready to collect", "delivered": "Delivered"}


def email_note(ex: Extraction) -> str | None:
    """What one email said, for the parcel's email list: "Delayed · new date Fri 20 Nov", "In transit · due Thu 19 Nov
    09:40–11:40". None when the email gave neither a status, a problem nor a date."""
    parts = [ex.problem or NOTE_STATUS.get(ex.status or "")]     # a problem says more than "In transit"
    parts = [x for x in parts if x]
    if ex.eta_date:
        when = ex.eta_date.strftime("%a %-d %b") + (f" {ex.eta_window}" if ex.eta_window else "")
        parts.append(("new date " if ex.problem == "Delayed" else "due ") + when)
    return " · ".join(parts) or None


def apply_extraction(store: dict[str, Any], ex: Extraction, email: dict[str, Any], now: datetime,
                     stage: str) -> tuple[dict | None, list[tuple[str, dict]]]:
    """Merge one email's extraction into the store. Returns (parcel, events). `email` = {key, uid, date, subject}."""
    events: list[tuple[str, dict]] = []
    if not ex.is_shipping:
        return None, events
    tracking = [normalise_tracking(t) for t in ex.tracking]
    p = find_parcel(store, tracking, ex.merchant, ex.order)
    created = p is None
    if created:
        p = new_parcel(now)
        store["parcels"][p["id"]] = p
        p["possible_duplicate_of"] = None if tracking or ex.order else _possible_duplicate(store, ex.merchant, now, p["id"])
    at = ex.sent_at or now
    for t in tracking:
        if not any(same_tracking(t, x) for x in p["tracking"]):     # a mistyped copy is not a second number
            p["tracking"].append(t)
    for key in ("order_url", "carrier_url"):     # the first email to carry one wins
        if getattr(ex, key) and not p.get(key):
            p[key] = getattr(ex, key)
    for key in ("merchant", "item", "order", "carrier"):
        val = getattr(ex, key)
        # the AI may correct the rules' merchant/item, but not with a carrier's view of it (Royal Mail names the shipper,
        # "Etarget Limited", where Bambu's own emails said "Bambulab")
        if val and (not p.get(key) or (stage == "llm" and key in ("merchant", "item")
                                       and (ex.carrier in (None, "amazon") or not p.get("emails")))):
            p[key] = val
    if stage == "llm":
        p["lines"] = merge_lines(p.get("lines"), ex.lines)
    old = p.get("status")
    reopened = reopen(p, ex.status, at)
    changed = p["status"] if reopened and p["status"] != old else set_status(p, ex.status, at, "email")
    if reopened:
        events.append(("follow_up", p))
    if set_problem(p, ex.problem, at):
        events.append(("problem", p))
    set_eta(p, ex.eta_date, ex.eta_window, at)
    note = email_note(ex)
    known = next((e for e in p["emails"] if e["key"] == email["key"]), None)
    if known is None:
        p["emails"].append({**{k: email.get(k) for k in ("key", "uid", "date", "subject")}, "note": note})
    elif note:      # the AI stage, after the rules: its reading wins
        known["note"] = note
    p["updated"] = _iso(now)
    if created:
        events.insert(0, ("new", p))
    if changed in ("out_for_delivery", "delivered", "pickup") and changed != old:
        events.append((changed, p))
    return p, events


def fits_carrier(carrier: str | None, number: str) -> bool:
    """The number has the format of this carrier's tracking numbers (Evri H01..., Royal Mail ..GB). A carrier whose
    format is not known (DHL, DPD...) takes any number."""
    if not carrier:
        return False
    if carrier not in rules().tracking:
        return True
    return any(re.fullmatch(x.replace(r"\b", ""), number) for x in rules().tracking[carrier])


def main_number(p: dict[str, Any]) -> str | None:
    """The tracking number to show and link: the one 17TRACK updated last (export carrier -> last-mile carrier, the
    last-mile number takes over), else the one in the parcel carrier's format, else the first."""
    nums = p.get("tracking") or []
    chosen = (p.get("t17") or {}).get("number")
    if chosen in nums:
        return chosen
    return next((n for n in nums if fits_carrier(p.get("carrier"), n)), nums[0] if nums else None)


def t17_registered(p: dict[str, Any]) -> set[str]:
    """Numbers of this parcel already in the 17TRACK account (before Nov 2026 only the first number was added)."""
    t17 = p.get("t17") or {}
    if "numbers" in t17:
        return set(t17["numbers"])
    return {p["tracking"][0]} if t17.get("registered") and p.get("tracking") else set()


def pick_t17(p: dict[str, Any], packages: dict[str, Any]) -> tuple[str, Any] | None:
    """(number, package) of the parcel's most recently updated 17TRACK package. `packages`: number -> package with
    .status and .timestamp (datetime or None). A package 17TRACK has not found loses to one it tracks; on a tie the
    number in the parcel carrier's format wins (the last-mile one)."""
    found = [(n, packages[n]) for n in p.get("tracking") or [] if n in packages]
    if not found:
        return None

    def key(item):
        n, pk = item
        ts = pk.timestamp if isinstance(pk.timestamp, datetime) else None
        return (str(pk.status).lower() != "not found", ts.timestamp() if ts else 0, fits_carrier(p.get("carrier"), n))
    return max(found, key=key)


def apply_t17(p: dict[str, Any], status: str, info: str | None, at: datetime | None, location: str | None,
              now: datetime) -> list[tuple[str, dict]]:
    """Apply a 17TRACK package status (only for parcels it already knows by tracking number)."""
    events: list[tuple[str, dict]] = []
    at = at or now
    last = _dt(p["t17"].get("at"))
    if last and at <= last and p["t17"].get("status") == status:
        return events
    p["t17"].update(status=status, info=info, location=location or None, at=_iso(at), seen=_iso(now))
    new_status, problem = T17_STATUS.get((status or "").lower(), (None, None))
    old = p.get("status")
    changed = set_status(p, new_status, at, "17track")
    if set_problem(p, problem, at):
        events.append(("problem", p))
    if changed in ("out_for_delivery", "delivered", "pickup") and changed != old:
        events.append((changed, p))
    p["updated"] = _iso(now)
    return events


def last_movement(p: dict[str, Any]) -> datetime:
    """Newest carrier news: the 17TRACK event time or the newest email."""
    times = [_dt(p["t17"].get("at"))] + [_dt(e.get("date")) for e in p.get("emails") or []]
    return max((t for t in times if t), default=datetime.min.replace(tzinfo=timezone.utc))


def check_follow_up(p: dict[str, Any], today: date, now: datetime) -> bool:
    """Auto-flag late, problem, or (live) no-movement parcels. Returns True when the flag was raised now."""
    # the no-movement flag goes away by itself once the carrier moves again (17TRACK event or carrier email)
    if p.get("flagged") and p.get("flag_reason") == NO_MOVEMENT_REASON and now - last_movement(p) <= NO_MOVEMENT:
        p.update(flagged=False, flag_reason=None)
    # a delivered parcel only for a misdelivery ("delivered to the wrong place", "can't find my parcel")
    if not active(p) or p.get("flagged") or (p.get("status") == "delivered" and p.get("problem") != MISDELIVERED):
        return False
    cleared = _dt(p.get("flag_cleared_at"))
    reason = None
    if p.get("problem") and (not cleared or _dt(p["problem_at"]) > cleared):
        reason = p["problem"]
    elif p.get("eta_date") and date.fromisoformat(p["eta_date"]) + LATE_AFTER < today \
            and (not cleared or cleared.date() < date.fromisoformat(p["eta_date"]) + LATE_AFTER):
        reason = "Late: expected " + date.fromisoformat(p["eta_date"]).strftime("%a %d %b")
    elif p["t17"].get("at") and p.get("status") == "in_transit" and now - _dt(p["t17"]["at"]) > NO_MOVEMENT \
            and (not cleared or cleared < _dt(p["t17"]["at"]) + NO_MOVEMENT):
        reason = NO_MOVEMENT_REASON
    if reason:
        p.update(flagged=True, flag_reason=reason)
        return True
    return False


def purge(store: dict[str, Any], now: datetime) -> list[dict]:
    """Drop parcels received more than KEEP_RECEIVED ago. Returns the removed parcels."""
    gone = [p for p in store["parcels"].values()
            if (p.get("received_at") and now - _dt(p["received_at"]) > KEEP_RECEIVED)
            or (p.get("merged_into") and now - _dt(p["updated"]) > KEEP_RECEIVED)]
    arch = store.setdefault("archive", {})
    for p in gone:
        del store["parcels"][p["id"]]
        if not p.get("merged_into") and (p.get("delivered_at") or p.get("received_at")):
            arch[p["id"]] = delivery_record(p)
    for k in [k for k, r in arch.items() if now - _dt(r["at"]) > KEEP_ARCHIVE]:
        del arch[k]
    return gone


def delivery_record(p: dict[str, Any]) -> dict[str, Any]:
    """What a client (e.g. a stock-keeping client) needs about a delivered parcel. Stable contract:
    id (stable, and `aliases` lists parcels merged into it), order (merchant order number or None), at (delivered,
    else received), lines [{name, variant, qty}], details_pending (the AI has not read every email yet: lines may grow)."""
    return {"id": p["id"], "aliases": list(p.get("aliases", [])), "merchant": p.get("merchant"), "item": p.get("item"),
            "title": title(p), "order": p.get("order"), "at": p.get("delivered_at") or p.get("received_at"),
            "delivered_at": p.get("delivered_at"), "received_at": p.get("received_at"), "lines": list(p.get("lines") or []),
            "details_pending": bool(p.get("details_pending")) or not p.get("lines_checked", False)}


def delivered_since(store: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
    """Delivered or received parcels (live + archive) since `since`, oldest first. Merged parcels are left out (their
    lines are in the parcel they were merged into)."""
    out = {k: r for k, r in store.get("archive", {}).items()}
    for p in store["parcels"].values():
        if not p.get("merged_into") and (p.get("delivered_at") or p.get("received_at")):
            out[p["id"]] = delivery_record(p)
    return sorted((r for r in out.values() if _dt(r["at"]) >= since), key=lambda r: r["at"])


MERGE_HOW = {"last_mile": "same parcel on Ship24", "order": "same order number", "manual": "merged by hand"}


def merge_record(src: dict[str, Any], at: datetime, how: str) -> dict[str, Any]:
    """What the remaining parcel keeps about a parcel merged into it (shown on its card and detail page)."""
    return {"id": src["id"], "title": title(src), "tracking": list(src.get("tracking") or []),
            "carrier": rules().carriers[src["carrier"]][0] if src.get("carrier") in rules().carriers else None,
            "at": _iso(at), "how": how}


def backfill_merges(store: dict[str, Any]) -> int:
    """Merge records for parcels merged before they were kept: rebuilt from the merged parcel, still in the store."""
    n = 0
    for p in store["parcels"].values():
        for k in ("detail_at", "detail_ok_at"):         # left by the 17TRACK detail lookups (removed 2026-11-11)
            (p.get("t17") or {}).pop(k, None)
        have = {m["id"] for m in p.get("merges") or []}
        for sid in p.get("aliases") or []:
            src = store["parcels"].get(sid)
            if sid in have or not src or src.get("merged_into") != p["id"]:
                continue
            how = ("order" if not src["tracking"] and src.get("order") and src.get("order") == p.get("order")
                   else "last_mile" if src["tracking"] and src.get("carrier") and not src.get("merchant") else "manual")
            p.setdefault("merges", []).append(merge_record(src, _dt(src.get("updated")) or _dt(p["updated"]), how))
            n += 1
    return n


def merge(store: dict[str, Any], source_id: str, target_id: str, now: datetime, how: str = "manual") -> dict:
    src, dst = store["parcels"][source_id], store["parcels"][target_id]
    dst.setdefault("merges", []).append(merge_record(src, now, how))
    dst["merges"] += [m for m in src.get("merges") or [] if m["id"] not in {x["id"] for x in dst["merges"]}]
    for t in src["tracking"]:
        if not any(same_tracking(t, x) for x in dst["tracking"]):
            dst["tracking"].append(t)
    for key in ("merchant", "item", "order", "carrier", "eta_date", "eta_window", "order_url", "carrier_url"):
        if not dst.get(key) and src.get(key):
            dst[key] = src[key]
    if src.get("status"):
        set_status(dst, src["status"], _dt(src["status_at"]) or now, src.get("status_source") or "email")
    dst["emails"] += [e for e in src["emails"] if e not in dst["emails"]]
    dst["lines"] = merge_lines(dst.get("lines"), src.get("lines"))
    dst["aliases"] = sorted({*dst.get("aliases", []), src["id"], *src.get("aliases", [])})
    dst["possible_duplicate_of"] = None
    src.update(merged_into=target_id, updated=_iso(now))
    dst["updated"] = _iso(now)
    return dst


def is_export_number(number: str) -> bool:
    """Not a number of a UK carrier we know (Royal Mail, Evri, InPost, Amazon...): an export carrier's (4PX...)."""
    pats = [p for k, ps in rules().tracking.items() if k is not None for p in ps]
    return not any(re.fullmatch(p.replace(r"\b", ""), number) for p in pats)


ORPHAN_DAYS = timedelta(days=21)      # a shop-less carrier parcel is looked into for this long
EXPORT_BEFORE = timedelta(days=30)    # the export parcel shipped at most this long before the carrier's first email


def first_seen(p: dict[str, Any]) -> datetime:
    """When the parcel's first email was sent (a forward made days later keeps the original date), else created."""
    dates = [d for d in (_dt(e.get("date")) for e in p.get("emails") or []) if d]
    return min(dates) if dates else _dt(p["created"])


def orphans(store: dict[str, Any], now: datetime) -> list[dict]:
    """Carrier parcels whose emails name no shop and no order (Evri: "the sender has your parcel"), not delivered, seen
    in the last ORPHAN_DAYS: probably the last leg of a parcel the shop shipped with an export carrier."""
    return [p for p in store["parcels"].values() if active(p) and p["tracking"] and p.get("carrier") in rules().carriers
            and p["carrier"] != "amazon" and not p.get("merchant") and not p.get("order")
            and p.get("status") not in ("delivered", "pickup") and now - first_seen(p) <= ORPHAN_DAYS]


def export_candidates(store: dict[str, Any], now: datetime) -> list[dict]:
    """Export parcels (number of no UK carrier, no carrier of ours, not delivered) that may be an orphan's first leg:
    shipped before the orphan's first email and at most EXPORT_BEFORE earlier. Only these are looked up on Ship24
    (free plan: 10 parcels a month), and only while such an orphan exists."""
    out: list[dict] = []
    for o in orphans(store, now):
        seen = first_seen(o)
        for p in store["parcels"].values():
            if (p is not o and p not in out and active(p) and p["tracking"] and not p.get("carrier")
                    and p.get("status") not in ("delivered", "pickup") and is_export_number(p["tracking"][0])
                    and seen - EXPORT_BEFORE <= first_seen(p) <= seen + timedelta(days=1)):
                out.append(p)
    return out


def last_mile_links(store: dict[str, Any], rows: list[dict]) -> list[tuple[str, str]]:
    """(source id, target id) pairs: one parcel's tracking number appears in another parcel's Ship24 record.

    When an export carrier hands a parcel to a local one (Nov 2026: 4PX -> Evri), Ship24 links both numbers on the
    shipment. `rows` = [{"number": looked-up number, "detail": raw answer}]; the answer is searched whole for the other
    parcels' numbers (10+ characters, exact), so its field names don't matter. The parcel with the shop's details
    (merchant / order) is kept and the other merged into it."""
    act = [p for p in store["parcels"].values() if active(p) and p["tracking"]]
    out: list[tuple[str, str]] = []
    for row in rows:
        own = normalise_tracking(str(row.get("number") or ""))
        a = next((p for p in act if own and own in p["tracking"]), None)
        if not a:
            continue
        hay = " ".join(str(v) for k, v in row.items() if k != "number").upper()
        for b in act:
            if b is a or any(b["id"] in pair for pair in out):
                continue
            if any(len(t) >= 10 and re.search(r"(?<![A-Z0-9])" + re.escape(t) + r"(?![A-Z0-9])", hay) for t in b["tracking"]):
                keep_a = bool(a.get("merchant") or a.get("order")) or not (b.get("merchant") or b.get("order"))
                out.append((b["id"], a["id"]) if keep_a else (a["id"], b["id"]))
    return out


def status_rank(p: dict[str, Any], today: date | None = None) -> int:
    """Follow up, delivered (to confirm), out for delivery, ready to collect, in transit, shipping soon, ordered."""
    if p.get("flagged"):
        return 0
    if p.get("status") == "delivered" or p.get("received_at"):
        return 1
    if today and out_today(p, today):
        return 2
    return {"pickup": 3, "in_transit": 4, "shipped": 5}.get(p.get("status") or "", 6)


def sort_key(p: dict[str, Any], today: date | None = None) -> tuple:
    """By status (status_rank), then by ETA (unknown last), then newest."""
    return (status_rank(p, today), p.get("eta_date") or "9999", -(_dt(p["created"]).timestamp()))


def eta_text(p: dict[str, Any], today: date) -> str:
    if p.get("status") == "delivered":
        d = _dt(p.get("delivered_at"))
        return "Delivered " + (_day(d.date(), today) if d else "")
    if not p.get("eta_date"):
        return "No date yet"
    text = _day(date.fromisoformat(p["eta_date"]), today)
    return f"{text} {p['eta_window']}" if p.get("eta_window") else text


def _day(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    if 1 < delta < 7:
        return d.strftime("%a")
    return d.strftime("%a %d %b")


SENDS_OUT_FOR_DELIVERY = {"amazon"}   # carriers whose "out for delivery" email always comes on the day


def out_today(p: dict[str, Any], today: date) -> bool:
    """Out for delivery: the carrier said so, or the parcel is with the carrier and due today. Royal Mail sends the
    date and time slot the day before and often no "out for delivery" email on the day. A label that is only created
    ("shipped") counts only when the email gave a time slot, which comes from the carrier. Carriers that always email
    on the day (Amazon) are never inferred: their "Dispatched ... arriving today" is not out for delivery (Nov 2026)."""
    if p.get("received_at") or p.get("problem"):
        return p.get("status") == "out_for_delivery" and not p.get("received_at")
    if p.get("status") == "out_for_delivery":
        return True
    if p.get("carrier") in SENDS_OUT_FOR_DELIVERY:
        return False
    return (p.get("eta_date") == today.isoformat()
            and (p.get("status") == "in_transit" or (p.get("status") == "shipped" and bool(p.get("eta_window")))))


def status_text(p: dict[str, Any], today: date | None = None) -> str:
    if p.get("received_at"):
        return "Received"
    if p.get("problem") and p.get("status") != "delivered":
        return p["problem"]
    if today and out_today(p, today):
        return STATUS_TEXT["out_for_delivery"]
    return STATUS_TEXT.get(p.get("status") or "", "Waiting for news")


def view(p: dict[str, Any], today: date) -> dict[str, Any]:
    """Flat dict for sensors, dashboards."""
    carrier = rules().carriers[p["carrier"]][0] if p.get("carrier") in rules().carriers else None
    live = bool(p["t17"].get("status")) and p["t17"]["status"].lower() != "not found"
    number = main_number(p)
    # the carrier's code and page only for a number in its format: a merged parcel's export number (4PX) is not Evri's
    own = p.get("carrier") if p.get("carrier") == "amazon" or (number and fits_carrier(p.get("carrier"), number)) else None
    amazon = p.get("carrier") == "amazon" or (number or "").startswith("TBA")
    return {"parcel_id": p["id"], "title": title(p), "eta": eta_text(p, today), "status": status_text(p, today),
            "carrier": carrier, "tracking": number, "live": live,
            "last_event": (p["t17"].get("info") or "")[:120] if live else None,
            "updated": p.get("t17", {}).get("at") if live else (p["emails"][-1]["date"] if p["emails"] else p["updated"]),
            "url": tracking_url(own, number) if number else None,
            "order": p.get("order"), "merchant": p.get("merchant"), "location": p["t17"].get("location") if live else None,
            "order_url": p.get("order_url") or order_page(p.get("merchant") or ("Amazon" if amazon else None), p.get("order")),
            # a built address first: the email's link is usually a mail-tracking redirect, which hides the destination
            "carrier_url": carrier_page(own, number) or (p.get("carrier_url") if own == p.get("carrier") else None),
            "t17_url": tracking_url(own, number) if number and not amazon else None,
            "emails": [{"date": e.get("date"), "subject": (e.get("subject") or "")[:90], "note": e.get("note")}
                       for e in p["emails"][-6:]],
            "flagged": bool(p.get("flagged")), "flag_reason": p.get("flag_reason"),
            "tracking_numbers": list(p["tracking"]),
            # parcels merged into this one: "Evri parcel (H09XYZ0000000042) · 11 Nov · same parcel on Ship24"
            "merged": [" · ".join(x for x in (m["title"] + (f" ({m['tracking'][0]})" if m.get("tracking") else ""),
                                              _dt(m["at"]).strftime("%d %b"), MERGE_HOW.get(m.get("how"), m.get("how")))
                                  if x) for m in p.get("merges") or []],
            # for the card's status line, for two days after a merge
            "merged_recent": next((f"Merged with {m['title']}" + (f" ({m['tracking'][0]})" if m.get("tracking") else "")
                                   for m in reversed(p.get("merges") or []) if (today - _dt(m["at"]).date()).days < 2), None),
            "delivered": p.get("status") == "delivered", "received": bool(p.get("received_at")),
            "details_pending": bool(p.get("details_pending")), "possible_duplicate_of": p.get("possible_duplicate_of"),
            "ship_to": p.get("ship_to"), "elsewhere": bool(p.get("elsewhere")),
            "line": " · ".join(x for x in (eta_text(p, today), carrier, status_text(p, today),
                                           f"to {p.get('ship_to')}" if p.get("elsewhere") else None) if x)}
