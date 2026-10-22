"""Search box for the transaction browser."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EnableBankingSearchText(entry.runtime_data)])


class EnableBankingSearchText(EnableBankingEntryEntity, TextEntity):
    """Words or an amount to look for; empty shows everything. Local only."""

    _attr_name = "Rechercher une opération"
    _attr_icon = "mdi:magnify"
    _attr_native_min = 0
    _attr_native_max = 100

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "browse_search")

    @property
    def native_value(self) -> str:
        return self.coordinator.browse_search

    async def async_set_value(self, value: str) -> None:
        self.coordinator.browse_set_search(value)
