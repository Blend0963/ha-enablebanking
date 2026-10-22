"""Monthly budget per spending category (restored across restarts)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import CURRENCY_EURO
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .categorize import DEFAULT_CATEGORIES, NON_EXPENSE_CATEGORIES
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EnableBankingBudgetNumber(coordinator, slug, name)
        for slug, name in coordinator.categorizer.categories.items()
        if slug not in NON_EXPENSE_CATEGORIES
    )


class EnableBankingBudgetNumber(EnableBankingEntryEntity, RestoreNumber):
    """Monthly budget for one category, in euros. 0 means no budget."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = CURRENCY_EURO

    def __init__(self, coordinator: EnableBankingCoordinator, slug: str, name: str) -> None:
        super().__init__(coordinator, f"budget_{slug}")
        self._slug = slug
        self._attr_name = f"Budget {name}"
        default = DEFAULT_CATEGORIES.get(slug)
        self._attr_icon = default[1] if default else "mdi:tag"
        self._value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._value = float(last.native_value)
        self.coordinator.set_budget(self._slug, self._value)

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.coordinator.set_budget(self._slug, value)
        self.async_write_ha_state()
