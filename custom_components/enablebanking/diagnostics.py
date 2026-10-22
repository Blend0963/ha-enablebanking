"""Diagnostics for Enable Banking.

Settings > Devices & services > Enable Banking > ⋮ > Download diagnostics.

The main purpose is to show what each bank really sends for transactions
(which fields are filled, sample raw objects) so categorisation can be built
on real data. Credentials and account identifiers are redacted.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONF_PSU_TYPE,
)
from .coordinator import EnableBankingConfigEntry
from .transactions import field_coverage

# Raw transactions included per account.
_SAMPLE_SIZE = 30

TO_REDACT = {
    "iban",
    "bban",
    "other",
    "identification",
    "postal_address",
    "jwt",
    "private_key",
    "session_id",
    CONF_APP_ID,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnableBankingConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    accounts: list[dict[str, Any]] = []
    for stable_id in sorted(coordinator.cached_stable_ids()):
        account = coordinator.cached_account(stable_id)
        txs = coordinator.transactions_for(stable_id)
        accounts.append(
            {
                "name": account.name if account else None,
                "product": account.product if account else None,
                "currency": account.currency if account else None,
                "transactions_state": coordinator.transactions_state(stable_id),
                "field_coverage": field_coverage(txs),
                "sample_transactions": async_redact_data(txs[:_SAMPLE_SIZE], TO_REDACT),
            }
        )
    return {
        "entry": {
            "aspsp_name": entry.data.get(CONF_ASPSP_NAME),
            "aspsp_country": entry.data.get(CONF_ASPSP_COUNTRY),
            "psu_type": entry.data.get(CONF_PSU_TYPE),
            "consent_expires_at": entry.data.get(CONF_CONSENT_EXPIRES_AT),
        },
        "last_refresh": coordinator.last_refresh.isoformat() if coordinator.last_refresh else None,
        "last_error": coordinator.last_error,
        "accounts": accounts,
        "categories": dict(Counter(i.category for i in coordinator.categorized)),
        "uncategorized_sample": [
            i.merchant for i in coordinator.categorized if i.category == "autre"
        ][:50],
    }
