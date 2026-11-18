"""parcels.test_email and parcels.reload_rules inside Home Assistant."""
import os

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.parcels.const import DOMAIN


async def test_test_email_returns_extraction(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.services.async_call(
        DOMAIN, "test_email",
        {"subject": "Out for delivery", "sender": "Evri <do-not-reply@evri.com>",
         "body": "Tracking number: H09XYZ0000000042\nArriving Friday 6 November", "date": "2026-11-05T10:00:00+00:00"},
        blocking=True, return_response=True)
    assert result["status"] == "out_for_delivery"
    assert result["carrier"] == "evri" and result["carrier_name"] == "Evri"
    assert result["tracking"] == ["H09XYZ0000000042"]
    assert result["eta_date"] == "2026-11-06"


async def test_reload_rules_applies_user_file_and_reports_errors(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    path = hass.config.path("parcels_rules.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("status:\n  delivered: ['\\bgeliefert\\b']\n")
    await hass.services.async_call(DOMAIN, "reload_rules", {}, blocking=True)
    call = {"subject": "Paket", "sender": "a@b.de", "body": "Ihr Paket wurde geliefert"}
    result = await hass.services.async_call(DOMAIN, "test_email", call, blocking=True, return_response=True)
    assert result["status"] == "delivered"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "rules_invalid") is None

    with open(path, "w", encoding="utf-8") as f:
        f.write("status:\n  delivered: ['bad(']\n")
    await hass.services.async_call(DOMAIN, "reload_rules", {}, blocking=True)
    issue = registry.async_get_issue(DOMAIN, "rules_invalid")
    assert issue is not None and "bad(" in issue.translation_placeholders["error"]
    result = await hass.services.async_call(DOMAIN, "test_email", call, blocking=True, return_response=True)
    assert result["status"] == "delivered"                      # previous rules stay active
    os.remove(path)                                             # module state is shared: back to the built-ins
    await hass.services.async_call(DOMAIN, "reload_rules", {}, blocking=True)
    assert registry.async_get_issue(DOMAIN, "rules_invalid") is None
