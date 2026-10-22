"""Constants for the Enable Banking integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "enablebanking"

CONF_JWT: Final = "jwt"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_APP_ID: Final = "app_id"
CONF_SESSION_ID: Final = "session_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ASPSP_NAME: Final = "aspsp_name"
CONF_ASPSP_COUNTRY: Final = "aspsp_country"
CONF_PSU_TYPE: Final = "psu_type"
CONF_AUTH_CODE: Final = "auth_code"
CONF_CONSENT_EXPIRES_AT: Final = "consent_expires_at"
# Options: the user's own categorisation rules, one "KEYWORD = Category" per line.
CONF_CUSTOM_RULES: Final = "custom_rules"

# Transaction browser periods: key -> label shown in the select.
BROWSE_PERIODS: Final = {
    "today": "Aujourd'hui",
    "7d": "7 derniers jours",
    "30d": "30 derniers jours",
    "month": "Ce mois-ci",
    "last_month": "Mois dernier",
    "3m": "3 derniers mois",
    "year": "Cette année",
    "all": "Tout",
}
DEFAULT_BROWSE_PERIOD: Final = "month"

# Fixed scheduled polling at these local hours > four polls/day, aligned
# with typical waking life, sitting exactly at the PSD2 4/day cap with
# regular 4-hour gaps (plus one 12-hour overnight gap).
POLL_HOURS: Final = (10, 14, 18, 22)

# Legacy / unused constants kept to avoid breaking imports in older
# user automations referencing scan_interval. Scheduled polling ignores
# these; the only live use is as the nominal interval that the sensor's
# `stale` attribute is measured against (2x this, so 16 h).
DEFAULT_SCAN_INTERVAL: Final = 8 * 60 * 60

# Sensor staleness threshold as originally designed: flag `stale: true` if the
# last successful poll is more than this many hours old, which allows for the
# 12-hour overnight gap plus slack. Currently NOT wired up: sensor._is_stale
# compares against 2x DEFAULT_SCAN_INTERVAL (16 h) instead. Kept so the intended
# figure is not lost; deciding between 16 h and 24 h is a behaviour change.
STALE_THRESHOLD_HOURS: Final = 24

# Storage (persistent on-disk balance cache, one file per config entry).
STORAGE_VERSION: Final = 1

# Transactions are fetched in the same scheduled poll as balances (after them,
# so a transactions 429 can never cost a balance update).
# First fetch for an account goes back this many days. Most ASPSPs allow 90
# days without a fresh SCA; asking for more can make the call fail outright.
TRANSACTIONS_INITIAL_DAYS: Final = 90
# Later fetches restart this many days before the newest booked transaction,
# to catch late bookings and pending items that became booked.
TRANSACTIONS_OVERLAP_DAYS: Final = 7
# Stored transactions older than this are dropped from the on-disk cache.
TRANSACTIONS_KEEP_DAYS: Final = 400
# How many recent transactions the transactions sensor exposes as an attribute.
TRANSACTIONS_RECENT_COUNT: Final = 10

# Max jitter added to the catch-up poll on HA startup, seconds.
STARTUP_JITTER_SECONDS: Final = 60

ENABLE_BANKING_API_URL: Final = "https://api.enablebanking.com"

# Redirect URL used during the OAuth consent flow.
# After authorising at the bank the user is sent here; they copy the
# ?code= query parameter and paste it into the config flow.
REDIRECT_URL: Final = "https://enablebanking.com/"

PSU_PERSONAL: Final = "personal"
PSU_BUSINESS: Final = "business"

CONSENT_WARNING_DAYS: Final = 14
