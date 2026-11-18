"""Unit tests for the parcels parser and reconciliation (pure Python; no Home Assistant needed).

Run: .venv/bin/python -m pytest -q
"""
import asyncio
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "parcels"
if "parcels_core" not in sys.modules:      # normally set up by conftest.py
    pkg = types.ModuleType("parcels_core")
    pkg.__path__ = [str(PKG)]          # import parser/model without running the HA-dependent __init__
    sys.modules["parcels_core"] = pkg
from parcels_core import model as M  # noqa: E402
from parcels_core import mailbox as MB  # noqa: E402
from parcels_core import parser as P  # noqa: E402

UTC = timezone.utc
EVRI = """---------- Forwarded message ---------
From: Evri <do-not-reply@evri.com>
Date: Thu, Nov 5, 2026 at 2:27 PM
Subject: We're expecting your parcel
To: <someone@example.com>
We're waiting for your parcel
The sender has your parcel. We expect to receive it within 48 hours.
Tracking number
H09XYZ0000000042
"""
DHL = """From: <notifications@dhlecommerce.co.uk>
WE HAVE YOUR PARCEL
We have your parcel from Spacex and will deliver to you on Friday, 6
November.
Shipment number: 60129999000001
"""
INPOST = """We'll deliver your parcel from Ali Express today
*FROM:* Ali Express
*PARCEL NO.* JJD0000000000000042
Arriving today between:
* 09:40-11:40 *
"""


def t(day, hour=12):
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


def test_forwarded_evri():
    ex = P.extract("Fwd: We're expecting your parcel", "me@example.com", EVRI, t(5, 14))
    assert ex.carrier == "evri" and ex.sender == "do-not-reply@evri.com"
    assert ex.tracking == ["H09XYZ0000000042"] and ex.status == "shipped" and ex.is_shipping


def test_dhl_date_and_merchant():
    ex = P.extract("We have your parcel from Spacex", "notifications@dhlecommerce.co.uk", DHL, t(5, 19))
    assert ex.carrier == "dhl_uk" and ex.tracking == ["60129999000001"]
    assert ex.merchant == "Spacex" and ex.status == "in_transit" and ex.eta_date == date(2026, 11, 6)


def test_inpost_window_and_aliexpress():
    ex = P.extract("Your parcel's out for delivery between 09:40-11:40", "InPost <inpost@inpost.co.uk>", INPOST, t(5, 10))
    assert ex.carrier == "inpost" and ex.tracking == ["JJD0000000000000042"] and ex.merchant == "AliExpress"
    assert ex.status == "out_for_delivery" and ex.eta_date == date(2026, 11, 5) and ex.eta_window == "09:40–11:40"


def test_forward_uses_the_original_date():
    fwd = ("---------- Forwarded message ---------\r\nFrom: InPost <inpost@inpost.co.uk>\r\n"
           "Date: Thu, Nov 5, 2026 at 10:15\u202fAM\r\nSubject: Your parcel's out for delivery\r\nTo: <a@example.com>\r\n\r\n" + INPOST)
    ex = P.extract("Fwd: Your parcel's out for delivery", "me@example.com", fwd, t(6, 11))
    assert ex.sent_at == datetime(2026, 11, 5, 10, 15, tzinfo=UTC) and ex.eta_date == date(2026, 11, 5)
    assert ex.eta_window == "09:40–11:40"
    # other formats; a nonsense or future date falls back on the forward's own date
    for raw in ("Thu, 5 Nov 2026 at 10:15", "Thu, 5 Nov 2026 10:15:00 +0000", "Thursday, November 5, 2026 10:15 AM"):
        got = P._original_date(f"--- Forwarded message ---\nFrom: x@y.z\nDate: {raw}\n", t(6, 11))
        assert got == datetime(2026, 11, 5, 10, 15, tzinfo=UTC), raw
    for raw in ("whenever", "Sat, Nov 14, 2026 at 10:15 AM"):
        assert P._original_date(f"--- Forwarded message ---\nDate: {raw}\n", t(6, 11)) == t(6, 11), raw
    # not a forward: the header date stands, even with a "Date:" line in the body
    assert P._original_date("Date: Thu, Nov 5, 2026 at 10:15 AM", t(6, 11)) == t(6, 11)


def test_non_shipping_ignored():
    ex = P.extract("Security alert", "no-reply@accounts.google.com", "A new sign-in on your account.", t(5))
    assert not ex.is_shipping


def test_problem_and_delivered():
    ex = P.extract("Sorry we missed you", "do-not-reply@evri.com", "We couldn't deliver your parcel H09XYZ0000000042 today.", t(6))
    assert ex.problem == "Delivery failed"
    ex = P.extract("Your parcel has been delivered", "do-not-reply@evri.com", "Tracking number H09XYZ0000000042", t(6))
    assert ex.status == "delivered"


def _apply(store, subject, sender, text, at, key, stage="regex"):
    ex = P.extract(subject, sender, text, at)
    return M.apply_extraction(store, ex, {"key": key, "uid": 1, "date": at.isoformat(), "subject": subject}, at, stage)


def test_same_tracking_updates_one_parcel_forward_only():
    store = M.new_store()
    p1, ev = _apply(store, "Your parcel's out for delivery", "inpost@inpost.co.uk", INPOST, t(5, 10), "a")
    assert [e[0] for e in ev] == ["new", "out_for_delivery"]
    # an older "shipped" email processed late must not move the status back
    p2, _ = _apply(store, "Your order has shipped", "inpost@inpost.co.uk", "PARCEL NO. JJD0000000000000042", t(4), "b")
    assert p1 is p2 and len(store["parcels"]) == 1 and p1["status"] == "out_for_delivery"
    p3, ev = _apply(store, "Your parcel has been delivered", "inpost@inpost.co.uk", "PARCEL NO. JJD0000000000000042", t(5, 11), "c")
    assert p3["status"] == "delivered" and ("delivered", p3) in ev
    # an "out for delivery" sent before the delivery (processed late) can't undo it; one sent after reopens it,
    # flagged (test_later_carrier_news_reopens_a_delivery_and_flags_it)
    _apply(store, "Out for delivery", "inpost@inpost.co.uk", "PARCEL NO. JJD0000000000000042", t(5, 10), "d")
    assert p3["status"] == "delivered" and not p3["flagged"]


def test_order_then_tracking_and_multi_shipment():
    store = M.new_store()
    a, _ = _apply(store, "Thanks for your order 123-4567890", "orders@shop.example", "Order number: 123-4567890", t(1), "o1")
    a["merchant"] = "Shop"
    b, _ = _apply(store, "Your order 123-4567890 has shipped", "orders@shop.example",
                  "Order number: 123-4567890 Tracking number: AB123456789GB", t(2), "o2")
    assert a is b and b["tracking"] == ["AB123456789GB"]
    c, _ = _apply(store, "Your order 123-4567890 has shipped", "orders@shop.example",
                  "Order number: 123-4567890 Tracking number: CD987654321GB", t(2, 13), "o3")
    assert c is not b and c["order"] == b["order"] and len(store["parcels"]) == 2


def test_possible_duplicate_never_merged_silently():
    store = M.new_store()
    a, _ = _apply(store, "Your parcel is on its way", "hello@plants.example", "Your Plants parcel is on its way", t(1), "x")
    b, _ = _apply(store, "Your parcel is on its way", "hello@plants.example", "Your Plants parcel is on its way again", t(2), "y")
    assert a is not b and b["possible_duplicate_of"] == a["id"]
    M.merge(store, b["id"], a["id"], t(2))
    assert b["merged_into"] == a["id"] and not M.active(b) and a["possible_duplicate_of"] is None


def test_t17_and_follow_up():
    store = M.new_store()
    p, _ = _apply(store, "We have your parcel from Spacex", "notifications@dhlecommerce.co.uk", DHL, t(5, 19), "k")
    ev = M.apply_t17(p, "Undelivered", "Address issue", t(6, 9), "Slough", t(6, 9))
    assert ("problem", p) in ev and p["problem"] == "Delivery failed"
    assert M.check_follow_up(p, date(2026, 11, 6), t(6, 10)) and p["flagged"] and p["flag_reason"] == "Delivery failed"
    # cleared by the user: the same problem does not re-flag; a later one does
    p.update(flagged=False, flag_cleared_at=t(6, 11).isoformat())
    assert not M.check_follow_up(p, date(2026, 11, 6), t(6, 12))
    M.apply_t17(p, "Alert", "Held", t(7, 9), None, t(7, 9))
    assert M.check_follow_up(p, date(2026, 11, 7), t(7, 10))


def test_late_flag_and_purge():
    store = M.new_store()
    p, _ = _apply(store, "We have your parcel from Spacex", "notifications@dhlecommerce.co.uk", DHL, t(5, 19), "k")
    assert not M.check_follow_up(p, date(2026, 11, 7), t(7))
    assert M.check_follow_up(p, date(2026, 11, 8), t(8)) and p["flag_reason"].startswith("Late")
    p["received_at"] = t(8).isoformat()
    assert M.view(p, date(2026, 11, 8))["status"] == "Received"
    assert M.purge(store, t(8) + timedelta(days=8)) == [p] and not store["parcels"]


def test_view_rows():
    store = M.new_store()
    p, _ = _apply(store, "Your parcel's out for delivery between 09:40-11:40", "inpost@inpost.co.uk", INPOST, t(5, 10), "a")
    v = M.view(p, date(2026, 11, 5))
    assert v["title"] == "AliExpress" and v["eta"] == "Today 09:40–11:40" and v["carrier"] == "InPost"
    assert v["status"] == "Out for delivery" and "fc=100462" in v["url"]


def test_mark_seen_stores_the_flag_by_uid(monkeypatch):
    calls = []

    class Client:
        async def uid(self, *args):
            calls.append(args)
            return types.SimpleNamespace(result="OK", lines=[])

    async def connect(_data):
        return Client()

    async def logout(_client):
        calls.append("logout")

    monkeypatch.setattr(MB, "_connect", connect)
    monkeypatch.setattr(MB, "_logout", logout)
    asyncio.run(MB.mark_seen({}, [12, 14]))
    assert calls == [("store", "12,14", "+FLAGS", "(\\Seen)"), "logout"]


# ---------------------------------------------------------------- links
SHOP_HTML = """<p>Hello</p>
<a href="https://shop.example/account/orders/123?a=1&amp;b=2"><b>View your order</b></a>
<a href="https://carrier.example/t/ABC12345678">Track your parcel</a>
<a href="https://shop.example/unsubscribe">Unsubscribe from your orders</a>
<a href="javascript:alert(1)">View order</a>
<a href="https://shop.example/help/orders">Help with my order</a>"""


def test_links_from_html():
    order, track = P.pick_links(P.find_links(SHOP_HTML, None), ["ABC12345678"])
    assert order == "https://shop.example/account/orders/123?a=1&b=2"
    assert track == "https://carrier.example/t/ABC12345678"


def test_links_from_plain_text():
    links = P.find_links(None, "Track your parcel: https://carrier.example/x.\nSee your order <https://shop.example/o/1>")
    assert P.pick_links(links, []) == ("https://shop.example/o/1", "https://carrier.example/x")


def test_links_found_by_address_when_text_says_nothing():
    links = [("https://c.example/ZX99", "Click here"), ("https://s.example/orders/5", "Click here")]
    assert P.pick_links(links, ["ZX99"]) == ("https://s.example/orders/5", "https://c.example/ZX99")


def test_footer_links_are_never_chosen():
    links = [("https://s.example/orders/5/review", "Rate your order"), ("https://s.example/contact", "Track your parcel")]
    assert P.pick_links(links, []) == (None, None)


def test_amazon_track_link_is_the_order_page():
    assert P.pick_links([("https://amazon.co.uk/x", "Track package")], [], "amazon") == ("https://amazon.co.uk/x", None)


def test_unsafe_urls_refused_and_markdown_kept_intact():
    assert P.safe_url("javascript:alert(1)") is None
    assert P.safe_url("https://a.example/x y") is None
    assert P.safe_url('https://a.example/"><script>') is None
    assert P.safe_url("https://a.example/" + "x" * 900) is None
    assert P.safe_url("https://a.example/x)[y](z") == "https://a.example/x%29[y]%28z"


def test_view_prefers_known_carrier_page_then_email_link():
    now = datetime(2026, 11, 7, tzinfo=UTC)
    p = M.new_parcel(now)
    p.update(carrier="evri", tracking=["H00AAA0000000000"], carrier_url="https://from.email/x")
    v = M.view(p, now.date())
    assert v["carrier_url"] == "https://www.evri.com/track/parcel/H00AAA0000000000/details"
    assert v["t17_url"].startswith("https://t.17track.net/") and v["order_url"] is None
    p.update(carrier="inpost", tracking=["JJD000000000000000000"])   # no known page for InPost
    assert M.view(p, now.date())["carrier_url"] == "https://from.email/x"


def test_view_amazon_has_order_page_and_no_17track():
    now = datetime(2026, 11, 7, tzinfo=UTC)
    p = M.new_parcel(now)
    p.update(carrier="amazon", merchant="Amazon", order="123-1234567-1234567", tracking=["TBA123456789012"])
    v = M.view(p, now.date())
    assert v["order_url"].endswith("orderID=123-1234567-1234567")
    assert v["t17_url"] is None and v["carrier_url"] is None


def test_first_email_link_is_kept():
    now = datetime(2026, 11, 7, tzinfo=UTC)
    store = M.new_store()
    mail = {"key": "k1", "uid": 1, "date": now.isoformat(), "subject": "s"}
    ex = P.Extraction(is_shipping=True, tracking=["ZX991234"], order_url="https://a.example/1", sent_at=now)
    p, _ = M.apply_extraction(store, ex, mail, now, "regex")
    ex2 = P.Extraction(is_shipping=True, tracking=["ZX991234"], order_url="https://a.example/2",
                       carrier_url="https://c.example/2", sent_at=now)
    p, _ = M.apply_extraction(store, ex2, {**mail, "key": "k2"}, now, "regex")
    assert p["order_url"] == "https://a.example/1" and p["carrier_url"] == "https://c.example/2"


# ---------------------------------------------------------------- Amazon's progress bar
AMAZON_ORDERED = """Your Orders

Thanks for your order!
Ordered

Dispatched

Out for delivery

Delivered


Arriving tomorrow

Sam - LONDON

Order #
111-2222222-3333333
"""
SENT = datetime(2026, 11, 7, 18, 36, tzinfo=UTC)


def test_amazon_progress_bar_is_not_a_status():
    ex = P.extract("Ordered: 'Example Lamp...'", "auto-confirm@amazon.co.uk", AMAZON_ORDERED.replace("\n", "\r\n"), SENT)
    assert ex.status == "ordered"
    assert ex.eta_date == date(2026, 11, 8)


def test_amazon_real_stages_still_come_from_the_subject():
    for subject, status in (("Delivered: 'Example Lamp...'", "delivered"), ("Out for delivery: 'Example Lamp...'", "out_for_delivery"),
                            ("Dispatched: 'Example Lamp...'", "in_transit")):
        assert P.extract(subject, "order-update@amazon.co.uk", AMAZON_ORDERED, SENT).status == status


def test_rebuild_status_undoes_a_misread_delivery():
    p = M.new_parcel(SENT)
    p.update(status="delivered", status_at=SENT.isoformat(), status_source="email", delivered_at=SENT.isoformat(), eta_date="2026-11-07")
    ex = P.Extraction(status="ordered", eta_date=date(2026, 11, 8), sent_at=SENT)
    assert M.rebuild_status(p, [ex])
    assert p["status"] == "ordered" and p["delivered_at"] is None and p["eta_date"] == "2026-11-08"


def test_rebuild_status_keeps_a_real_delivery():
    p = M.new_parcel(SENT)
    p.update(status="delivered", delivered_at=SENT.isoformat())
    assert not M.rebuild_status(p, [P.Extraction(status="ordered", sent_at=SENT), P.Extraction(status="delivered", sent_at=SENT)])
    assert p["status"] == "delivered"


def test_carrier_saying_it_has_delivered_counts():
    assert P.extract("Example Couriers has delivered your parcel", "x@dhlecommerce.co.uk", "Hello", SENT).status == "delivered"


def test_restore_carrier_delivery():
    p = M.new_parcel(SENT)
    p.update(status="in_transit", t17={"status": "Delivered", "at": SENT.isoformat()})
    assert M.restore_carrier_delivery(p)
    assert p["status"] == "delivered" and p["delivered_at"] == SENT.isoformat()
    assert not M.restore_carrier_delivery(p)


def test_restore_ignores_parcels_the_carrier_has_not_delivered():
    p = M.new_parcel(SENT)
    p.update(status="in_transit", t17={"status": "In Transit", "at": SENT.isoformat()})
    assert not M.restore_carrier_delivery(p) and p["status"] == "in_transit"


SHOP_CONFIRMED = """Order Confirmation
Hi Sam,
Thank you for your order. We're preparing it for shipment and will notify you once your items are on the way.
Order Summary
Example Filament x 1
Shipping 2-4 Business days to deliver, after order is shipped.
"""


def test_order_confirmation_is_not_in_transit():
    ex = P.extract("Your order uk123456789 is confirmed", "no-reply@shop.example", SHOP_CONFIRMED, SENT)
    assert ex.status == "ordered"
    ex = P.extract("Thanks for shopping", "no-reply@shop.example", SHOP_CONFIRMED, SENT)
    assert ex.status == "ordered"     # the body's future "on the way" and "after order is shipped" don't count


def test_past_tense_still_counts():
    assert P.extract("Update", "x@shop.example", "Your order has been shipped. It is on the way.", SENT).status == "in_transit"
    assert P.extract("Update", "x@shop.example", "Your parcel was delivered when you were out.", SENT).status == "delivered"


# --- order lines + delivered parcels (parcels.delivered, used by the 3D studio's filament stock) -------------------
BAMBU = "Order #UK123456 confirmed\nPLA Basic Refill Jade White x2\n"


def _llm_apply(store, lines, at, key, order="UK123456", status=None):
    ex = P.extract("Your Bambu Lab order", "no-reply@bambulab.com", BAMBU, at)
    ex.order, ex.merchant, ex.lines, ex.is_shipping, ex.status = order, "Bambu Lab", lines, True, status
    return M.apply_extraction(store, ex, {"key": key, "uid": 1, "date": at.isoformat(), "subject": "x"}, at, "llm")


def test_lines_from_several_emails_are_not_added_up():
    store = M.new_store()
    jade = {"name": "PLA Basic", "variant": "Jade White (10100) / Refill", "qty": 2}
    p, _ = _llm_apply(store, [jade], t(5), "k1")
    p, _ = _llm_apply(store, [dict(jade), {"name": "PLA Matte", "variant": "Charcoal", "qty": 1}], t(6), "k2",
                      status="delivered")
    p, _ = _llm_apply(store, [{**jade, "variant": "jade white (10100) / refill"}], t(6, 15), "k3")
    assert sorted((x["name"], x["qty"]) for x in p["lines"]) == [("PLA Basic", 2), ("PLA Matte", 1)]


def test_regex_stage_keeps_no_lines():
    store = M.new_store()
    ex = P.extract("Your Bambu Lab order", "no-reply@bambulab.com", BAMBU, t(5))
    ex.lines, ex.is_shipping, ex.order = [{"name": "X", "variant": None, "qty": 1}], True, "UK1"
    p, _ = M.apply_extraction(store, ex, {"key": "k", "uid": 1, "date": t(5).isoformat(), "subject": "x"}, t(5), "regex")
    assert p["lines"] == []


def test_merge_unions_lines_and_records_alias():
    store = M.new_store()
    a, _ = _llm_apply(store, [{"name": "PLA Basic", "variant": "Black", "qty": 1}], t(5), "k1", order="A1")
    b, _ = _llm_apply(store, [{"name": "PLA Basic", "variant": "Black", "qty": 1}], t(5), "k2", order="B2")
    M.merge(store, a["id"], b["id"], t(6))
    assert b["lines"] == [{"name": "PLA Basic", "variant": "Black", "qty": 1}] and b["aliases"] == [a["id"]]


def test_delivered_since_live_archive_and_merged():
    store = M.new_store()
    a, _ = _llm_apply(store, [{"name": "PLA Basic", "variant": "Black", "qty": 1}], t(5), "k1", order="A1",
                      status="delivered")
    b, _ = _llm_apply(store, [], t(5), "k2", order="B2", status="delivered")
    c, _ = _llm_apply(store, [], t(5), "k3", order="C3")                # not delivered
    M.merge(store, b["id"], a["id"], t(6))
    out = M.delivered_since(store, t(1))
    assert [r["id"] for r in out] == [a["id"]] and out[0]["aliases"] == [b["id"]] and out[0]["order"] == "A1"
    assert not out[0]["details_pending"]
    assert M.delivered_since(store, t(5) + timedelta(days=1)) == []
    a["received_at"] = t(6).isoformat()
    M.purge(store, t(6) + timedelta(days=8))                           # gone from the live list, kept in the archive
    assert a["id"] not in store["parcels"] and c["id"] in store["parcels"]
    assert [r["id"] for r in M.delivered_since(store, t(1))] == [a["id"]]
    M.purge(store, t(6) + timedelta(days=130))
    assert M.delivered_since(store, t(1)) == []


def test_old_parcel_without_lines_check_is_pending():
    store = M.new_store()
    a, _ = _llm_apply(store, [], t(5), "k1", status="delivered")
    del a["lines_checked"]                                               # recorded before lines were kept
    assert M.delivered_since(store, t(1))[0]["details_pending"]


def test_clean_lines():
    import pytest
    try:
        from parcels_core import llm as L
    except ImportError:
        pytest.skip("aiohttp not installed")
    got = L.clean_lines([{"name": " PLA  Basic ", "variant": "Black", "quantity": "3"}, {"name": ""}, "x",
                         {"name": "Hub", "variant": None, "quantity": 500}])
    assert got == [{"name": "PLA Basic", "variant": "Black", "qty": 3}, {"name": "Hub", "variant": None, "qty": 99}]


def test_lines_with_trailing_sale_text_are_the_same_line():
    a = [{"name": "PLA Silk+", "variant": "Gold (13405) / Filament with spool / 1 kg Filament Bulk Sale -£4.25 GBP", "qty": 1}]
    b = [{"name": "PLA Silk+", "variant": "Gold (13405) / Filament with spool / 1 kg", "qty": 1},
         {"name": "PLA Basic", "variant": "Red (10200) / Filament with spool / 1 kg", "qty": 1}]
    got = M.merge_lines(a, b)
    assert len(got) == 2 and sorted(x["name"] for x in got) == ["PLA Basic", "PLA Silk+"]
    assert len(M.merge_lines(b, a)) == 2
    # a refill and a spool of one colour stay two lines
    assert len(M.merge_lines([{"name": "PLA Basic", "variant": "Red / Refill", "qty": 1}],
                             [{"name": "PLA Basic", "variant": "Red / Filament with spool", "qty": 1}])) == 2


def test_clean_lines_drops_the_sender_name():
    from parcels_core import llm as L
    raw = [{"name": "Spacex", "variant": None, "quantity": 1}, {"name": "Spacex parcel", "variant": None, "quantity": 1}]
    assert L.clean_lines(raw, "Spacex") == []
    assert L.clean_lines([{"name": "Ali Express", "variant": None, "quantity": 1}], "AliExpress") == []
    assert L.clean_lines([{"name": "Starlink Mini", "variant": None, "quantity": 1}], "Spacex")[0]["name"] == "Starlink Mini"


# --- a carrier retypes the number of a lost parcel (InPost, 2026-11) ---------------------------------------------
INPOST_DELIVERED = """We've delivered your parcel
FROM: Ali Express
PARCEL NO. JJD0000000000000042
Your Ali Express parcel has been delivered to your letterbox - enjoy!
"""
INPOST_INVESTIGATING = """Hello. I'm contacting you from InPost UK regarding your parcel ID : JD0000000000000042.
Please accept our sincere apologies for the delay in receiving your parcel.
We are writing to provide an update on the ongoing investigation regarding your delivery.
"""


def test_same_tracking():
    assert M.same_tracking("JJD0000000000000042", "JD0000000000000042")
    assert M.same_tracking("JD0000000000000042", "JJD0000000000000042")
    assert not M.same_tracking("JJD0000000000000042", "JJD0000000000000043")
    assert not M.same_tracking("AB1234567", "B1234567")                 # short numbers: no guessing


def test_retyped_number_of_a_misdelivered_parcel_joins_it():
    store = M.new_store()
    p, _ = _apply(store, "Your parcel has been delivered", "inpost@inpost.co.uk", INPOST_DELIVERED, t(5, 11), "k1")
    assert p["status"] == "delivered"
    q, events = _apply(store, "JD0000000000000042", "followup@inpost.co.uk", INPOST_INVESTIGATING, t(9, 3), "k2")
    assert q is p and len(store["parcels"]) == 1
    assert p["tracking"] == ["JJD0000000000000042"]                      # the mistyped copy is not kept
    assert p["problem"] == P.MISDELIVERED and ("problem", p) in events


def test_delay_after_delivery_is_still_ignored():
    store = M.new_store()
    p, _ = _apply(store, "Your parcel has been delivered", "inpost@inpost.co.uk", INPOST_DELIVERED, t(5, 11), "k1")
    _apply(store, "Update", "inpost@inpost.co.uk", "Your parcel JJD0000000000000042 is delayed.", t(6), "k2")
    assert p["problem"] is None


def test_merge_skips_a_mistyped_number():
    store = M.new_store()
    p, _ = _apply(store, "Your parcel has been delivered", "inpost@inpost.co.uk", INPOST_DELIVERED, t(5, 11), "k1")
    q = M.new_parcel(t(9)); q["tracking"] = ["JD0000000000000042"]; store["parcels"][q["id"]] = q
    M.merge(store, q["id"], p["id"], t(9))
    assert p["tracking"] == ["JJD0000000000000042"]


def test_in_transit_and_due_today_counts_as_out_for_delivery():
    """Royal Mail gives the date and slot the day before and sends nothing on the day."""
    now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    p = {**M.new_parcel(now), "status": "in_transit", "eta_date": "2026-11-10", "eta_window": "12:10–15:10"}
    assert M.view(p, date(2026, 11, 9))["status"] == "In transit"
    assert M.view(p, date(2026, 11, 10))["status"] == "Out for delivery" and M.out_today(p, date(2026, 11, 10))
    assert not M.out_today({**p, "status": "shipped", "eta_window": None}, date(2026, 11, 10))   # label only
    assert M.out_today({**p, "status": "shipped"}, date(2026, 11, 10))                          # carrier gave a slot
    assert not M.out_today({**p, "status": "delivered"}, date(2026, 11, 10))
    assert not M.out_today({**p, "received_at": now.isoformat()}, date(2026, 11, 10))
    amazon = {**p, "carrier": "amazon", "eta_window": None}          # Amazon "Dispatched", arriving today
    assert not M.out_today(amazon, date(2026, 11, 10)) and M.view(amazon, date(2026, 11, 10))["status"] == "In transit"
    assert M.out_today({**amazon, "status": "out_for_delivery"}, date(2026, 11, 10))
    assert M.view({**p, "problem": "Delayed"}, date(2026, 11, 10))["status"] == "Delayed"


ROYAL_DUE = """Hello,
Your parcel from Example Shop is due to be delivered: Today, Wednesday, 11 November 2026
Delivery is estimated between: 12:10pm and 3:10pm*
We won't need a signature.
Track Parcel GN123456785GB
"""


def test_due_to_be_delivered_is_not_delivered():
    """Royal Mail's morning announcement (2026-11-11) said "due to be delivered today": it set two parcels to delivered."""
    ex = P.extract("Your parcel from Example Shop is due to be delivered today", "Royal Mail <no-reply@royalmail.com>",
                   ROYAL_DUE, t(11, 12))
    assert ex.status == "out_for_delivery" and ex.eta_date == date(2026, 11, 11) and ex.eta_window == "12:10–15:10"
    for text, want in (("Your parcel is expected to be delivered on Friday", "in_transit"),
                       ("Your parcel will be delivered to your safe place", None),
                       ("Your parcel was delivered to your safe place", "delivered"),
                       ("Delivered today at 12:41", "delivered")):
        assert P._find_status(text, "") == want, text


def test_misread_delivery_is_rebuilt_from_the_emails():
    now = t(11, 12)
    p = {**M.new_parcel(t(8)), "status": "delivered", "status_at": now.isoformat(), "status_source": "email",
         "delivered_at": now.isoformat()}
    exs = [P.extract("A shipment from order uk1 is on the way", "shop@example.com", "Your order is on the way.", t(8)),
           P.extract("Your parcel from Example Shop is due to be delivered today", "Royal Mail <no-reply@royalmail.com>",
                     ROYAL_DUE, now)]
    assert P.TO_BE_DELIVERED.search(ROYAL_DUE)
    assert M.rebuild_status(p, exs)
    assert p["status"] == "out_for_delivery" and p["delivered_at"] is None and p["eta_date"] == "2026-11-11"


def test_review_of_real_emails_2026_11_11():
    """Wording from the mailbox review (paraphrased): DHL's driver email, a tracking-info email, a Shopify forward."""
    assert P._find_status("Your Example parcel is with your DHL eCommerce UK driver for delivery today", "") == "out_for_delivery"
    assert P._find_status("Tracking Information for Your Shop Order #4000", "Here is the tracking information for "
                          "your order #4000: Tracking Number: 4PX3000000000000CN") == "shipped"
    fwd = ("---------- Forwarded message ---------\nFrom: Example Babies <store+123@t.shopifyemail.com>\n"
           "Date: Mon, Nov 2, 2026 at 2:31 AM\nSubject: A shipment from order #23909 is on the way\nTo: <me@example.com>\n\n"
           "Tracking number: 4PX3000000000042CN\n")
    ex = P.extract("Fwd: A shipment from order #23909 is on the way", "me@example.com", fwd, t(10))
    assert ex.merchant == "Example Babies" and ex.sender == "store+123@t.shopifyemail.com"


def test_carrier_email_does_not_rename_the_shop():
    store = M.new_store()
    shop = P.extract("A shipment from order uk1 is on the way", "noreply@notify-eu.example-shop.com",
                     "Tracking number: GN123456785GB", t(8))
    shop.merchant = "Bambulab"
    p, _ = M.apply_extraction(store, shop, {"key": "a", "uid": 1, "date": t(8).isoformat(), "subject": "x"}, t(8), "llm")
    rm = P.extract("Your parcel from Etarget Limited is on its way", "Royal Mail <no-reply@royalmail.com>",
                   "Track Parcel GN123456785GB", t(9))
    assert rm.merchant == "Etarget Limited"
    p2, _ = M.apply_extraction(store, rm, {"key": "b", "uid": 2, "date": t(9).isoformat(), "subject": "y"}, t(9), "llm")
    assert p2 is p and p["merchant"] == "Bambulab" and p["carrier"] == "royal_mail"


def test_later_carrier_news_reopens_a_delivery_and_flags_it():
    """DHL, Nov 2026: "delivered" on the 6th, "with your driver for delivery today" on the 9th, "delivered" on the 10th."""
    store = M.new_store()

    def mail(subject, day, n):
        ex = P.extract(subject, "notifications@dhlecommerce.co.uk", "Tracking number: 60129999000001", t(day, 10))
        return M.apply_extraction(store, ex, {"key": f"k{n}", "uid": n, "date": t(day, 10).isoformat(), "subject": subject},
                                  t(day, 10), "rules")

    p, _ = mail("DHL eCommerce UK has delivered your Example parcel", 6, 1)
    assert p["status"] == "delivered" and not p["flagged"]
    p, ev = mail("Your Example parcel is with your DHL eCommerce UK driver for delivery today", 9, 2)
    assert p["status"] == "out_for_delivery" and p["delivered_at"] is None and p["flagged"]
    assert p["flag_reason"] == "Delivered Fri 06 Nov, then out for delivery again"
    assert [e[0] for e in ev] == ["follow_up", "out_for_delivery"]
    assert M.apply_t17(p, "Delivered", None, t(6, 10), None, t(9, 11)) == [] and p["status"] == "out_for_delivery"
    p, ev = mail("DHL eCommerce UK has delivered your Example parcel", 10, 3)
    assert p["status"] == "delivered" and p["delivered_at"].startswith("2026-11-10") and p["flagged"]
    assert [e[0] for e in ev] == ["delivered"]


def test_old_or_weak_news_does_not_reopen():
    now = t(6, 10)
    base = {**M.new_parcel(t(1)), "status": "delivered", "status_at": now.isoformat(), "delivered_at": now.isoformat()}
    assert not M.reopen(dict(base), "out_for_delivery", t(6, 9))           # sent before the delivery
    assert not M.reopen(dict(base), "in_transit", t(8))                     # a late "shipped" mail
    rec = {**base, "received_at": t(7).isoformat()}
    assert M.reopen(rec, "out_for_delivery", t(9)) and rec["status"] == "delivered" and rec["flagged"]


def test_misdelivery_after_delivered_is_flagged():
    now = t(7, 21)
    p = {**M.new_parcel(t(1)), "status": "delivered", "status_at": t(5).isoformat(), "delivered_at": t(5).isoformat()}
    assert M.set_problem(p, P.MISDELIVERED, now)
    assert M.check_follow_up(p, now.date(), now) and p["flag_reason"] == P.MISDELIVERED
    q = {**M.new_parcel(t(1)), "status": "delivered", "status_at": t(5).isoformat(), "delivered_at": t(5).isoformat()}
    assert not M.check_follow_up(q, now.date(), now)                       # an ordinary delivery is not flagged


def test_short_shopify_order_numbers():
    assert P.extract("Order #23909 confirmed", "Shop <store+1@t.shopifyemail.com>", "Thank you for your order!", t(1)).order == "23909"
    assert P.extract("A shipment from order #2390 is on the way", "x@example.com", "", t(1)).order == "2390"
    assert P.extract("Your order is on its way", "x@example.com", "Order 12345 shipped", t(1)).order is None   # 5 without '#'


def test_last_mile_number_in_ship24_record_merges_the_parcels():
    """4PX -> Evri (Nov 2026): Ship24's record of the 4PX number holds the Evri number; the shop's parcel is kept."""
    store = M.new_store()
    shop = {**M.new_parcel(t(2)), "merchant": "Example Babies", "order": "23909", "tracking": ["4PX3000000000042CN"]}
    evri = {**M.new_parcel(t(5)), "carrier": "evri", "tracking": ["H09XYZ0000000042"]}
    other = {**M.new_parcel(t(5)), "carrier": "evri", "tracking": ["H09XYZ0000000999"]}
    for p in (shop, evri, other):
        store["parcels"][p["id"]] = p
    rows = [{"number": "4PX3000000000042CN", "detail": '{"shipment": {"trackingNumbers": [{"tn": "4PX3000000000042CN"}, '
                                                       '{"tn": "H09XYZ0000000042"}]}}'},
            {"number": "H09XYZ0000000042", "detail": "{}"},
            {"number": "UNKNOWN123456"}]
    assert M.last_mile_links(store, rows) == [(evri["id"], shop["id"])]
    M.merge(store, evri["id"], shop["id"], t(11))
    assert shop["tracking"] == ["4PX3000000000042CN", "H09XYZ0000000042"] and shop["carrier"] == "evri"
    assert M.last_mile_links(store, rows) == []                                     # merged: nothing left to do
    assert M.last_mile_links(store, [{"number": "4PX3000000000042CN", "detail": "XH09XYZ0000000999"}]) == []   # not whole


def test_only_export_numbers_are_looked_up_on_ship24():
    for n in ("4PX3000000000042CN", "4PX3000000000077CN", "LP00123456789012", "YT2412345678901234"):
        assert M.is_export_number(n), n
    for n in ("GN876543216GB", "H09XYZ0000000042", "JJD0000000000000042", "1Z999AA10123456784", "TBA123456789012"):
        assert not M.is_export_number(n), n


BAMBU_DELIVERED = ("Ordered Packed In Transit Delivered Hi, user Your order uk100000000000000001 has been successfully "
                   "delivered, according to the carrier's tracking information! View Order Status")


def test_bambu_delivered_email_joins_the_parcel_despite_another_merchant_name():
    """2026-11-11: Bambu's "successfully delivered" made a new in-transit parcel, as the delivered one was named after
    the shipper ("Etarget Limited")."""
    ex = P.extract("Your order uk100000000000000001 has been successfully delivered", "noreply@notify-eu.bambulab.com",
                   BAMBU_DELIVERED, t(11, 13))
    assert ex.status == "delivered" and ex.order == "UK100000000000000001"
    store = M.new_store()
    p = {**M.new_parcel(t(8)), "merchant": "Etarget Limited", "order": "UK100000000000000001", "tracking": ["GN123456785GB"],
         "status": "delivered", "status_at": t(11, 12).isoformat(), "delivered_at": t(11, 12).isoformat()}
    store["parcels"][p["id"]] = p
    q, _ = M.apply_extraction(store, ex, {"key": "z", "uid": 9, "date": t(11, 13).isoformat(), "subject": "x"}, t(11, 13), "rules")
    assert q is p and len(store["parcels"]) == 1


def test_order_twins_made_earlier_are_joined():
    store = M.new_store()
    p = {**M.new_parcel(t(8)), "merchant": "Etarget Limited", "order": "UK100000000000000001", "tracking": ["GN123456785GB"],
         "status": "delivered", "status_at": t(11, 12).isoformat(), "delivered_at": t(11, 12).isoformat()}
    twin = {**M.new_parcel(t(11)), "merchant": "Bambulab", "order": "UK100000000000000001", "status": "in_transit",
            "status_at": t(11, 13).isoformat()}
    short = {**M.new_parcel(t(11)), "merchant": "Shop", "order": "23909"}
    for x in (p, twin, short):
        store["parcels"][x["id"]] = x
    assert M.merge_order_twins(store, t(11, 14)) == [(twin["id"], p["id"])]
    assert twin["merged_into"] == p["id"] and p["status"] == "delivered" and not short.get("merged_into")


def test_ship24_is_only_asked_when_a_carrier_email_names_no_shop():
    store = M.new_store()
    now = t(11, 14)
    shop = {**M.new_parcel(t(2)), "merchant": "Example Babies", "order": "23909", "tracking": ["4PX3000000000042CN"],
            "emails": [{"date": t(1, 4).isoformat()}]}
    later = {**M.new_parcel(t(10)), "merchant": "Seeed", "tracking": ["4PX3000000000077CN"],
             "emails": [{"date": t(10, 9).isoformat()}]}
    store["parcels"].update({shop["id"]: shop, later["id"]: later})
    assert M.export_candidates(store, now) == []                       # no shop-less carrier parcel: nothing to ask
    evri = {**M.new_parcel(t(6)), "carrier": "evri", "tracking": ["H09XYZ0000000042"], "emails": [{"date": t(5, 14).isoformat()}]}
    store["parcels"][evri["id"]] = evri
    assert M.export_candidates(store, now) == [shop]                   # the Seeed parcel shipped after Evri's email
    evri["merchant"] = "Example Babies"
    assert M.export_candidates(store, now) == []                       # the carrier email named the shop
    evri["merchant"] = None
    assert M.export_candidates(store, t(30)) == []                     # 21 days on: no longer looked into


def test_a_merge_is_shown_on_the_remaining_parcel():
    store = M.new_store()
    shop = {**M.new_parcel(t(2)), "merchant": "Example Babies", "tracking": ["4PX3000000000042CN"]}
    evri = {**M.new_parcel(t(5)), "carrier": "evri", "tracking": ["H09XYZ0000000042"]}
    store["parcels"].update({shop["id"]: shop, evri["id"]: evri})
    M.merge(store, evri["id"], shop["id"], t(11, 14), "last_mile")
    v = M.view(shop, date(2026, 11, 11))
    assert v["tracking_numbers"] == ["4PX3000000000042CN", "H09XYZ0000000042"]
    assert v["merged"] == ["Evri parcel (H09XYZ0000000042) · 11 Nov · same parcel on Ship24"]
    assert v["merged_recent"] == "Merged with Evri parcel (H09XYZ0000000042)"
    assert M.view(shop, date(2026, 11, 13))["merged_recent"] is None          # card line only for two days
    assert M.backfill_merges(store) == 0                                       # already recorded


def test_merges_made_before_records_are_backfilled():
    store = M.new_store()
    p = {**M.new_parcel(t(8)), "order": "UK100000000000000001", "tracking": ["GN123456785GB"]}
    twin = {**M.new_parcel(t(11)), "merchant": "Bambulab", "order": "UK100000000000000001"}
    store["parcels"].update({p["id"]: p, twin["id"]: twin})
    M.merge(store, twin["id"], p["id"], t(11, 14))
    p.pop("merges")                                                            # as merged by the earlier code
    assert M.backfill_merges(store) == 1 and p["merges"][0]["how"] == "order"


def test_merged_parcel_follows_the_most_recently_updated_17track_number():
    """Cuddleberry (Nov 2026): 4PX export number + Evri last-mile number after the merge. The card and links must use
    the number 17TRACK updated last; on a tie the one in the carrier's format (Evri), never 4PX with Evri's code."""
    from types import SimpleNamespace as P
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    p = M.new_parcel(now)
    p.update(carrier="evri", tracking=["4PX3000000000042CN", "H09XYZ0000000042"])
    assert M.fits_carrier("evri", "H09XYZ0000000042") and not M.fits_carrier("evri", "4PX3000000000042CN")
    assert M.main_number(p) == "H09XYZ0000000042"                       # no 17TRACK news yet: the carrier's format
    t = datetime(2026, 11, 13, 14, 52, tzinfo=timezone.utc)
    pk = {"4PX3000000000042CN": P(status="In Transit", timestamp=t),
          "H09XYZ0000000042": P(status="In Transit", timestamp=t)}
    assert M.pick_t17(p, pk)[0] == "H09XYZ0000000042"                   # tie: last-mile number
    pk["4PX3000000000042CN"] = P(status="In Transit", timestamp=t + timedelta(hours=1))
    assert M.pick_t17(p, pk)[0] == "4PX3000000000042CN"                 # newer event wins
    pk["4PX3000000000042CN"] = P(status="Not Found", timestamp=None)
    assert M.pick_t17(p, pk)[0] == "H09XYZ0000000042"                   # tracked beats not found
    p["t17"]["number"] = "4PX3000000000042CN"
    v = M.view(p, now.date())
    assert v["tracking"] == "4PX3000000000042CN" and "nums=4PX3000000000042CN" in v["t17_url"]
    assert "fc=" not in v["t17_url"] and v["carrier_url"] is None          # not Evri's code / page for the 4PX number
    p["t17"]["number"] = "H09XYZ0000000042"
    assert "fc=" in M.view(p, now.date())["t17_url"]
    assert M.t17_registered({"tracking": ["A1"], "t17": {"registered": True}}) == {"A1"}   # before Nov 2026: first only
    assert M.t17_registered({"tracking": ["A1", "B2"], "t17": {"registered": True, "numbers": ["A1", "B2"]}}) == {"A1", "B2"}
    assert M.pick_t17(p, {}) is None
    assert M.fits_carrier("dpd", "15501234567890") and not M.fits_carrier(None, "H09XYZ0000000042")


def test_sort_by_status():
    today = date(2026, 11, 15)
    def mk(i, **kw):
        return {"id": i, "created": f"2026-11-0{i}T10:00:00+00:00", "t17": {}, "emails": [], **kw}
    ps = [mk(1, status="ordered"), mk(2, status="in_transit"), mk(3, status="out_for_delivery"),
          mk(4, status="delivered"), mk(5, status="in_transit", flagged=True), mk(6, status="shipped"),
          mk(7, status="in_transit", eta_date="2026-11-15")]   # due today with the carrier = out for delivery
    order = [p["id"] for p in sorted(ps, key=lambda p: M.sort_key(p, today))]
    assert order[:2] == [5, 4] and set(order[2:4]) == {3, 7} and order[4:] == [2, 6, 1]


def test_no_movement_flag_clears_when_the_carrier_moves_again():
    now = datetime(2026, 11, 15, 20, tzinfo=timezone.utc)
    p = {"id": "x", "status": "in_transit", "created": "2026-11-01T00:00:00+00:00", "flagged": True,
         "flag_reason": M.NO_MOVEMENT_REASON, "t17": {"at": "2026-11-15T17:51:00+00:00"},
         "emails": [{"date": "2026-11-06T11:11:32+00:00"}]}
    assert not M.check_follow_up(p, now.date(), now) and not p["flagged"] and p["flag_reason"] is None
    # still stale: stays flagged
    q = dict(p, flagged=True, flag_reason=M.NO_MOVEMENT_REASON, t17={"at": "2026-11-10T00:00:00+00:00"})
    M.check_follow_up(q, now.date(), now)
    assert q["flagged"]
    # other reasons are never cleared by movement
    r = dict(p, flagged=True, flag_reason="Delivery failed")
    M.check_follow_up(r, now.date(), now)
    assert r["flagged"]


# --- delivery address (Nov 2026): month-first dates, eBay "shipping info", the address check
EBAY_SHIPPED = """<html><body><div>Estimated delivery: </div><div>Mon, Nov 16</div><p>Just in! Shipping info now available.</p>
<p>Order number: 12-34567-89012</p><p>Your order will ship to: </p><div>Alex Example</div><div>1 Test Road</div>
<div>London, ZZ9 9ZZ</div><div>United Kingdom</div><div> Estimated delivery: </div><div>Mon, Nov 16</div></body></html>"""
AMAZON_ELSEWHERE = """Your package is out for delivery! Ordered Dispatched Out for delivery Delivered Arriving today
12:00 pm - 3:30 pm Sam – LONDON Order # 026-1111111-2222222 Track package"""
AMAZON_HOME = """Arriving Wednesday 18 November 2026 Alex de Example – LONDON Order # 026-3333333-4444444 View or edit order"""


def test_ebay_month_first_date_status_and_address():
    ex = P.extract("Order update: Solar panel", "eBay <ebay@ebay.com>", EBAY_SHIPPED, datetime(2026, 11, 16, 12, 26, tzinfo=UTC))
    assert ex.is_shipping and ex.status == "shipped" and ex.eta_date == date(2026, 11, 16)
    assert ex.ship_to == "Alex Example 1 Test Road London, ZZ9 9ZZ United Kingdom"
    assert P.is_home(ex.ship_to, "ZZ99ZZ", ["Alex"]) is True
    assert P.is_home("Sam, 2 Other St, London, N1 2AB", "ZZ99ZZ", ["Alex"]) is False   # postcode decides


def test_amazon_address_name():
    sent = datetime(2026, 11, 16, 11, 52, tzinfo=UTC)
    ex = P.extract("Out for delivery: 'PIR switch'", "shipment-tracking@amazon.co.uk", AMAZON_ELSEWHERE, sent)
    assert ex.ship_to == "Sam – LONDON" and P.is_home(ex.ship_to, "ZZ9 9ZZ", ["Alex"]) is False
    ex = P.extract("Delivery estimate update", "shipment-tracking@amazon.co.uk", AMAZON_HOME, sent)
    assert ex.ship_to == "Alex de Example – LONDON" and P.is_home(ex.ship_to, "ZZ9 9ZZ", ["alex"]) is True
    assert P.is_home(ex.ship_to, "", []) is None and P.is_home(None, "ZZ9 9ZZ", ["Alex"]) is None


def test_abbreviated_weekday():
    assert P._find_eta("", "Estimated delivery: Thu", date(2026, 11, 16))[0] == date(2026, 11, 19)


def test_undone_received_goes_back_to_the_email_status():
    # Nov 2026: an accidental "Received" tap marked a just-ordered Amazon parcel delivered; undoing
    # "received" left it "Delivered, confirm?". The manager replays its emails, which must give "in transit" back.
    p = {"id": "x", "status": "ordered", "status_at": SENT.isoformat(), "status_source": "email", "emails": []}
    M.set_status(p, "delivered", SENT + timedelta(hours=6), "user")
    assert p["status"] == "delivered"
    exs = [P.extract("Ordered: 2 ‘Item 50 Pack...’", "auto-confirm@amazon.co.uk", "", SENT),
           P.extract("Dispatched: 2 ‘Item 50 Pack...’", "shipment-tracking@amazon.co.uk", "",
                     SENT + timedelta(hours=9))]
    assert M.rebuild_status(p, exs)
    assert p["status"] == "in_transit" and p["status_source"] == "email" and not p.get("delivered_at")


def test_email_note_says_what_the_email_changed():
    store = M.new_store()
    p, _ = _apply(store, "We have your parcel", "notifications@dhlecommerce.co.uk", DHL, t(4), "k1")
    assert p["emails"][-1]["note"] == "In transit · due Fri 6 Nov"
    p, _ = _apply(store, "Your parcel is delayed", "notifications@dhlecommerce.co.uk",
                  "Your parcel is delayed. We will now deliver it on Monday, 9 November.\nShipment number: 60129999000001",
                  t(5), "k2")
    assert p["emails"][-1]["note"] == "Delayed · new date Mon 9 Nov"
    assert M.view(p, date(2026, 11, 5))["emails"][-1]["note"] == "Delayed · new date Mon 9 Nov"
