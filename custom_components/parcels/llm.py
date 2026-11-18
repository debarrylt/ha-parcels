"""Second-pass extraction with an AI: item, order lines, merchant, status, ETA.

Two backends share one validation step (`merge_answer`): an Ollama server (/api/chat with a JSON-schema `format`,
temperature 0, thinking off) and Home Assistant's AI Task (`ai_task.generate_data`, JSON parsed from the reply text).
The answer is validated: tracking numbers must appear verbatim in the email, enums and dates must parse, strings
are capped.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import aiohttp

from .const import DEFAULT_OLLAMA_MODEL as MODEL
from .parser import STATUS_RANK, Extraction, normalise_tracking

TIMEOUT = aiohttp.ClientTimeout(total=180)
MAX_TEXT = 6000
SCHEMA = {
    "type": "object",
    "properties": {
        "is_shipping": {"type": "boolean"},
        "merchant": {"type": ["string", "null"]},
        "item": {"type": ["string", "null"]},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "variant": {"type": ["string", "null"]}, "quantity": {"type": "integer"}},
            "required": ["name", "variant", "quantity"]}},
        "tracking_numbers": {"type": "array", "items": {"type": "string"}},
        "order_number": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"], "enum": [*STATUS_RANK, None]},
        "problem": {"type": ["string", "null"]},
        "eta_date": {"type": ["string", "null"]},
        "eta_window": {"type": ["string", "null"]},
    },
    "required": ["is_shipping", "merchant", "item", "lines", "tracking_numbers", "order_number", "status", "problem",
                 "eta_date", "eta_window"],
}
SYSTEM = """You read one email and extract parcel-delivery facts as JSON. Use only facts stated in the email.
- is_shipping: true only if the email is about an order or parcel the recipient will receive (order confirmation,
  shipped, in transit, out for delivery, delivered, failed delivery, ready to collect). Marketing, receipts for
  services, account and security emails are false.
- merchant: the shop or brand that sold the goods (not the carrier), e.g. "Amazon", "AliExpress", "Acme Store". null if unknown.
- item: what was bought, max 6 words, e.g. "USB-C hub". null if the email does not say.
- lines: every product line the email lists, as written: name (the product, e.g. "PLA Basic"), variant (colour, size
  or option exactly as shown, e.g. "Jade White (10100) / Refill / 1 kg", without prices, discounts or sale names; null if none) and quantity (1 if not shown).
  Only goods in this order or parcel, not recommendations or ads. [] if the email lists no products, e.g. a
  carrier email that only names the sender ("your parcel from X").
- tracking_numbers: carrier tracking numbers exactly as written. Not order numbers, not phone numbers.
- order_number: the merchant's order number, or null.
- status: ordered | shipped (label created, not yet with the carrier) | in_transit | out_for_delivery |
  pickup (ready to collect) | delivered | null.
- problem: short text if delivery failed, was delayed, is held, needs a fee or is being returned; else null.
- eta_date: expected delivery date as YYYY-MM-DD (the email was sent on {sent}); null if not stated.
- eta_window: delivery time window as "HH:MM–HH:MM" if stated, else null."""


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:limit] or None


def clean_lines(raw: Any, merchant: str | None = None) -> list[dict]:
    """Validated order lines: at most 30, capped strings, quantity 1-99. A "line" that is only the sender's name
    ("Acme", "Ali Express parcel": carrier emails name no products) is dropped."""
    out = []
    shop = re.sub(r"\W|parcel|package", "", (merchant or "").lower())
    for ln in raw if isinstance(raw, list) else []:
        if not isinstance(ln, dict) or not (name := _clean(ln.get("name"), 80)):
            continue
        bare = re.sub(r"\W|parcel|package", "", name.lower())
        if not bare or (shop and bare == shop and not ln.get("variant")):
            continue
        try:
            qty = min(99, max(1, int(ln.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        out.append({"name": name, "variant": _clean(ln.get("variant"), 80), "qty": qty})
    return out[:30]


def _body(ex: Extraction, text: str) -> tuple[str, str]:
    sent = ex.sent_at.date().isoformat() if ex.sent_at else "unknown"
    return sent, f"From: {ex.sender}\nSubject: {ex.subject}\nSent: {sent}\n\n{text[:MAX_TEXT]}"


def parse_json_answer(reply: Any) -> dict:
    """The JSON object in a model's reply: plain, or wrapped in ``` fences / chatter. Raises ValueError otherwise."""
    if isinstance(reply, dict):
        return reply
    if not isinstance(reply, str):
        raise ValueError("no text in the AI reply")
    text = reply.strip()
    if fence := re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I):
        text = fence.group(1).strip()
    try:
        answer = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("the AI reply holds no JSON object") from None
        answer = json.loads(text[start:end + 1])
    if not isinstance(answer, dict):
        raise ValueError("the AI reply is not a JSON object")
    return answer


async def extract_ollama(session: aiohttp.ClientSession, url: str, api_key: str | None, ex: Extraction, text: str,
                         model: str = MODEL) -> Extraction:
    """Ask an Ollama server; return a new Extraction combining the rules' result `ex` with the model's answer."""
    sent, body = _body(ex, text)
    payload = {"model": model, "stream": False, "think": False, "format": SCHEMA,
               "options": {"temperature": 0, "num_ctx": 8192},
               "messages": [{"role": "system", "content": SYSTEM.replace("{sent}", sent)},
                            {"role": "user", "content": body}]}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with session.post(url.rstrip("/") + "/api/chat", json=payload, headers=headers, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        answer = parse_json_answer((await resp.json())["message"]["content"])
    return merge_answer(ex, answer, text)


def ai_task_instructions(ex: Extraction, text: str) -> str:
    sent, body = _body(ex, text)
    return (SYSTEM.replace("{sent}", sent) + "\n\nReply with one JSON object only (no commentary) matching this JSON "
            "schema:\n" + json.dumps(SCHEMA) + "\n\nThe email:\n" + body)


async def extract_ai_task(hass: Any, entity_id: str, ex: Extraction, text: str) -> Extraction:
    """Ask a Home Assistant AI Task entity (`ai_task.generate_data`)."""
    result = await hass.services.async_call(
        "ai_task", "generate_data",
        {"task_name": "parcels", "entity_id": entity_id, "instructions": ai_task_instructions(ex, text)},
        blocking=True, return_response=True)
    reply = (result or {}).get("data") if isinstance(result, dict) else None
    return merge_answer(ex, parse_json_answer(reply), text)


def merge_answer(ex: Extraction, answer: dict, text: str) -> Extraction:
    """Validate the AI's `answer` against the email `text` and merge it into a copy of the rules' result `ex`."""
    out = Extraction(**{**ex.__dict__, "tracking": list(ex.tracking)})
    out.is_shipping = bool(answer.get("is_shipping")) or bool(ex.carrier and ex.tracking)
    haystack = normalise_tracking(text)
    for t in answer.get("tracking_numbers") or []:
        t = normalise_tracking(str(t))
        if 8 <= len(t) <= 30 and t in haystack and t not in out.tracking and t != (ex.order or ""):
            out.tracking.append(t)
    merchant = _clean(answer.get("merchant"), 40)
    same = merchant and ex.merchant and re.sub(r"\W", "", merchant).lower() == re.sub(r"\W", "", ex.merchant).lower()
    out.merchant = ex.merchant if (same or not merchant) else merchant
    out.item = _clean(answer.get("item"), 60)
    out.lines = clean_lines(answer.get("lines"), out.merchant or ex.merchant)
    out.order = ex.order or _clean(answer.get("order_number"), 30)
    status = answer.get("status")
    if status in STATUS_RANK and not ex.status:    # the keyword match is more literal; the model only fills gaps
        out.status = status
    out.problem = ex.problem or _clean(answer.get("problem"), 60)
    if not ex.eta_date and answer.get("eta_date"):
        try:
            out.eta_date = date.fromisoformat(answer["eta_date"])
        except ValueError:
            pass
    win = _clean(answer.get("eta_window"), 20)
    if not ex.eta_window and win and re.fullmatch(r"\d{2}:\d{2}[–-]\d{2}:\d{2}", win):
        out.eta_window = win.replace("-", "–")
    return out


async def online(session: aiohttp.ClientSession, url: str, api_key: str | None, model: str = MODEL) -> bool:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with session.get(url.rstrip("/") + "/api/tags", headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return False
            return model_available(await r.json(), model)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return False


def model_available(tags: Any, model: str) -> bool:
    """Is `model` in an Ollama /api/tags answer? "name" matches "name:latest"."""
    wanted = model if ":" in model else model + ":latest"
    names = {m.get("name") for m in (tags or {}).get("models", []) if isinstance(m, dict)} if isinstance(tags, dict) else set()
    return model in names or wanted in names
