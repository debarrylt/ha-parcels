"""The example dashboard and the README's YAML: valid, complete for all 8 slots, and in step with the code."""
import re
from pathlib import Path

import yaml

from parcels_core import model as M

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "examples" / "dashboard.yaml").read_text()


def _cards(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _cards(v)
    elif isinstance(x, list):
        for v in x:
            yield from _cards(v)


def test_each_slot_has_its_links_and_number_box_in_the_list_and_on_its_page():
    views = yaml.safe_load(DASH)["views"]
    assert [v["path"] for v in views] == ["parcels"] + [f"parcel-{n}" for n in range(1, 9)]
    for n in range(1, 9):
        for sections in ([s for s in views[0]["sections"] if f"parcels_slot_{n}_received" in str(s)], views[n]["sections"]):
            cards = list(_cards(sections))
            boxes = [c for c in cards if c.get("type") == "conditional"]
            assert len(boxes) == 1 and set(re.findall(r"parcels_slot_(\d)", str(boxes))) == {str(n)}
            text = " ".join(c["content"] for c in cards if c.get("type") == "markdown")
            assert "shop_url" in text and "order_url" in text and "flag_reason" in text
        assert set(re.findall(r"parcels_slot_(\d)", str(views[n]))) == {str(n)}


def test_attributes_used_exist():
    view = set(M.view(M.new_parcel(M.datetime(2026, 11, 1, tzinfo=M.timezone.utc)), M.date(2026, 11, 1)))
    per_slot = DASH.split("parcels_slot_1.attributes", 1)[1].split("sensor.parcels_slot_2")[0]
    assert set(re.findall(r"a\.get\('(\w+)'", per_slot)) <= view | {"emails", "tracking_numbers"}


def test_readme_yaml_is_valid_and_matches_the_flag():
    blocks = re.findall(r"```yaml\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    parsed = [yaml.safe_load(b) for b in blocks]
    assert any(isinstance(b, dict) and b.get("alias") == "Parcel needs a tracking number" for b in parsed)
    assert M.NO_TRACKING_REASON.startswith("No tracking number")


def test_no_template_where_home_assistant_shows_it_as_text():
    """A markdown card's title is not rendered as a template; a folded list of emails becomes one line."""
    cards = list(_cards(yaml.safe_load(DASH)))
    assert not [c["title"] for c in cards if "{{" in str(c.get("title", ""))]
    assert "<h2>" not in DASH          # an HTML block turns Markdown off until a blank line
    emails = [c["content"] for c in cards if c.get("title") == "Emails"]
    assert len(emails) == 8 and all("\n- {{" in e for e in emails)
