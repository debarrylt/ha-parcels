"""Constants for the parcels integration."""
from datetime import timedelta

DOMAIN = "parcels"
SIGNAL_UPDATE = "parcels_update_entities"
EVENT = "parcels_update"          # bus event: kind = new | out_for_delivery | pickup | delivered | problem | follow_up
SLOTS = 8                          # fixed rows for dashboards
STORE_KEY = "parcels.store"
STORE_VERSION = 1

SCAN_INTERVAL = timedelta(minutes=5)
AI_CHECK_INTERVAL = timedelta(seconds=60)
TRACK_INTERVAL = timedelta(minutes=15)
HOUSEKEEPING_INTERVAL = timedelta(hours=1)
CARRIER_FIX_AFTER = timedelta(minutes=30)   # 17TRACK still "Not Found": set the carrier explicitly
EMAIL_RECORD_DAYS = 60
FIRST_RUN_DAYS = timedelta(days=30)   # first scan: only mail from the last 30 days
LAST_MILE_EVERY = timedelta(hours=3)        # Ship24 lookup per export parcel (last-mile number)
SHIP24_URL = "https://api.ship24.com/public/v1"              # Ship24 API (key in the Parcels settings)

# --- configuration keys (entry.data = credentials, entry.options = the rest) ---------------------------------------
CONF_IMAP_ENTRY = "imap_entry_id"      # entry_id of the Home Assistant `imap` config entry that provides the mailbox
CONF_T17_USERNAME = "seventeentrack_username"
CONF_T17_PASSWORD = "seventeentrack_password"
CONF_SHIP24_KEY = "ship24_api_key"
CONF_OLLAMA_KEY = "ollama_api_key"
CONF_AI_MODE = "ai_mode"
CONF_AI_ENTITY = "ai_task_entity"
CONF_OLLAMA_URL = "ollama_url"
CONF_OLLAMA_MODEL = "ollama_model"
CONF_HOME_POSTCODE = "home_postcode"
CONF_HOME_NAMES = "home_names"

DATA_KEYS = (CONF_IMAP_ENTRY, CONF_T17_USERNAME, CONF_T17_PASSWORD, CONF_SHIP24_KEY, CONF_OLLAMA_KEY)
OPTION_KEYS = (CONF_AI_MODE, CONF_AI_ENTITY, CONF_OLLAMA_URL, CONF_OLLAMA_MODEL, CONF_HOME_POSTCODE, CONF_HOME_NAMES)
SECRET_KEYS = (CONF_T17_PASSWORD, CONF_SHIP24_KEY, CONF_OLLAMA_KEY)

AI_NONE, AI_TASK, AI_OLLAMA = "none", "ai_task", "ollama"
DEFAULT_OLLAMA_MODEL = "qwen3:30b"
FLOW_VERSION = 1          # same as the first private version, so existing entries load without a migration; legacy = empty data


def uses_own_credentials(data) -> bool:
    """True for entries made by the config flow : they name the `imap` entry to read and keep their own
    17TRACK, Ship24 and AI settings. A legacy entry has empty data and borrows the Home Assistant `imap`,
    `ollama` and `seventeentrack` integrations' config entries instead."""
    return CONF_IMAP_ENTRY in data
