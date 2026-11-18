"""Regex pass over a shipping email (no Home Assistant imports: unit-tested on its own).

`extract(subject, sender, text, sent_at)` returns an Extraction: the carrier from the sender domain (or the
"Forwarded message" block of a manual forward), tracking numbers from per-carrier patterns, order number, merchant,
status and ETA. The local LLM later fills what this misses (item description, odd wording); its tracking numbers
are only accepted when they appear verbatim in the email text.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from . import rules as _rules

# The country / language data (carriers, status phrases, month names...) lives in rules.yaml and the user's
# parcels_rules.yaml; see rules.py. `rules()` is the active set, `set_rules()` swaps it (Home Assistant does that at setup).
_ACTIVE: _rules.Rules = _rules.load_rules()


def rules() -> _rules.Rules:
    return _ACTIVE


def set_rules(new: _rules.Rules) -> None:
    global _ACTIVE
    _ACTIVE = new


# Compiled rules that other modules (and tests) read under their old constant names.
_DYNAMIC = {"CARRIERS": "carriers", "TRACKING": "tracking", "PROGRESS_BAR": "progress_bar",
            "SHIPPING_HINT": "shipping_hint", "TO_BE_DELIVERED": "to_be_delivered"}


def __getattr__(name: str):
    if name in _DYNAMIC:
        return getattr(_ACTIVE, _DYNAMIC[name])
    raise AttributeError(name)


MISDELIVERED = "Misdelivered / investigating"     # the problem text the program knows by name (see rules.yaml problems)
FORWARDED_MARK = re.compile(r"-{3,}\s*Forwarded message\s*-{3,}|^Begin forwarded message:", re.I | re.M)
FORWARDED_FROM = re.compile(r"-{3,}\s*Forwarded message\s*-{3,}.*?^From:\s*(.*?)$", re.I | re.S | re.M)
FORWARDED_FIELD = r"^{}:\s*(.*?)$"
EMAIL_ADDR = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

STATUS_RANK = {"ordered": 0, "shipped": 1, "in_transit": 2, "out_for_delivery": 3, "pickup": 4, "delivered": 5}
# Links in the emails.
A_TAG = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
BARE_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
MAX_URL = 800


@dataclass
class Extraction:
    is_shipping: bool = False
    sender: str = ""                      # original sender address (forwarded mail resolved)
    sender_domain: str = ""
    carrier: str | None = None            # key in CARRIERS
    merchant: str | None = None
    item: str | None = None
    lines: list[dict] = field(default_factory=list)   # order lines {name, variant, qty} (LLM stage only)
    tracking: list[str] = field(default_factory=list)
    order: str | None = None
    status: str | None = None             # key in STATUS_RANK
    problem: str | None = None
    eta_date: date | None = None
    eta_window: str | None = None         # "09:40–11:40"
    sent_at: datetime | None = None
    subject: str = ""
    order_url: str | None = None          # the merchant's order page, from the email's links
    carrier_url: str | None = None        # the carrier's tracking page, from the email's links
    ship_to: str | None = None            # who / where the email says it goes: "Sam – LONDON", "… London, ZZ99ZZ"


def html_to_text(body: str) -> str:
    """Rough HTML -> text, good enough for the patterns and the LLM."""
    body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t ]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n", body).strip()


def normalise_tracking(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


def carrier_for_domain(domain: str) -> str | None:
    domain = domain.lower()
    for key, (_, _, domains) in _ACTIVE.carriers.items():
        if any(domain == d or domain.endswith("." + d) for d in domains):
            return key
    return None


def _original_sender(sender: str, text: str) -> tuple[str, str]:
    """Return (address, subject) of the original email when `text` is a manual forward."""
    m = FORWARDED_FROM.search(text[:4000])
    if not m:
        return sender, ""
    block = text[m.start():m.start() + 1500]
    subj = re.search(FORWARDED_FIELD.format("Subject"), block, re.M)
    addr = EMAIL_ADDR.search(m.group(1))
    return (m.group(1).strip() if addr else sender), (subj.group(1).strip() if subj else "")   # "Name <address>"


def _original_date(text: str, sent_at: datetime | None) -> datetime | None:
    """When the email was really sent: the Date of the innermost forwarded block, else `sent_at`.

    A manual forward carries a new Date header, so "arriving today" must be read against the date quoted in the body.
    Those dates usually have no zone: taken as the forward's zone. A date later than the forward itself is ignored.
    """
    marks = list(FORWARDED_MARK.finditer(text[:4000]))
    if not marks:
        return sent_at
    m = re.search(r"^[\s*>]*(?:Date|Sent):\*?\s*(.+?)\s*$", text[marks[-1].start():marks[-1].start() + 1500], re.I | re.M)
    if not m:
        return sent_at
    raw = re.sub(r"\s+at\s+", " ", re.sub(r"[\u202f\xa0\s]+", " ", m.group(1))).strip()
    found = None
    try:
        found = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        for fmt in ("%a, %b %d, %Y %I:%M %p", "%A, %B %d, %Y %I:%M %p", "%a, %d %b %Y %H:%M", "%A, %d %B %Y %H:%M",
                    "%d %B %Y %H:%M", "%d %b %Y %H:%M", "%B %d, %Y %I:%M %p"):
            try:
                found = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if found is None:
        return sent_at
    if found.tzinfo is None:
        found = found.replace(tzinfo=sent_at.tzinfo if sent_at else None)
    if sent_at and found.tzinfo and found > sent_at + timedelta(days=1):
        return sent_at
    return found


def _find_status(subject: str, body: str) -> str | None:
    r = _ACTIVE
    for pattern, status in r.subject_status:              # "Ordered: 'item...'" (Amazon), "Your order X is confirmed"
        if pattern.search(subject):
            return status
    for src in (subject, body[:1500]):
        for pattern, status in r.status:
            if pattern.search(src):
                return status
    return None


def _find_problem(subject: str, body: str) -> str | None:
    for src in (subject, body[:2000]):
        for pattern, label in _ACTIVE.problems:
            if pattern.search(src):
                return label
    return None


def _find_tracking(carrier: str | None, text: str) -> list[str]:
    found: list[str] = []
    patterns = _ACTIVE.tracking_re.get(carrier, []) + _ACTIVE.tracking_re[None]
    for m in _ACTIVE.labelled.finditer(text):
        val = normalise_tracking(m.group(1).split("\n")[0])
        if 8 <= len(val) <= 30 and re.search(r"\d", val):
            found.append(val)
    for p in patterns:
        found += [normalise_tracking(x) for x in p.findall(text)]
    out: list[str] = []
    for t in found:   # unique, keep order; drop values that are a prefix/suffix of a longer one
        if t not in out and not any(t != o and (o.startswith(t) or o.endswith(t)) for o in found if len(o) > len(t)):
            out.append(t)
    return out[:5]


def _find_merchant(text: str, subject: str, sender_domain: str, carrier: str | None, sender: str = "") -> str | None:
    r = _ACTIVE
    for dom, merchant in r.merchants.items():
        if sender_domain == dom or sender_domain.endswith("." + dom):
            return merchant.name
    if any(sender_domain == d or sender_domain.endswith("." + d) for d in r.platform_domains):
        display = re.sub(r"\s*<[^>]*>\s*$", "", sender).strip(' "\'')
        if display and "@" not in display:
            return display[:40]
    for pattern in r.merchant_patterns:
        for src in (subject, text[:3000]):
            m = pattern.search(src)
            if m:
                name = m.group(1).strip(" .*")
                if name.lower() not in r.not_merchant_words and len(name) > 1:
                    flat = name.replace(" ", "").lower()
                    for known in r.merchants.values():
                        if known.fix_spelling and re.sub(r"\s", "", known.name).lower() == flat:
                            return known.name
                    return name
    if carrier is None and sender_domain:
        two_part = sender_domain.count(".") >= 2 and any(sender_domain.endswith("." + x) for x in r.two_part_suffixes)
        base = sender_domain.split(".")[-3] if two_part else sender_domain.split(".")[-2]
        if base not in r.webmail_domains:
            return base.capitalize()
    return None


def _next_weekday(base: date, wd: int) -> date:
    return base + timedelta(days=(wd - base.weekday()) % 7)


def _month_number(months: list[str], word: str) -> int:
    """1-12 for a month as written ("nov", "november", "juil"): the first month it is the start of (or starts with)."""
    for i, name in enumerate(months):
        if name.startswith(word) or word.startswith(name):
            return i + 1
    return next(i + 1 for i, name in enumerate(months) if name[:3] == word[:3])


def _find_eta(subject: str, body: str, sent: date) -> tuple[date | None, str | None]:
    src = subject + "\n" + body[:3000]
    window = None
    r = _ACTIVE
    m = r.time_window.search(src)
    if m:
        h1, m1, h2, m2, ampm = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4), (m.group(5) or "").lower()
        if ampm == "pm" and h2 < 12:
            h2 += 12
            if h1 + 12 <= h2:
                h1 += 12
        if h1 < 24 and h2 < 24:
            window = f"{h1:02d}:{m1}–{h2:02d}:{m2}"
    low = src.lower()
    # explicit date: "Friday, 6 November" / "6 November" / "6th Nov", or month first (eBay): "Mon, Nov 16"
    for pattern, day_g, month_g in r.date_patterns:
        m = pattern.search(low)
        if m and r.eta_context.search(low[max(0, m.start() - 120):m.start()]):
            try:
                d = date(sent.year, _month_number(r.months, m.group(month_g)), int(m.group(day_g)))
                if d < sent - timedelta(days=60):
                    d = d.replace(year=sent.year + 1)
                return d, window
            except ValueError:
                pass
    if r.eta_today.search(low):
        return sent, window
    if r.eta_tomorrow.search(low):
        return sent + timedelta(days=1), window
    m = r.eta_weekday.search(low)
    if m:
        abbr = m.group(1)[:3]
        return _next_weekday(sent, [x[:3] for x in r.weekdays].index(abbr)), window
    return None, window


def _find_ship_to(text: str) -> str | None:
    for pattern in _ACTIVE.ship_to_address_book:
        m = pattern.search(text[:6000])
        if m:
            return f"{m.group(1)} – {m.group(2).strip()}"
    m = _ACTIVE.ship_to.search(text[:8000])
    return re.sub(r"\s+", " ", m.group(1)).strip(" ,") if m else None


def is_home(ship_to: str | None, postcode: str | None, names: list[str] | None) -> bool | None:
    """Whether the address an email names is home. A UK postcode decides when both sides have one; otherwise one of
    the home names (the Amazon address-book entry, "Alex – LONDON") must appear. None = can't tell."""
    if not ship_to:
        return None
    pc = re.sub(r"[^A-Z0-9]", "", (postcode or "").upper())
    found = next((m for m in (p.search(ship_to.upper()) for p in _ACTIVE.postcodes) if m), None)
    if pc and found:
        return "".join(g or "" for g in found.groups()) == pc
    names = [n.strip().lower() for n in names or [] if n.strip()]
    if names:
        return any(re.search(r"\b" + re.escape(n) + r"\b", ship_to.lower()) for n in names)
    return None


def find_links(html_body: str | None, plain: str | None) -> list[tuple[str, str]]:
    """(url, link text) for every web link in an email: HTML anchors first, then URLs written in the plain text."""
    links = []
    for href, label in A_TAG.findall(html_body or ""):
        links.append((html.unescape(href).strip(), re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", label))).strip()))
    for line in (plain or "").splitlines():
        for m in BARE_URL.finditer(line):
            links.append((m.group(0).rstrip(".,;"), line[:m.start()].strip(" <([:-")[-60:]))
    return links[:300]


def safe_url(url: str | None) -> str | None:
    """A web link fit for a dashboard: http(s) only, no characters that could break out of a Markdown link."""
    if not url or len(url) > MAX_URL or not re.match(r"https?://[^/\s]+\.[^/\s]+", url, re.I):
        return None
    if re.search(r"[\s<>\"'`\\]", url):
        return None
    return url.replace("(", "%28").replace(")", "%29")


def pick_links(links: list[tuple[str, str]], tracking: list[str], carrier: str | None = None) -> tuple[str | None, str | None]:
    """Choose (order page, carrier tracking page) from an email's links; None when the email has no such link.

    The link text decides first ("View order", "Track your parcel"), then the address (contains the tracking number,
    or /orders/). Footer links (unsubscribe, help, reviews...) never qualify.
    """
    order = track = order_by_url = track_by_url = None
    numbers = [t.lower() for t in tracking]
    for url, label in links:
        url = safe_url(url)
        if not url or _ACTIVE.not_a_parcel_link.search(url) or _ACTIVE.not_a_parcel_link.search(label) or "17track" in url.lower():
            continue
        low = url.lower()
        if not track and _ACTIVE.track_label.search(label):
            track = url
        elif not order and _ACTIVE.order_label.search(label):
            order = url
        if not track_by_url and any(n in low for n in numbers):
            track_by_url = url
        elif not order_by_url and _ACTIVE.order_path.search(low):
            order_by_url = url
    if carrier == "amazon":   # Amazon has no carrier page: "Track package" goes to the order
        return order or track or order_by_url or track_by_url, None
    return order or order_by_url, track or track_by_url


def carrier_page(carrier: str | None, number: str | None) -> str | None:
    """The carrier's own tracking page for this number, when its address is known."""
    pages = _ACTIVE.carrier_pages
    return pages[carrier].format(quote(number, safe="")) if number and carrier in pages else None


def order_page(merchant: str | None, order: str | None) -> str | None:
    """The order page of the shops whose address is predictable (the email's link is preferred when there is one)."""
    shop = _ACTIVE.merchant_for_name(merchant, "order_page")
    if not shop:
        return None
    if order:
        return shop.order_page.format(quote(order, safe=""))
    return shop.order_history_page


def extract(subject: str, sender: str, text: str, sent_at: datetime | None,
            links: list[tuple[str, str]] | None = None) -> Extraction:
    text = text or ""
    r = _ACTIVE
    if "<html" in text[:2000].lower() or re.search(r"</(?:div|td|p)>", text[:5000], re.I):
        text = html_to_text(text)
    sent_at = _original_date(text, sent_at)
    orig, fwd_subject = _original_sender(sender or "", text)
    if fwd_subject:
        subject = fwd_subject
    else:
        subject = r.forward_prefix.sub("", subject or "", count=1)
    m = EMAIL_ADDR.search(orig or "")
    domain = m.group(1).lower() if m else ""
    carrier = carrier_for_domain(domain)
    ex = Extraction(sender=m.group(0).lower() if m else (orig or ""), sender_domain=domain, carrier=carrier,
                    subject=subject, sent_at=sent_at)
    ex.tracking = _find_tracking(carrier, text)
    om = r.order.search(subject) or r.order.search(text[:4000])
    ex.order = (om.group(1) or om.group(2)).upper() if om else None
    if ex.order and ex.order in ex.tracking:
        ex.order = None
    plain_text = r.future_clause.sub(" ", r.progress_bar.sub(" ", text))
    ex.status = _find_status(subject, plain_text)
    ex.problem = _find_problem(subject, text)
    ex.merchant = _find_merchant(text, subject, domain, carrier, orig or "")
    if sent_at:
        ex.eta_date, ex.eta_window = _find_eta(subject, plain_text, sent_at.date())
    ex.order_url, ex.carrier_url = pick_links(links or [], ex.tracking, carrier)
    ex.ship_to = _find_ship_to(text)
    ex.is_shipping = bool(carrier or ex.tracking or (ex.status and r.shipping_hint.search(subject)))
    return ex


def tracking_url(carrier: str | None, number: str) -> str:
    if carrier == "amazon" or number.startswith("TBA"):
        shop = _ACTIVE.merchant_for_name("Amazon", "order_history_page")
        return shop.order_history_page if shop else "https://www.amazon.co.uk/gp/css/order-history"
    code = _ACTIVE.carriers.get(carrier or "", (None, None, ()))[1]
    return f"https://t.17track.net/en#nums={number}" + (f"&fc={code}" if code else "")
