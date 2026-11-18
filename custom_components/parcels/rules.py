"""Country / language rules of the email parser: loading, merging, validation (no Home Assistant imports).

The data lives in `rules.yaml` (built in) and optionally in the user's `parcels_rules.yaml`. See the README section
"Adapting the rules to your country". `load_rules()` merges the two, checks every entry and compiles the regexes once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import yaml

DEFAULT_PATH = Path(__file__).with_name("rules.yaml")
STATUSES = ("ordered", "shipped", "in_transit", "out_for_delivery", "pickup", "delivered")


class RulesError(Exception):
    """A rules file is wrong. The message names the file, the section and the entry."""


class Merchant(NamedTuple):
    name: str
    order_page: str | None
    order_history_page: str | None
    fix_spelling: bool
    name_starts_with: bool


# --- schema ---------------------------------------------------------------------------------------------------------
# kind -> checked by _check_section. "regexes" = list of regexes (flags: I = ignore case, M = ^/$ per line, "" = none).
# "pieces" = list of regex fragments (joined by the program), "strings" = list of plain strings.
_LISTS: dict[str, tuple[str, str]] = {
    "generic_tracking": ("regexes", ""), "tracking_labels": ("pieces", "I"), "order_labels": ("pieces", "I"),
    "platform_domains": ("strings", ""), "webmail_domains": ("strings", ""), "two_part_suffixes": ("strings", ""),
    "merchant_patterns": ("regexes", "M"), "not_merchant_words": ("strings", ""),
    "to_be_delivered": ("regexes", "I"), "future_clause": ("regexes", "I"), "progress_bar": ("regexes", "I"),
    "shipping_hint": ("regexes", "I"), "forward_prefixes": ("pieces", "I"),
    "ignored_link_words": ("regexes", "I"), "track_link_labels": ("regexes", "I"),
    "order_link_labels": ("regexes", "I"), "order_link_paths": ("regexes", "I"),
    "weekdays": ("strings", ""), "months": ("strings", ""), "ordinal_suffixes": ("pieces", "I"),
    "eta_context": ("pieces", "I"), "eta_today": ("pieces", "I"), "eta_tomorrow": ("pieces", "I"),
    "eta_weekday_cues": ("pieces", "I"), "time_window_joiners": ("pieces", "I"),
    "ship_to_labels": ("pieces", "I"), "ship_to_stop_words": ("strings", ""),
    "ship_to_address_book": ("regexes", ""), "postcode": ("regexes", ""),
}
_STATUS_MAPS = ("subject_status", "status")         # status -> list of regexes (ignore case)
_PROBLEMS = "problems"                              # problem text -> list of regexes (ignore case)
_STRUCTS: dict[str, dict[str, type | tuple]] = {
    "carriers": {"name": str, "code_17track": (int, type(None)), "domains": list, "tracking": list, "page": str},
    "merchants": {"name": str, "order_page": str, "order_history_page": str, "fix_spelling": bool,
                  "name_starts_with": bool},
}
SECTIONS = set(_LISTS) | set(_STATUS_MAPS) | {_PROBLEMS} | set(_STRUCTS)
_FLAGS = {"I": re.I, "M": re.M, "": 0}


def _where(source: str, section: str, entry: str | None = None) -> str:
    return f"{source}: section '{section}'" + (f", entry '{entry}'" if entry is not None else "")


def _check_regex(value: Any, flags: str, source: str, section: str, entry: str | None, fragment: bool = False) -> None:
    if not isinstance(value, str):
        raise RulesError(f"{_where(source, section, entry)}: expected text, got {value!r}")
    try:
        re.compile(value, _FLAGS[flags])
    except re.error as err:
        raise RulesError(f"{_where(source, section, entry)}: bad regex {value!r}: {err}") from err


def _check_list(value: Any, kind: str, flags: str, source: str, section: str, entry: str | None = None) -> None:
    if not isinstance(value, list):
        raise RulesError(f"{_where(source, section, entry)}: expected a list, got {type(value).__name__}")
    for item in value:
        if kind == "strings":
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise RulesError(f"{_where(source, section, entry)}: expected text, got {item!r}")
        else:
            _check_regex(item, flags, source, section, entry)


def _check_section(name: str, value: Any, source: str, merged: bool) -> None:
    """Check one section's shape (and compile its regexes). In a user file (`merged` false) None values mean "remove"."""
    if name in _LISTS:
        kind, flags = _LISTS[name]
        _check_list(value, kind, flags, source, name)
        return
    if not isinstance(value, dict):
        raise RulesError(f"{_where(source, name)}: expected a mapping, got {type(value).__name__}")
    for key, item in value.items():
        if item is None and not merged:
            continue
        if name in _STATUS_MAPS:
            if key not in STATUSES:
                raise RulesError(f"{_where(source, name, key)}: unknown status (use one of {', '.join(STATUSES)})")
            _check_list(item, "regexes", "I", source, name, key)
        elif name == _PROBLEMS:
            _check_list(item, "regexes", "I", source, name, key)
        else:
            fields = _STRUCTS[name]
            if not isinstance(item, dict):
                raise RulesError(f"{_where(source, name, key)}: expected a mapping of fields, got {item!r}")
            for fname, fvalue in item.items():
                if fname not in fields:
                    raise RulesError(f"{_where(source, name, key)}: unknown field '{fname}' (known: {', '.join(fields)})")
                if fvalue is None:
                    continue
                if not isinstance(fvalue, fields[fname]) or (fields[fname] is int and isinstance(fvalue, bool)):
                    raise RulesError(f"{_where(source, name, key)}: field '{fname}' has the wrong type: {fvalue!r}")
                if fname in ("domains", "tracking"):
                    _check_list(fvalue, "strings" if fname == "domains" else "regexes", "", source, name, key)
                if fname in ("page", "order_page") and "{}" not in fvalue:
                    raise RulesError(f"{_where(source, name, key)}: field '{fname}' must contain {{}} for the number")
            if merged and not isinstance(item.get("name"), str):
                raise RulesError(f"{_where(source, name, key)}: missing field 'name'")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise RulesError(f"{path}: cannot read the file: {err}") from err
    except yaml.YAMLError as err:
        raise RulesError(f"{path}: invalid YAML: {err}") from err
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RulesError(f"{path}: the file must be a mapping of sections (carriers:, status:, ...)")
    return data


def _merge_value(base: Any, extra: Any, source: str, where: str) -> Any:
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for key, value in extra.items():
            if value is None:
                out.pop(key, None)
            elif key in out:
                out[key] = _merge_value(out[key], value, source, f"{where}.{key}")
            else:
                out[key] = value
        return out
    if isinstance(base, list) and isinstance(extra, list):
        return base + extra
    return extra          # a scalar (or a value of another type: the section check reports it)


def merge(default: dict[str, Any], user: dict[str, Any], source: str) -> dict[str, Any]:
    """Merge the user's data into the defaults: mappings by key (null removes), lists appended, `replace` sections whole."""
    replace = user.get("replace") or []
    if not isinstance(replace, list) or not all(isinstance(x, str) for x in replace):
        raise RulesError(f"{source}: 'replace' must be a list of section names")
    for name in replace:
        if name not in SECTIONS:
            raise RulesError(f"{source}: 'replace' names an unknown section '{name}'")
    out = dict(default)
    for name, value in user.items():
        if name == "replace":
            continue
        if name not in SECTIONS:
            raise RulesError(f"{source}: unknown section '{name}' (known: {', '.join(sorted(SECTIONS))})")
        if name in replace:
            out[name] = {k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value
        else:
            out[name] = _merge_value(out.get(name), value, source, name)
    for name in replace:
        if name not in user:
            raise RulesError(f"{source}: 'replace' lists '{name}' but the file has no such section")
    return out


# --- compiled rules -------------------------------------------------------------------------------------------------
def _alt(items: list[str]) -> str:
    return "|".join(f"(?:{x})" for x in items) if items else "(?!)"


@dataclass
class Rules:
    carriers: dict[str, tuple[str, int | None, tuple[str, ...]]]      # key -> (name, 17TRACK code, sender domains)
    tracking: dict[str | None, list[str]]                             # carrier -> number regexes (None = every carrier)
    tracking_re: dict[str | None, list[re.Pattern]]
    carrier_pages: dict[str, str]
    merchants: dict[str, Merchant]                                    # sender domain -> shop
    platform_domains: tuple[str, ...]
    webmail_domains: frozenset[str]
    two_part_suffixes: tuple[str, ...]
    merchant_patterns: list[re.Pattern]
    not_merchant_words: frozenset[str]
    subject_status: list[tuple[re.Pattern, str]]
    status: list[tuple[re.Pattern, str]]
    problems: list[tuple[re.Pattern, str]]
    to_be_delivered: re.Pattern
    future_clause: re.Pattern
    progress_bar: re.Pattern
    shipping_hint: re.Pattern
    forward_prefix: re.Pattern
    labelled: re.Pattern                                              # "Tracking number: X"
    order: re.Pattern
    not_a_parcel_link: re.Pattern
    track_label: re.Pattern
    order_label: re.Pattern
    order_path: re.Pattern
    weekdays: list[str]
    months: list[str]
    eta_context: re.Pattern
    eta_today: re.Pattern
    eta_tomorrow: re.Pattern
    eta_weekday: re.Pattern
    date_patterns: list[tuple[re.Pattern, int, int]]                 # (pattern, day group, month group)
    time_window: re.Pattern
    ship_to: re.Pattern
    ship_to_address_book: list[re.Pattern]
    postcodes: list[re.Pattern]
    sources: list[str] = field(default_factory=list)                  # files this was loaded from

    def merchant_for_name(self, name: str | None, need: str = "order_page") -> Merchant | None:
        """The shop entry (with the page `need`) for a shop's display name, ignoring case, spaces and punctuation."""
        norm = re.sub(r"[^a-z]", "", (name or "").lower())
        if not norm:
            return None
        for m in self.merchants.values():
            if not getattr(m, need):
                continue
            mine = re.sub(r"[^a-z]", "", m.name.lower())
            if norm == mine or (m.name_starts_with and norm.startswith(mine)):
                return m
        return None


def _compile(data: dict[str, Any], sources: list[str]) -> Rules:
    def comp(items: list[str], flags: str = "I") -> re.Pattern:
        return re.compile(_alt(items), _FLAGS[flags])

    carriers, tracking, pages = {}, {}, {}
    for key, c in data["carriers"].items():
        carriers[key] = (c["name"], c.get("code_17track"), tuple(str(d).lower() for d in c.get("domains") or []))
        if c.get("tracking"):
            tracking[key] = list(c["tracking"])
        if c.get("page"):
            pages[key] = c["page"]
    tracking[None] = list(data["generic_tracking"])
    merchants = {str(dom).lower(): Merchant(m["name"], m.get("order_page"), m.get("order_history_page"),
                                            bool(m.get("fix_spelling")), bool(m.get("name_starts_with")))
                 for dom, m in data["merchants"].items()}
    months, weekdays = [str(x).lower() for x in data["months"]], [str(x).lower() for x in data["weekdays"]]
    if len(weekdays) != 7:
        raise RulesError(f"{sources[-1]}: section 'weekdays' needs 7 names (Monday first), has {len(weekdays)}; "
                         "to change language use replace: [weekdays]")
    if len(months) != 12:
        raise RulesError(f"{sources[-1]}: section 'months' needs 12 names (January first), has {len(months)}; "
                         "to change language use replace: [months]")
    month_word = "(?:" + "|".join(re.escape(x[:3]) for x in months) + r")[^\W\d_]*"
    ordinal = f"(?:{_alt(data['ordinal_suffixes'])})?" if data["ordinal_suffixes"] else ""
    weekday_alt = "|".join(re.escape(w[:3]) + (f"(?:{re.escape(w[3:])})?" if w[3:] else "") for w in weekdays)
    stop = "|".join(re.escape(x) for x in data["ship_to_stop_words"]) or "(?!)"
    return Rules(
        carriers=carriers, tracking=tracking,
        tracking_re={k: [re.compile(p) for p in v] for k, v in tracking.items()}, carrier_pages=pages,
        merchants=merchants, platform_domains=tuple(str(x).lower() for x in data["platform_domains"]),
        webmail_domains=frozenset(str(x).lower() for x in data["webmail_domains"]),
        two_part_suffixes=tuple(str(x).lower() for x in data["two_part_suffixes"]),
        merchant_patterns=[re.compile(p, re.M) for p in data["merchant_patterns"]],
        not_merchant_words=frozenset(str(x).lower() for x in data["not_merchant_words"]),
        subject_status=[(re.compile(p, re.I), s) for s, ps in data["subject_status"].items() for p in ps],
        status=[(re.compile(p, re.I), s) for s, ps in data["status"].items() for p in ps],
        problems=[(re.compile(p, re.I), text) for text, ps in data["problems"].items() for p in ps],
        to_be_delivered=comp(data["to_be_delivered"]), future_clause=comp(data["future_clause"]),
        progress_bar=comp(data["progress_bar"]), shipping_hint=comp(data["shipping_hint"]),
        forward_prefix=re.compile(r"^\s*(?:" + _alt(data["forward_prefixes"]) + r"):\s*", re.I),
        labelled=re.compile(r"(?:" + _alt(data["tracking_labels"]) + r")\s*[:#*]*\s*\**\s*"
                            r"([A-Z0-9][A-Z0-9 -]{6,30}[A-Z0-9])", re.I),
        order=re.compile(r"\b(?:" + _alt(data["order_labels"]) + r")\s*(?:#\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{2,28}[A-Z0-9])|"
                         r"[:#]?\s*\**\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,28}[A-Z0-9]))\b", re.I),
        not_a_parcel_link=comp(data["ignored_link_words"]), track_label=comp(data["track_link_labels"]),
        order_label=comp(data["order_link_labels"]), order_path=comp(data["order_link_paths"]),
        weekdays=weekdays, months=months,
        eta_context=comp(data["eta_context"]),
        eta_today=re.compile(r"\b(?:" + _alt(data["eta_today"]) + r")\b", re.I),
        eta_tomorrow=re.compile(r"\b(?:" + _alt(data["eta_tomorrow"]) + r")\b", re.I),
        eta_weekday=re.compile(r"\b(?:" + _alt(data["eta_weekday_cues"]) + r")\b[^.\n]{0,40}?\b(" + weekday_alt + r")\b", re.I),
        date_patterns=[(re.compile(r"\b(\d{1,2})" + ordinal + r"\s+(" + month_word + r")\b", re.I), 1, 2),
                       (re.compile(r"\b(" + month_word + r")\.?\s+(\d{1,2})" + ordinal + r"\b", re.I), 2, 1)],
        time_window=re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(?:am|pm)?\s*(?:" + _alt(data["time_window_joiners"]) +
                               r")\s*(\d{1,2})[:.](\d{2})\s*(am|pm)?", re.I),
        ship_to=re.compile(r"\b(?:" + _alt(data["ship_to_labels"]) + r")\s*:\s*(.{3,160}?)"
                           r"(?=\s+(?:" + stop + r")\b|\n\s*\n|$)", re.I | re.S),
        ship_to_address_book=[re.compile(p) for p in data["ship_to_address_book"]],
        postcodes=[re.compile(p) for p in data["postcode"]], sources=sources)


def load_rules(override_path: str | Path | None = None) -> Rules:
    """The built-in rules, merged with the user's file when `override_path` exists. Raises RulesError."""
    data = _read(DEFAULT_PATH)
    for name, value in data.items():
        _check_section(name, value, str(DEFAULT_PATH), True)
    sources = [str(DEFAULT_PATH)]
    if override_path is not None and Path(override_path).is_file():
        path = Path(override_path)
        user = _read(path)
        for name, value in user.items():
            if name != "replace" and name in SECTIONS:
                _check_section(name, value, str(path), False)
        data = merge(data, user, str(path))
        for name in user:
            if name in SECTIONS:      # again after the merge: a replaced section must be complete
                _check_section(name, data[name], str(path), True)
        sources.append(str(path))
    return _compile(data, sources)
