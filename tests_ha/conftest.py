"""Tests run inside a real Home Assistant (pytest-homeassistant-custom-component), in .venv-ha."""
import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
