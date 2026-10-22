"""Sensor platform for the Enable Banking integration."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import slugify
from homeassistant.util.dt import as_local, start_of_local_day, utcnow

from .categorize import (
    CAT_INCOME,
    CAT_INTERNAL,
    CAT_OTHER,
    DEFAULT_CATEGORIES,
    CategorizedTransaction,
    by_category,
    category_total,
    compact,
    in_month,
    income_total,
    previous_month_start,
    spending_total,
)
from .const import (
    BROWSE_PERIODS,
    CONF_ASPSP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    TRANSACTIONS_RECENT_COUNT,
)
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntity, EnableBankingEntryEntity, account_unique_id
from .models import AccountBalance
from .transactions import field_coverage

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EnableBankingSensorDescription(SensorEntityDescription):
    """Describes an Enable Banking sensor entity."""

    value_fn: Callable[[AccountBalance], StateType] = lambda _: None
    account_attrs_fn: Callable[[AccountBalance], dict[str, Any]] | None = None


BALANCE_SENSOR = EnableBankingSensorDescription(
    key="balance",
    translation_key="balance",
    native_unit_of_measurement=CURRENCY_EURO,
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=2,
    icon="mdi:bank",
    value_fn=lambda acc: round(acc.balance, 2),
    account_attrs_fn=lambda acc: {
        "overdrawn": acc.balance < 0,
        "iban": acc.iban,
        "account_name": acc.name,
        "product": acc.product,
        "currency": acc.currency,
        "balance_type": acc.balance_type,
        "reference_date": acc.reference_date,
    },
)

TRANSACTIONS_SENSOR = SensorEntityDescription(
    key="transactions",
    translation_key="transactions",
    icon="mdi:format-list-bulleted",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enable Banking balance sensors.

    Accounts discovered on a later poll are added without a reload. Accounts
    present only in the on-disk cache are picked up at boot via
    ``coordinator.cached_stable_ids()`` (the cache has already seeded
    ``coordinator.data`` in ``async_load_cache`` by the time we reach here).
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_for_new_accounts() -> None:
        # Re-point any pre-0.6.5 uid-based entities to the stable-id scheme
        # BEFORE adding, so the added entity reuses the existing registry entry
        # (and its entity_id / history) instead of orphaning it.
        _migrate_unique_ids(hass, entry, coordinator)

        new_entities: list[SensorEntity] = []
        seen_ids: set[str] = set()
        if coordinator.data is not None:
            seen_ids.update(coordinator.data.accounts.keys())
        # coordinator.data only carries the accounts of the latest poll, so also
        # surface sensors for accounts that exist only in the cache (e.g. first
        # boot after an HA restart, before the first post-boot poll has run).
        seen_ids.update(coordinator.cached_stable_ids())

        for stable_id in seen_ids:
            if stable_id in known:
                continue
            known.add(stable_id)
            new_entities.append(EnableBankingBalanceSensor(coordinator, BALANCE_SENSOR, stable_id))
            new_entities.append(
                EnableBankingTransactionsSensor(coordinator, TRANSACTIONS_SENSOR, stable_id)
            )
        if new_entities:
            async_add_entities(new_entities)

        # Give IBAN accounts a clean `sensor.<iban>` entity_id.
        _apply_iban_entity_ids(hass, entry, coordinator)

    _async_add_for_new_accounts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_for_new_accounts))

    # Entry-level budget sensors: one per category (defaults plus any the
    # user's rules introduce; a rule change reloads the entry), and totals.
    budget_entities: list[SensorEntity] = [
        EnableBankingCategorySensor(coordinator, slug, name)
        for slug, name in coordinator.categorizer.categories.items()
    ]
    budget_entities.append(EnableBankingTotalSensor(coordinator))
    budget_entities.append(EnableBankingBrowseSensor(coordinator))
    budget_entities.append(EnableBankingTotalBalanceSensor(coordinator))
    budget_entities.append(EnableBankingOverBudgetSensor(coordinator))
    async_add_entities(budget_entities)


@callback
def _migrate_unique_ids(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    coordinator: EnableBankingCoordinator,
) -> None:
    """Migrate old ``{entry}_{uid}_balance`` unique_ids to the stable-id scheme.

    Enable Banking rotates the account ``uid`` on every reauth, so uid-based
    unique_ids used to spawn a fresh entity each time. We now key on
    ``stable_id`` (identification_hash). This re-points the *currently active*
    entity for each account in place, preserving its entity_id and history,
    using the live uid-to-stable_id mapping from the latest poll. Historical
    orphans from earlier reauths can't be mapped and are left for the user to
    delete. Idempotent: after migration the old unique_id no longer exists.
    """
    data = coordinator.data
    if data is None:
        return
    registry = er.async_get(hass)
    for ab in data.accounts.values():
        uid = ab.account_id
        if not uid:
            continue
        old_unique = f"{entry.entry_id}_{uid}_{BALANCE_SENSOR.key}"
        new_unique = account_unique_id(entry.entry_id, ab.stable_id, BALANCE_SENSOR.key)
        if old_unique == new_unique:
            continue
        old_eid = registry.async_get_entity_id("sensor", DOMAIN, old_unique)
        new_eid = registry.async_get_entity_id("sensor", DOMAIN, new_unique)
        if old_eid and not new_eid:
            registry.async_update_entity(old_eid, new_unique_id=new_unique)
            _LOGGER.info(
                "Enable Banking: migrated %s to a session-stable unique_id",
                old_eid,
            )


@callback
def _apply_iban_entity_ids(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    coordinator: EnableBankingCoordinator,
) -> None:
    """Rename each IBAN account's entity to ``sensor.<iban>``.

    New IBAN sensors already register as ``sensor.<iban>`` (they set
    ``has_entity_name = False`` with the IBAN as their name). This fixes up the
    entity_id of sensors that were created by an earlier version under the
    ``sensor.<bank>_balance_<…>`` scheme, preserving their history.

    Guards: only auto-generated ids (still containing ``_balance``) are touched,
    so a user's own rename is never overwritten; and only when the target id is
    free. Accounts without an IBAN are left unchanged.
    """
    data = coordinator.data
    if data is None:
        return
    registry = er.async_get(hass)
    for ab in data.accounts.values():
        if not ab.iban:
            continue
        unique = account_unique_id(entry.entry_id, ab.stable_id, BALANCE_SENSOR.key)
        eid = registry.async_get_entity_id("sensor", DOMAIN, unique)
        if not eid:
            continue
        target = f"sensor.{slugify(ab.iban)}"
        if eid == target or "_balance" not in eid:
            continue
        if registry.async_get(target) is not None:
            _LOGGER.debug(
                "Enable Banking: cannot rename %s to %s (already exists)",
                eid,
                target,
            )
            continue
        registry.async_update_entity(eid, new_entity_id=target)
        _LOGGER.info("Enable Banking: renamed %s to %s", eid, target)


class EnableBankingBalanceSensor(EnableBankingEntity, SensorEntity):
    """Balance sensor for one Enable Banking account.

    Always returns the last known balance > fresh from the coordinator if
    the latest poll had it, otherwise from the persistent cache. The sensor
    never goes ``unavailable`` or returns ``unknown`` as long as at least
    one successful poll has ever happened for this account.
    """

    entity_description: EnableBankingSensorDescription
    coordinator: EnableBankingCoordinator

    def __init__(
        self,
        coordinator: EnableBankingCoordinator,
        description: EnableBankingSensorDescription,
        stable_id: str,
    ) -> None:
        super().__init__(coordinator, description, stable_id)
        # An account with an IBAN is named solely by its IBAN: friendly name
        # = the IBAN and entity_id = sensor.<iban> (no device prefix, no
        # "Balance" label). Accounts without an IBAN keep the device-qualified
        # "Balance …" name unchanged. Decided at creation from the IBAN the
        # first poll resolved; the entity_id of pre-existing sensors is fixed
        # up separately by _apply_iban_entity_ids().
        account = None
        if coordinator.data is not None:
            account = coordinator.data.accounts.get(stable_id)
        if account is None:
            account = coordinator.cached_account(stable_id)
        if account is not None and account.iban:
            self._attr_has_entity_name = False

    @property
    def name(self) -> str | None:
        account = self._current_account
        if account is not None and account.iban:
            return account.iban
        # No IBAN > unchanged behaviour (device-qualified "Balance …").
        if account is not None and account.name:
            return f"Balance {account.name}"
        return "Balance"

    @property
    def _current_account(self) -> AccountBalance | None:
        """Best-effort account lookup by stable_id: fresh data, else cache."""
        data = self.coordinator.data
        if data is not None and self._stable_id in data.accounts:
            return data.accounts[self._stable_id]
        return self.coordinator.cached_account(self._stable_id)

    @property
    def available(self) -> bool:
        # Intentionally does NOT chain to super().available. The base
        # CoordinatorEntity ties availability to the last poll's success,
        # which would flip the sensor to unavailable on a transient network
        # blip or a rate-limit response. We want exactly the opposite:
        # show the last known value with a `stale` attribute if need be.
        return self._current_account is not None

    @property
    def native_value(self) -> StateType:
        account = self._current_account
        if account is None:
            return None
        return self.entity_description.value_fn(account)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        account = self._current_account
        if account is None or self.entity_description.account_attrs_fn is None:
            return None

        attrs = self.entity_description.account_attrs_fn(account)
        attrs["aspsp"] = self.coordinator.config_entry.data.get(CONF_ASPSP_NAME)
        attrs["last_polled_at"] = (
            account.last_polled_at.isoformat() if account.last_polled_at else None
        )
        attrs["last_error"] = self.coordinator.last_error
        attrs["stale"] = _is_stale(account, self.coordinator.update_interval)

        data = self.coordinator.data
        if data is not None and data.consent_expires_at is not None:
            attrs["consent_expires_at"] = data.consent_expires_at.isoformat()
            attrs["consent_days_remaining"] = max(0, (data.consent_expires_at - utcnow()).days)

        return attrs


class EnableBankingTransactionsSensor(EnableBankingEntity, SensorEntity):
    """Transactions overview for one account.

    State: number of transactions in the current calendar month (by purchase
    date). Attributes: money in/out this month (internal transfers excluded),
    the most recent transactions with their category, and which fields the
    bank fills in (``field_coverage``).
    """

    coordinator: EnableBankingCoordinator
    # Bulky and only useful live; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"recent_transactions", "field_coverage"})

    @property
    def name(self) -> str | None:
        account = self.coordinator.cached_account(self._stable_id)
        if account is not None and (account.iban or account.name):
            return f"Transactions {account.iban or account.name}"
        return "Transactions"

    @property
    def available(self) -> bool:
        # Same philosophy as the balance sensor: show what we have.
        return True

    def _items(self) -> list[CategorizedTransaction]:
        return [i for i in self.coordinator.categorized if i.account_id == self._stable_id]

    @property
    def native_value(self) -> StateType:
        items = self._items()
        if not items and not self.coordinator.transactions_state(self._stable_id).get(
            "last_fetched_at"
        ):
            return None
        first = _current_month_start()
        return sum(1 for i in items if in_month(i, first))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        items = self._items()
        month = [i for i in items if in_month(i, _current_month_start())]
        state = self.coordinator.transactions_state(self._stable_id)
        return {
            "month_spent": spending_total(month),
            "month_received": income_total(month),
            "by_category": by_category(month, self.coordinator.categorizer.categories),
            "stored_transactions": len(items),
            "last_fetched_at": state.get("last_fetched_at"),
            "last_error": state.get("last_error", ""),
            "recent_transactions": [
                compact(i, self.coordinator.categorizer.categories)
                for i in items[:TRANSACTIONS_RECENT_COUNT]
            ],
            "field_coverage": field_coverage(self.coordinator.transactions_for(self._stable_id))[
                "filled"
            ],
        }


# Recent transactions (all accounts) on the total spending sensor.
RECENT_ALL_ACCOUNTS = 20
# Filtered transactions listed on the browser sensor.
BROWSE_LIST_SIZE = 100


def _current_month_start() -> date:
    return as_local(utcnow()).date().replace(day=1)


def _month_last_reset() -> datetime:
    """Local midnight on the 1st of the current month."""
    return start_of_local_day(_current_month_start())


class _MonthlyAmountSensor(EnableBankingEntryEntity, SensorEntity):
    """Common bits of the monthly money sensors (reset on the 1st)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2
    _unrecorded_attributes = frozenset(
        {"transactions", "unmatched_merchants", "recent_transactions", "by_category"}
    )

    @property
    def last_reset(self) -> datetime | None:
        return _month_last_reset()

    def _month_items(self, first: date) -> list[CategorizedTransaction]:
        return [i for i in self.coordinator.categorized if in_month(i, first)]


class EnableBankingCategorySensor(_MonthlyAmountSensor):
    """Net amount of one category this month, all accounts of the entry.

    For spending categories the state is money out minus refunds; for
    "Revenus" it is money in. Attributes give last month's figure and the
    month's transactions. "Autre" also lists the merchants no rule caught,
    which is what to write custom rules for.
    """

    def __init__(self, coordinator: EnableBankingCoordinator, slug: str, name: str) -> None:
        super().__init__(coordinator, f"category_{slug}")
        self._slug = slug
        if slug in (CAT_INCOME, CAT_INTERNAL):
            self._attr_name = f"{name} du mois"
        else:
            self._attr_name = f"Dépenses {name}"
        default = DEFAULT_CATEGORIES.get(slug)
        self._attr_icon = default[1] if default else "mdi:tag"

    @property
    def native_value(self) -> StateType:
        return category_total(self._month_items(_current_month_start()), self._slug)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        first = _current_month_start()
        month = [i for i in self._month_items(first) if i.category == self._slug]
        attrs: dict[str, Any] = {
            "category": self._slug,
            "previous_month": category_total(
                self._month_items(previous_month_start(first)), self._slug
            ),
            "count": len(month),
            "transactions": [
                compact(i, self.coordinator.categorizer.categories) for i in month[:50]
            ],
        }
        attrs.update(_budget_attrs(self.coordinator.budgets.get(self._slug), self.native_value))
        if self._slug == CAT_OTHER:
            # Merchants to write rules for, most spent first, over all history.
            spent: Counter[str] = Counter()
            for item in self.coordinator.categorized:
                if item.category == CAT_OTHER:
                    spent[item.merchant] += -item.amount
            attrs["unmatched_merchants"] = [
                {"merchant": m, "total": round(v, 2)} for m, v in spent.most_common(30)
            ]
        return attrs


class EnableBankingTotalSensor(_MonthlyAmountSensor):
    """Total spending this month, internal transfers and income excluded.

    Income has no separate total: the "Revenus du mois" category sensor is it.
    """

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "spending_month")
        self._attr_name = "Dépenses totales du mois"
        self._attr_icon = "mdi:cash-minus"

    @property
    def native_value(self) -> StateType:
        return spending_total(self._month_items(_current_month_start()))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        first = _current_month_start()
        month = self._month_items(first)
        names = self.coordinator.categorizer.categories
        return {
            "previous_month": spending_total(self._month_items(previous_month_start(first))),
            "income": income_total(month),
            "by_category": by_category(month, names),
            "recent_transactions": [
                compact(i, names) for i in self.coordinator.categorized[:RECENT_ALL_ACCOUNTS]
            ],
        }


def _budget_attrs(budget: float | None, spent: StateType) -> dict[str, Any]:
    """Budget figures for a category sensor (empty values when no budget)."""
    if not budget:
        return {
            "budget": None,
            "budget_remaining": None,
            "budget_used_pct": None,
            "over_budget": False,
        }
    value = float(spent) if isinstance(spent, int | float) else 0.0
    return {
        "budget": budget,
        "budget_remaining": round(budget - value, 2),
        "budget_used_pct": round(value / budget * 100),
        "over_budget": value > budget,
    }


class EnableBankingOverBudgetSensor(EnableBankingEntryEntity, SensorEntity):
    """How many categories are over their monthly budget, and which.

    Handy for an automation ("notify me when a budget is exceeded") and for
    the dashboard. ``budgets`` lists every category that has a budget.
    """

    _attr_name = "Budgets dépassés"
    _attr_icon = "mdi:alert-octagon-outline"

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "over_budget")

    def _rows(self) -> list[dict[str, Any]]:
        month = [i for i in self.coordinator.categorized if in_month(i, _current_month_start())]
        names = self.coordinator.categorizer.categories
        rows = []
        for slug, budget in self.coordinator.budgets.items():
            if slug not in names or not budget:
                continue
            spent = category_total(month, slug)
            rows.append(
                {
                    "category": names[slug],
                    "spent": spent,
                    "budget": budget,
                    "used_pct": round(spent / budget * 100),
                    "over": spent > budget,
                }
            )
        return sorted(rows, key=lambda r: r["used_pct"], reverse=True)

    @property
    def native_value(self) -> StateType:
        return sum(1 for r in self._rows() if r["over"])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rows = self._rows()
        return {
            "over": [r["category"] for r in rows if r["over"]],
            "budgets": rows,
            "total_budget": round(sum(r["budget"] for r in rows), 2),
            "total_spent_budgeted": round(sum(r["spent"] for r in rows), 2),
        }


class EnableBankingTotalBalanceSensor(EnableBankingEntryEntity, SensorEntity):
    """Sum of every account balance of this bank entry, overdrafts included.

    Negative when the accounts are overdrawn overall. Attributes list each
    account with its own (signed) balance and an ``overdrawn`` flag.
    """

    _attr_name = "Solde total"
    _attr_icon = "mdi:piggy-bank-outline"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "total_balance")

    def _accounts(self) -> list[AccountBalance]:
        ids = sorted(self.coordinator.cached_stable_ids())
        return [a for i in ids if (a := self.coordinator.cached_account(i)) is not None]

    @property
    def native_value(self) -> StateType:
        accounts = self._accounts()
        if not accounts:
            return None
        return round(sum(a.balance for a in accounts), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        accounts = self._accounts()
        return {
            "overdrawn": sum(a.balance for a in accounts) < 0,
            "overdrawn_accounts": sum(1 for a in accounts if a.balance < 0),
            "accounts": [
                {
                    "name": a.name,
                    "iban": a.iban,
                    "balance": round(a.balance, 2),
                    "overdrawn": a.balance < 0,
                }
                for a in accounts
            ],
        }


class EnableBankingBrowseSensor(EnableBankingEntryEntity, SensorEntity):
    """The transaction currently selected in the browser.

    State: the merchant. Attributes: every detail including the exact bank
    label, the position ("3 / 57"), and the filtered list (``operations``,
    newest first, up to ``BROWSE_LIST_SIZE``) for a scrollable list card.
    Navigation is the "Filtre des opérations" select and the previous/next
    buttons; nothing here calls the bank.
    """

    _attr_name = "Opération sélectionnée"
    _attr_icon = "mdi:receipt-text-outline"
    _unrecorded_attributes = frozenset({"operations", "label", "detail"})

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, "browse_selected")

    def _selected(self) -> tuple[list[CategorizedTransaction], CategorizedTransaction | None]:
        items = self.coordinator.browse_items()
        if not items:
            return items, None
        index = min(self.coordinator.browse_index, len(items) - 1)
        return items, items[index]

    @property
    def native_value(self) -> StateType:
        _items, item = self._selected()
        # States are capped at 255 characters.
        return item.merchant[:255] if item else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        items, item = self._selected()
        names = self.coordinator.categorizer.categories
        attrs: dict[str, Any] = {
            "position": min(self.coordinator.browse_index, max(len(items) - 1, 0)) + 1
            if items
            else 0,
            "total": len(items),
            "filter": names.get(self.coordinator.browse_category, "Toutes"),
            "period": BROWSE_PERIODS.get(self.coordinator.browse_period),
            "sum_out": round(-sum(i.amount for i in items if i.amount < 0), 2),
            "sum_in": round(sum(i.amount for i in items if i.amount > 0), 2),
            "operations": [compact(i, names) for i in items[:BROWSE_LIST_SIZE]],
        }
        if item is not None:
            detail = compact(item, names)
            attrs.update(
                {
                    "detail": detail,
                    "label": detail["label"],
                    "date": detail["date"],
                    "amount": detail["amount"],
                    "category_name": detail["category_name"],
                    "account": detail["account"],
                    "pending": detail["pending"],
                    "kind": item.kind,
                }
            )
        return attrs


def _is_stale(account: AccountBalance, update_interval: timedelta | None) -> bool:
    """True if the last successful poll for this account is older than 2x interval."""
    if account.last_polled_at is None:
        return True
    interval = update_interval or timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    # homeassistant-stubs types dt_util.utcnow as Incomplete, so the comparison
    # below is Any as far as mypy is concerned. bool() keeps strict mode happy
    # without an ignore comment.
    return bool((utcnow() - account.last_polled_at) > 2 * interval)
