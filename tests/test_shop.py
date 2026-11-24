"""Shop order page check: links, page reading, address safety, schedule, adding a number."""
import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

from parcels_core import model as M
from parcels_core import parser as P
from parcels_core import shop as S

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
KEY = "k" * 900
ORDER = f"https://shop.example.com/12345678901/orders/{'a1' * 16}/authenticate?key={KEY}"
WRAPPED = "https://shop.example.com/_t/c/v3/" + "B" * 1600
APP = "https://open.shop.app/E2jp6/abc?sbt=1"
FILLER = "<p>" + "Thank you for your order, we are getting it ready. " * 10 + "</p>"


def _parcel(days=5, **kw):
    p = M.new_parcel(NOW - timedelta(days=days))
    p.update(merchant="Acme", order="A1", status="ordered", shop_order_url=ORDER)
    p.update(kw)
    return p


# --- links
def test_direct_and_wrapped_shop_links():
    assert P.shop_links([(ORDER, "View your order"), (APP, "Track order with Shop")]) == (ORDER, APP)
    links = [(WRAPPED + "0", "Acme"), (WRAPPED + "1", "View your order"), (WRAPPED + "2", "Track order with"),
             (WRAPPED + "3", "Visit our store")]
    assert P.shop_links(links) == (WRAPPED + "1", WRAPPED + "2")


def test_other_shops_have_no_shop_links():
    assert P.shop_links([("https://acme.example/orders/1", "View your order"), ("https://acme.example/t", "Track parcel")]) == (None, None)


def test_the_shop_app_link_is_not_the_carrier_page():
    assert P.pick_links([(APP, "Track order with Shop"), ("https://x.example/_t/c/v3/a", "Track order with")], []) == (None, None)


def test_an_email_keeps_its_shop_links():
    ex = P.extract("Order #A1 confirmed", "Acme <support@acme.example>", "Thank you for your order! Order #A1", NOW,
                   [(ORDER, "View your order"), (APP, "Track order with Shop")])
    store = M.new_store()
    p, _ = M.apply_extraction(store, ex, {"key": "k", "uid": 1, "date": NOW.isoformat(), "subject": "s"}, NOW, "regex")
    assert (p["shop_order_url"], p["shop_url"]) == (ORDER, APP)
    v = M.view(p, NOW.date())
    assert v["order_url"] == ORDER and v["shop_url"] == APP and v["needs_tracking"]


# --- reading the page
@pytest.mark.parametrize("body, number", [
    (FILLER + "<p>Yuanpeng tracking number: <a href='https://t.example/x'>YP0000000042KD</a></p>", "YP0000000042KD"),
    (FILLER + "<a href='https://carrier.example/track?n=1'>ZS00000000042</a>", "ZS00000000042"),
])
def test_a_number_on_the_page(body, number):
    r = S.read_page(200, "/orders/1", body)
    assert (r.outcome, r.number) == (S.FOUND, number)


def test_page_without_a_number_is_not_yet():
    assert S.read_page(200, "/orders/1", FILLER + "Order number 12345678").outcome == S.NOT_YET


def test_data_the_page_hides_is_not_used():
    hidden = '<script>{"trackingInformation":[{"number":"ZS00000000042"}], "t": "Tracking number: ZS00000000042"}</script>'
    assert S.read_page(200, "/orders/1", hidden + "<div id='app'></div>").outcome == S.UNREADABLE
    for wrap in ("<template>%s</template>", "<noscript>%s</noscript>", "<!-- %s -->"):
        assert S.read_page(200, "/orders/1", FILLER + wrap % "Tracking number: ZS00000000042").outcome == S.NOT_YET


@pytest.mark.parametrize("status, path, outcome", [
    (403, "/orders/1", S.UNREADABLE), (401, "/", S.UNREADABLE), (406, "/", S.UNREADABLE), (404, "/", S.UNREADABLE),
    (200, "/account/login", S.UNREADABLE), (200, "/challenge", S.UNREADABLE), (429, "/", S.RETRY), (503, "/", S.RETRY)])
def test_refusals_stop_and_server_trouble_retries(status, path, outcome):
    assert S.read_page(status, path, FILLER + "Tracking number: ZS00000000042").outcome == outcome


# --- addresses come from an email: not trusted
@pytest.mark.parametrize("url", ["http://shop.example.com/a", "https://192.168.1.1/a", "https://[::1]/a", "https://localhost/a",
                                 "https://shop.example.com:8123/a", "https://homeassistant.local/a", "https://u:p@shop.example.com/",
                                 "https://127.1/", "https://2130706433/", "https://0x7f.1/", "https://127.0.0.0x1/", "ftp://shop.example.com/", "https://a:b/", "https://nas.lan/"])
def test_bad_addresses(url):
    assert not S.allowed_url(url)


def test_good_address_and_public_addresses():
    assert S.allowed_url(ORDER)
    assert S.public_address("8.8.8.8") and S.public_address("2606:4700::1")
    for a in ("192.168.1.130", "10.0.0.1", "127.0.0.1", "::1", "fe80::1%eth0", "169.254.1.1", "100.64.0.1", "::ffff:192.168.1.1", "fd00::1", "224.0.0.1", "x"):
        assert not S.public_address(a), a


class _Resp:
    def __init__(self, status, headers, body=b""):
        self.status, self.headers, self._b = status, headers, body
        self.content = self

    async def iter_chunked(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _serve(monkeypatch, pages):
    aiohttp = pytest.importorskip("aiohttp")
    seen = []

    def get(self, url, **kw):
        seen.append(url)
        assert kw["allow_redirects"] is False
        return pages[url]
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    return seen


def test_redirects_are_followed_one_by_one_and_checked(monkeypatch):
    page = _Resp(200, {"Content-Type": "text/html"}, (FILLER + "Tracking number: ZS00000000042").encode())
    seen = _serve(monkeypatch, {ORDER: _Resp(302, {"Location": "/orders/1"}), "https://shop.example.com/orders/1": page})
    r = asyncio.run(S.check(ORDER))
    assert r.number == "ZS00000000042" and len(seen) == 2
    seen = _serve(monkeypatch, {ORDER: _Resp(302, {"Location": "https://192.168.1.1/admin"})})
    assert asyncio.run(S.check(ORDER)).outcome == S.UNREADABLE and seen == [ORDER]
    seen = _serve(monkeypatch, {ORDER: _Resp(302, {"Location": ORDER})})
    assert asyncio.run(S.check(ORDER)).reason == "too many redirects" and len(seen) == S.MAX_REDIRECTS + 1


def test_big_or_odd_answers_are_refused(monkeypatch):
    _serve(monkeypatch, {ORDER: _Resp(200, {"Content-Type": "text/html"}, b"x" * (S.MAX_BYTES + 1))})
    assert asyncio.run(S.check(ORDER)).reason == "page too large"
    _serve(monkeypatch, {ORDER: _Resp(200, {"Content-Type": "application/pdf"})})
    assert asyncio.run(S.check(ORDER)).outcome == S.UNREADABLE


def test_a_name_that_points_into_the_home_network_is_refused(monkeypatch):
    pytest.importorskip("aiohttp")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.130", 443))])
    r = asyncio.run(S.check("https://evil.example.com/x"))
    assert r.outcome == S.RETRY and r.number is None


# --- schedule
def due(p, now=NOW, start=3, every=3, stop=30):
    return M.shop_check_due(p, now, start, every, stop)


def test_schedule():
    assert due(_parcel(days=2)) is None
    p = _parcel(days=4)
    assert due(p) == "check"
    M.shop_check_result(p, "not_yet", "", NOW, False)
    assert due(p, NOW + timedelta(days=2)) is None
    assert due(p, NOW + timedelta(days=3)) == "check"
    assert due(p, NOW + timedelta(days=27)) == "give_up"
    assert due(_parcel(days=60)) == "check", "an old order gets one check"


@pytest.mark.parametrize("kw", [{"tracking": ["X1234567890"]}, {"shop_order_url": None}, {"status": "in_transit"},
                                {"status": "delivered"}, {"received_at": NOW.isoformat()}, {"merged_into": "z"},
                                {"carrier": "amazon"}, {"shop_check": {"done": True}}])
def test_not_checked(kw):
    assert due(_parcel(**kw)) is None


def test_the_end_flags_once():
    p = _parcel()
    assert M.shop_check_result(p, "not_yet", "", NOW, False) is False and not p["flagged"]
    assert M.shop_check_result(p, "unreadable", "login needed", NOW, True) is True
    assert p["flag_reason"] == M.NO_TRACKING_REASON and p["shop_check"]["tries"] == 2
    p.update(flagged=False)
    assert due(p, NOW + timedelta(days=9)) is None, "cleared by the user: not flagged again"


# --- adding a number
def test_add_tracking_clears_the_flag_and_moves_the_status():
    store, p = M.new_store(), _parcel()
    store["parcels"][p["id"]] = p
    M.shop_check_result(p, "unreadable", "", NOW, True)
    M.add_tracking(store, p, "ZS00000000042", NOW, "user")
    assert p["tracking"] == ["ZS00000000042"] and p["status"] == "shipped" and not p["flagged"]
    assert not M.view(p, NOW.date())["needs_tracking"]
    with pytest.raises(ValueError):
        M.add_tracking(store, p, "ZS00000000044", NOW, "user")


def test_another_flag_stays_and_taken_numbers_are_refused():
    store, p, o = M.new_store(), _parcel(flagged=True, flag_reason="Ask the shop"), _parcel(tracking=["ZS00000000042"])
    store["parcels"] = {p["id"]: p, o["id"]: o}
    with pytest.raises(ValueError):
        M.add_tracking(store, p, "ZS00000000042", NOW, "user")
    M.add_tracking(store, p, "ZS00000000043", NOW, "user")
    assert p["flagged"] and p["flag_reason"] == "Ask the shop"


@pytest.mark.parametrize("raw, ok", [(" zs 0000-0000042 ", "ZS00000000042"), ("hello world", None), ("1234567", None),
                                     ("A" * 40 + "1234", None), ("ABCDEFGH12", None), (None, None), ("ZS000<b>00000042", None)])
def test_valid_tracking(raw, ok):
    assert P.valid_tracking(raw) == ok
