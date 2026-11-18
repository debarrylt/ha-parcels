"""Import the integration as `parcels_core` by path, so its pure modules load without Home Assistant."""
import sys
import types
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "parcels"
if "parcels_core" not in sys.modules:
    pkg = types.ModuleType("parcels_core")
    pkg.__path__ = [str(PKG)]          # skips the HA-dependent __init__
    sys.modules["parcels_core"] = pkg
