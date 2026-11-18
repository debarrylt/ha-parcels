"""Rules loading, merging, validation, and the French example (pure Python)."""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "parcels"
if "parcels_core" not in sys.modules:
    pkg = types.ModuleType("parcels_core")
    pkg.__path__ = [str(PKG)]
    sys.modules["parcels_core"] = pkg
from parcels_core import parser as P  # noqa: E402
from parcels_core import rules as R  # noqa: E402

FRENCH = ROOT / "examples" / "parcels_rules.yaml"


@pytest.fixture(autouse=True)
def restore_rules():
    before = P.rules()
    yield
    P.set_rules(before)


def load(tmp_path, text):
    path = tmp_path / "parcels_rules.yaml"
    path.write_text(text, encoding="utf-8")
    return R.load_rules(path)


def test_default_rules_load_without_override(tmp_path):
    base = R.load_rules(None)
    assert R.load_rules(tmp_path / "missing.yaml").carriers == base.carriers      # no file: defaults
    assert base.carriers["royal_mail"][:2] == ("Royal Mail", 11031)
    assert "evri" in base.carrier_pages and None in base.tracking


def test_mapping_merges_by_key_and_user_wins(tmp_path):
    r = load(tmp_path, "carriers:\n  evri: {name: Evri UK}\n  newco: {name: NewCo, domains: [newco.example]}\n")
    assert r.carriers["evri"][0] == "Evri UK"
    assert r.carriers["evri"][2] == ("evri.com", "hermes-europe.co.uk", "myhermes.co.uk")      # other fields kept
    assert r.carriers["newco"] == ("NewCo", None, ("newco.example",))
    assert "royal_mail" in r.carriers


def test_null_removes_a_default_key(tmp_path):
    r = load(tmp_path, "carriers:\n  fedex: null\nmerchants:\n  ebay.com: null\n")
    assert "fedex" not in r.carriers and "fedex" not in r.carrier_pages
    assert "ebay.com" not in r.merchants and "ebay.co.uk" in r.merchants


def test_lists_are_appended(tmp_path):
    base = R.load_rules(None)
    r = load(tmp_path, "carriers:\n  royal_mail: {domains: [royalmail.example]}\nplatform_domains: [shop.example]\n"
                       "status:\n  delivered: ['\\bgeliefert\\b']\n")
    assert r.carriers["royal_mail"][2] == base.carriers["royal_mail"][2] + ("royalmail.example",)
    assert r.platform_domains == base.platform_domains + ("shop.example",)
    assert len(r.status) == len(base.status) + 1
    P.set_rules(r)
    assert P.extract("Paket geliefert", "x@y.de", "Ihr Paket wurde geliefert", None).status == "delivered"


def test_replace_swaps_a_section(tmp_path):
    r = load(tmp_path, "replace: [weekdays, platform_domains]\nplatform_domains: [only.example]\n"
                       "weekdays: [a1, b2, c3, d4, e5, f6, g7]\n")
    assert r.platform_domains == ("only.example",) and r.weekdays[0] == "a1"
    assert len(r.months) == 12                                    # untouched section stays


def test_replace_needs_complete_section(tmp_path):
    with pytest.raises(R.RulesError, match=r"section 'weekdays'.*7 names"):
        load(tmp_path, "replace: [weekdays]\nweekdays: [lundi, mardi]\n")
    with pytest.raises(R.RulesError, match="months"):          # appended to the 12 English names: 13
        load(tmp_path, "months: [extra]\n")


def test_bad_regex_names_file_section_and_entry(tmp_path):
    with pytest.raises(R.RulesError) as e:
        load(tmp_path, "status:\n  delivered: ['ok', 'bad(']\n")
    msg = str(e.value)
    assert "parcels_rules.yaml" in msg and "section 'status'" in msg and "entry 'delivered'" in msg and "bad(" in msg
    with pytest.raises(R.RulesError, match=r"section 'carriers', entry 'x'.*bad regex"):
        load(tmp_path, "carriers:\n  x: {name: X, tracking: ['[']}\n")


def test_bad_yaml_and_wrong_types(tmp_path):
    with pytest.raises(R.RulesError, match=r"parcels_rules.yaml: invalid YAML"):
        load(tmp_path, "carriers: [unclosed\n")
    with pytest.raises(R.RulesError, match=r"section 'platform_domains': expected a list"):
        load(tmp_path, "platform_domains: nope\n")
    with pytest.raises(R.RulesError, match=r"unknown section 'carrier'"):
        load(tmp_path, "carrier: {}\n")
    with pytest.raises(R.RulesError, match=r"entry 'newco'.*missing field 'name'"):
        load(tmp_path, "carriers:\n  newco: {domains: [a.example]}\n")
    with pytest.raises(R.RulesError, match=r"unknown status"):
        load(tmp_path, "status:\n  lost: ['x']\n")
    with pytest.raises(R.RulesError, match=r"must contain \{\}"):
        load(tmp_path, "carriers:\n  evri: {page: 'http://x'}\n")


def test_french_example_extracts_status_and_dates(tmp_path):
    r = R.load_rules(FRENCH)
    P.set_rules(r)
    sent = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)
    ex = P.extract("Votre colis est en cours de livraison", "Colissimo <noreply@colissimo.fr>",
                   "Bonjour,\nVotre colis 6A12345678901 sera livré le vendredi 20 novembre.\n", sent)
    assert (ex.carrier, ex.status, ex.tracking) == ("colissimo", "out_for_delivery", ["6A12345678901"])
    assert str(ex.eta_date) == "2026-11-20"
    assert P.extract("Colis livré", "x@mondialrelay.fr", "Votre colis a été livré", sent).status == "delivered"
    assert P.extract("Commande", "x@boutique.fr", "Votre colis a été expédié", sent).status == "in_transit"
    assert P.extract("Colis", "x@mondialrelay.fr", "Votre colis est disponible en point relais", sent).status == "pickup"
    july = P.extract("Colis", "x@chronopost.fr", "Livraison prévue le 3 juillet", datetime(2026, 6, 20, tzinfo=timezone.utc))
    assert str(july.eta_date) == "2026-07-03"                      # juillet, not juin
    assert P.is_home("Rue X, Paris 75011", "75011", None) is True
    assert P.order_page("Amazon", "123") == "https://www.amazon.fr/gp/your-account/order-details?orderID=123"
    # English still works next to French
    assert P.extract("Delivered", "x@evri.com", "Your parcel has been delivered", sent).status == "delivered"


def test_default_example_uk_behaviour_unchanged():
    sent = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
    ex = P.extract("Out for delivery", "x@evri.com", "Your parcel H09XYZ0000000042 arrives Friday 6 November", sent)
    assert (ex.status, ex.tracking, str(ex.eta_date)) == ("out_for_delivery", ["H09XYZ0000000042"], "2026-11-06")
