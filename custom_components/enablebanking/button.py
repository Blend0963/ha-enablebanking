"""Previous / next buttons for the transaction browser."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            EnableBankingBrowseButton(coordinator, step=-1),
            EnableBankingBrowseButton(coordinator, step=1),
        ]
    )


class EnableBankingBrowseButton(EnableBankingEntryEntity, ButtonEntity):
    """Move the transaction browser one step (no bank call)."""

    def __init__(self, coordinator: EnableBankingCoordinator, *, step: int) -> None:
        super().__init__(coordinator, "browse_older" if step > 0 else "browse_newer")
        self._step = step
        # Newest first: "next" goes back in time.
        self._attr_name = "Opération suivante" if step > 0 else "Opération précédente"
        self._attr_icon = "mdi:chevron-right" if step > 0 else "mdi:chevron-left"

    async def async_press(self) -> None:
        self.coordinator.browse_move(self._step)
