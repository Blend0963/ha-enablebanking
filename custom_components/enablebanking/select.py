"""Selects for the transaction browser: category filter, period, reclassify."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .categorize import CAT_INTERNAL
from .const import BROWSE_PERIODS
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntryEntity

ALL_CATEGORIES = "Toutes"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            EnableBankingBrowseCategorySelect(coordinator),
            EnableBankingBrowsePeriodSelect(coordinator),
            EnableBankingClassifySelect(coordinator),
        ]
    )


class EnableBankingBrowseCategorySelect(EnableBankingEntryEntity, SelectEntity):
    """Which category the transaction browser shows."""

    _attr_name = "Filtre des opérations"
    _attr_icon = "mdi:filter-variant"

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "browse_category")

    @property
    def options(self) -> list[str]:
        return [ALL_CATEGORIES, *self.coordinator.categorizer.categories.values()]

    @property
    def current_option(self) -> str | None:
        slug = self.coordinator.browse_category
        if not slug:
            return ALL_CATEGORIES
        return self.coordinator.categorizer.categories.get(slug, ALL_CATEGORIES)

    async def async_select_option(self, option: str) -> None:
        by_name = {name: slug for slug, name in self.coordinator.categorizer.categories.items()}
        self.coordinator.browse_set_category(by_name.get(option, ""))


class EnableBankingBrowsePeriodSelect(EnableBankingEntryEntity, SelectEntity):
    """Which period the transaction browser shows."""

    _attr_name = "Période des opérations"
    _attr_icon = "mdi:calendar-range"

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "browse_period")
        self._attr_options = list(BROWSE_PERIODS.values())

    @property
    def current_option(self) -> str | None:
        return BROWSE_PERIODS.get(self.coordinator.browse_period)

    async def async_select_option(self, option: str) -> None:
        by_label = {label: key for key, label in BROWSE_PERIODS.items()}
        if option in by_label:
            self.coordinator.browse_set_period(by_label[option])


class EnableBankingClassifySelect(EnableBankingEntryEntity, SelectEntity):
    """Category of the selected transaction; changing it teaches a rule.

    Picking a category stores ``MERCHANT = Category`` in the entry's rules
    (the same list as the Configure dialog), so every past and future
    transaction of that merchant follows. Nothing is sent to the bank.
    """

    _attr_name = "Classer l'opération"
    _attr_icon = "mdi:tag-arrow-right-outline"

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "classify_selected")

    def _selected_keyword(self) -> str:
        items = self.coordinator.browse_items()
        if not items:
            return ""
        item = items[min(self.coordinator.browse_index, len(items) - 1)]
        return item.merchant or ""

    @property
    def options(self) -> list[str]:
        return [
            name
            for slug, name in self.coordinator.categorizer.categories.items()
            if slug != CAT_INTERNAL
        ] + [self.coordinator.categorizer.categories[CAT_INTERNAL]]

    @property
    def current_option(self) -> str | None:
        items = self.coordinator.browse_items()
        if not items:
            return None
        item = items[min(self.coordinator.browse_index, len(items) - 1)]
        return self.coordinator.categorizer.categories.get(item.category)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"keyword": self._selected_keyword()}

    async def async_select_option(self, option: str) -> None:
        keyword = self._selected_keyword()
        if keyword and option != self.current_option:
            self.coordinator.add_rule(keyword, option)
