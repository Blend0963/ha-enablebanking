"""DataUpdateCoordinator for the Enable Banking integration.

Design notes (v0.5.0):

- **Fixed-schedule polling**: polls fire at ``POLL_HOURS`` local time
  (10:00, 14:00, 18:00, 22:00) with a per-entry minute jitter so multiple
  banks don't burst at ``HH:00:00``. The minute offset is a deterministic
  hash of ``entry_id`` so it's stable across HA restarts.

- **``update_interval = None``**: we opt out of ``DataUpdateCoordinator``'s
  built-in interval scheduler entirely. All polls come from our
  ``async_track_time_change`` listeners or the one-shot catch-up.

- **Catch-up on startup**: if cache's ``last_polled_at`` is older than the
  most recent scheduled time that has passed, we trigger one refresh
  (with 0-60 s jitter). Otherwise we just wait for the next slot. This is
  what keeps HA restarts from burning PSD2 quota.

- **``_async_update_data`` NEVER raises.** On any failure (rate limit,
  network, consent expiry, auth) it sets ``self.last_error`` and returns
  the cached snapshot so sensors keep displaying their last good value.
  Reauth UI is triggered directly via ``config_entry.async_start_reauth``.

- **Per-account 429 back-off**: a rate-limited UID gets
  ``rate_limited_until = now + 4 hours`` stamped on its cached entry. The
  next scheduled poll skips it; the one after resumes.

- **Transactions** ride on the same scheduled poll, after balances, so they
  never add a poll of their own and a transactions failure can never cost a
  balance update. They have their own per-account back-off and their own
  on-disk store (``enablebanking.<entry_id>.transactions``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import EnableBankingClient
from .categorize import (
    CategorizedTransaction,
    Categorizer,
    normalize,
    parse_user_rules,
    previous_month_start,
)
from .const import (
    CONF_APP_ID,
    CONF_ASPSP_NAME,
    CONF_CONSENT_EXPIRES_AT,
    CONF_CUSTOM_RULES,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONSENT_WARNING_DAYS,
    DEFAULT_BROWSE_PERIOD,
    DOMAIN,
    POLL_HOURS,
    STORAGE_VERSION,
    TRANSACTIONS_INITIAL_DAYS,
    TRANSACTIONS_KEEP_DAYS,
    TRANSACTIONS_OVERLAP_DAYS,
)
from .errors import (
    EnableBankingAPIError,
    EnableBankingAuthenticationError,
    EnableBankingConnectionError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .jwt_helper import jwt_seconds_remaining, mint_jwt
from .models import AccountBalance, EnableBankingData
from .transactions import (
    field_coverage,
    latest_booked_date,
    merge_transactions,
    prune_transactions,
    sorted_transactions,
)

_LOGGER = logging.getLogger(__name__)

type EnableBankingConfigEntry = ConfigEntry[EnableBankingCoordinator]

# Back-off one scheduled cycle on a 429. With 4-hour gaps between polls
# this effectively retries at the next slot.
_BACK_OFF = timedelta(hours=4)


class EnableBankingCoordinator(DataUpdateCoordinator[EnableBankingData]):
    """Coordinator to fetch balances via Enable Banking on a fixed schedule."""

    config_entry: EnableBankingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EnableBankingConfigEntry,
        client: EnableBankingClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # scheduled polling > we drive refresh ourselves
        )
        self.client = client
        self.last_refresh: datetime | None = None
        self.last_error: str = ""
        self._warned_expiry = False
        self._cached: dict[str, AccountBalance] = {}
        # Pre-0.6.5 cache entries were keyed by the session uid and carry no
        # stable_id, so they can't seed a (stable_id-keyed) sensor directly.
        # We hold them here and adopt them on the first poll > which knows the
        # uid-to-stable_id mapping > so last-known balances survive the upgrade
        # instead of the sensors going unavailable until a fresh poll succeeds.
        self._legacy_by_uid: dict[str, AccountBalance] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache"
        )
        # Transactions: {stable_id: {tx_key: raw_tx}} plus per-account fetch
        # state ({stable_id: {"last_fetched_at", "rate_limited_until",
        # "last_error"}}), persisted in their own file.
        self._transactions: dict[str, dict[str, dict[str, Any]]] = {}
        self._tx_state: dict[str, dict[str, Any]] = {}
        self._tx_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.transactions"
        )
        # Categorised view of every stored transaction of this entry, rebuilt
        # after each transactions update (and at load), never from the API.
        self.categorizer = Categorizer()
        self.categorized: list[CategorizedTransaction] = []
        # Transaction browser state (dashboard navigation, no API involved):
        # category filter slug ("" = all) and 0-based position in the
        # filtered list, newest first.
        self.browse_category: str = ""
        self.browse_period: str = DEFAULT_BROWSE_PERIOD
        self.browse_index: int = 0
        # Monthly budget per category slug (0 / missing = no budget). Set by
        # the number entities, which restore their value across restarts.
        self.budgets: dict[str, float] = {}
        # Options as last applied by this coordinator. The entry's update
        # listener reloads only when the options differ from this, so a rule
        # added from the dashboard applies in place without a reload.
        self.applied_options: dict[str, Any] = dict(entry.options)
        # Deterministic per-entry minute offset in [0, 59] so multiple
        # banks don't all poll at xx:00:00. Hash of entry_id stays stable
        # across HA restarts.
        self._minute_offset: int = abs(hash(entry.entry_id)) % 60

    @property
    def minute_offset(self) -> int:
        return self._minute_offset

    # ------------------------------------------------------------------ #
    # Scheduling                                                           #
    # ------------------------------------------------------------------ #

    def register_scheduled_polls(self) -> list[CALLBACK_TYPE]:
        """Register an ``async_track_time_change`` per POLL_HOUR.

        Returns the unsub callbacks > caller should attach them to
        ``entry.async_on_unload``.
        """

        async def _on_scheduled(now: datetime) -> None:
            _LOGGER.debug(
                "Scheduled poll fired for entry %s at %s (minute_offset=%d)",
                self.config_entry.entry_id,
                now.isoformat(),
                self._minute_offset,
            )
            await self.async_refresh()

        unsubs: list[CALLBACK_TYPE] = []
        for hour in POLL_HOURS:
            unsubs.append(
                async_track_time_change(
                    self.hass,
                    _on_scheduled,
                    hour=hour,
                    minute=self._minute_offset,
                    second=0,
                )
            )
        _LOGGER.debug(
            "Registered %d scheduled polls for entry %s at %s local time, minute %02d",
            len(unsubs),
            self.config_entry.entry_id,
            ", ".join(f"{h:02d}:00" for h in POLL_HOURS),
            self._minute_offset,
        )
        return unsubs

    def most_recent_scheduled_time(self, now: datetime) -> datetime:
        """The most recent of the POLL_HOURS slots at or before ``now`` (UTC)."""
        local_now = dt_util.as_local(now)
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        candidates = [today.replace(hour=h, minute=self._minute_offset) for h in POLL_HOURS]
        past = [c for c in candidates if c <= local_now]
        if past:
            return dt_util.as_utc(max(past))
        # Before today's first slot > most recent is yesterday's last slot
        yesterday_last = (today - timedelta(days=1)).replace(
            hour=POLL_HOURS[-1], minute=self._minute_offset
        )
        return dt_util.as_utc(yesterday_last)

    def needs_catchup(self) -> bool:
        """True if we should poll now rather than wait for the next slot.

        We catch up if the cache has never been populated, OR the most
        recent scheduled slot already passed and the cache is older than it.
        """
        now = dt_util.utcnow()
        if self.last_refresh is None:
            return True
        return self.last_refresh < self.most_recent_scheduled_time(now)

    # ------------------------------------------------------------------ #
    # Cache                                                                #
    # ------------------------------------------------------------------ #

    async def async_load_cache(self) -> None:
        """Hydrate ``self._cached`` from disk and seed ``coordinator.data``.

        Call this once in ``async_setup_entry`` before forwarding platforms.
        """
        stored = await self._store.async_load() or {}
        # The stored key is ignored on purpose: which key an entry belongs
        # under is re-derived below (stable_id, or uid for pre-0.6.5 files).
        for raw in (stored.get("accounts") or {}).values():
            if not isinstance(raw, dict):
                continue
            ab = _balance_from_stored(raw)
            if ab is None:
                continue
            if ab.stable_id:
                self._cached[ab.stable_id] = ab
            elif ab.account_id:
                # Pre-0.6.5 entry: keep by uid and adopt on the first poll.
                self._legacy_by_uid[ab.account_id] = ab

        self.last_refresh = _parse_iso(stored.get("last_polled_at"))

        tx_stored = await self._tx_store.async_load() or {}
        for stable_id, txs in (tx_stored.get("accounts") or {}).items():
            if isinstance(stable_id, str) and isinstance(txs, dict):
                self._transactions[stable_id] = {
                    k: v for k, v in txs.items() if isinstance(k, str) and isinstance(v, dict)
                }
        for stable_id, state in (tx_stored.get("state") or {}).items():
            if isinstance(stable_id, str) and isinstance(state, dict):
                self._tx_state[stable_id] = state
        self.recategorize()

        if self._cached:
            _LOGGER.debug(
                "Hydrated %d account(s) from cache for entry %s",
                len(self._cached),
                self.config_entry.entry_id,
            )
            self.async_set_updated_data(
                EnableBankingData(
                    accounts=dict(self._cached),
                    consent_expires_at=self._parse_consent_expires(),
                )
            )

    async def _save_cache(self) -> None:
        await self._store.async_save(
            {
                "last_polled_at": self.last_refresh.isoformat() if self.last_refresh else None,
                "accounts": {
                    stable_id: _balance_to_stored(ab) for stable_id, ab in self._cached.items()
                },
            }
        )

    async def _save_transactions(self) -> None:
        await self._tx_store.async_save({"accounts": self._transactions, "state": self._tx_state})

    def recategorize(self) -> None:
        """Rebuild the categoriser from the options and re-sort every transaction.

        Cheap (a few hundred regex searches) and API-free, so a rule change
        applies to the whole history at once.
        """
        rules, errors = parse_user_rules(self.config_entry.options.get(CONF_CUSTOM_RULES, ""))
        if errors:
            _LOGGER.warning("Enable Banking: ignored malformed rule(s): %s", "; ".join(errors))
        owners = {ab.name for ab in self._cached.values() if ab.name}
        self.categorizer = Categorizer(user_rules=rules, owner_names=sorted(owners))
        items: list[CategorizedTransaction] = []
        for stable_id, txs in self._transactions.items():
            account = self._cached.get(stable_id)
            account_label = (account.iban or account.name) if account else ""
            items.extend(
                self.categorizer.categorize(tx, account_label, stable_id) for tx in txs.values()
            )
        items.sort(key=lambda i: i.day or date.min, reverse=True)
        self.categorized = items

    def browse_items(self) -> list[CategorizedTransaction]:
        """Transactions matching the browser's category and period, newest first."""
        start, end = period_bounds(self.browse_period, dt_util.now().date())
        return [
            i
            for i in self.categorized
            if (not self.browse_category or i.category == self.browse_category)
            and (start is None or (i.day is not None and start <= i.day <= end))
        ]

    def browse_set_period(self, period: str) -> None:
        self.browse_period = period
        self.browse_index = 0
        self.async_update_listeners()

    def set_budget(self, slug: str, value: float) -> None:
        if value > 0:
            self.budgets[slug] = value
        else:
            self.budgets.pop(slug, None)
        self.async_update_listeners()

    def add_rule(self, keyword: str, category_name: str) -> None:
        """Store ``KEYWORD = Category`` in the options and re-sort everything.

        Any earlier rule with the same keyword is replaced. Applied in place:
        no reload, no bank call. The rule then shows (and can be edited) in
        the entry's Configure dialog.
        """
        key = normalize(keyword)
        if not key:
            return
        lines = [
            line
            for line in str(self.config_entry.options.get(CONF_CUSTOM_RULES, "")).splitlines()
            if normalize(line.partition("=")[0]) != key or "=" not in line
        ]
        # Newest rule first: rules are checked in order, so it wins.
        lines.insert(0, f"{key} = {category_name}")
        options = {**self.config_entry.options, CONF_CUSTOM_RULES: "\n".join(lines).strip()}
        self.applied_options = options
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)
        self.recategorize()
        self.async_update_listeners()

    def browse_set_category(self, slug: str) -> None:
        self.browse_category = slug
        self.browse_index = 0
        self.async_update_listeners()

    def browse_move(self, step: int) -> None:
        """Move the selection; +1 = older, -1 = newer. Clamped to the list."""
        count = len(self.browse_items())
        self.browse_index = max(0, min(self.browse_index + step, max(count - 1, 0)))
        self.async_update_listeners()

    def transactions_for(self, stable_id: str) -> list[dict[str, Any]]:
        """Stored raw transactions of one account, newest first."""
        return sorted_transactions(self._transactions.get(stable_id, {}))

    def transactions_state(self, stable_id: str) -> dict[str, Any]:
        """Fetch state of one account's transactions (last fetch, last error)."""
        return dict(self._tx_state.get(stable_id, {}))

    def cached_account(self, stable_id: str) -> AccountBalance | None:
        return self._cached.get(stable_id)

    def cached_stable_ids(self) -> set[str]:
        """Every account we hold a cached balance for.

        The sensor platform needs this at boot: ``self.data`` only reflects the
        latest poll, while the cache also covers accounts whose last poll
        happened before the current HA run.
        """
        return set(self._cached)

    # ------------------------------------------------------------------ #
    # Refresh                                                              #
    # ------------------------------------------------------------------ #

    async def _async_maybe_renew_jwt(self) -> None:
        """Silently regenerate the JWT if it expires within 30 minutes.

        Only runs when a private key is stored in the config entry (new-style
        setup). Old entries without a private key are unaffected.
        """
        private_key = self.config_entry.data.get(CONF_PRIVATE_KEY)
        app_id = self.config_entry.data.get(CONF_APP_ID)
        if not private_key or not app_id:
            return

        remaining = jwt_seconds_remaining(self.client._jwt)
        if remaining > 1800:  # more than 30 min left > nothing to do
            return

        _LOGGER.debug(
            "JWT for entry %s expires in %ds > auto-renewing",
            self.config_entry.entry_id,
            remaining,
        )
        try:
            new_jwt = mint_jwt(private_key, app_id)
        except Exception as err:
            _LOGGER.warning("Failed to auto-renew JWT: %s", err)
            return

        self.client.update_jwt(new_jwt)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_JWT: new_jwt},
        )
        _LOGGER.debug("JWT auto-renewed for entry %s", self.config_entry.entry_id)

    async def _async_update_data(self) -> EnableBankingData:
        """Fetch balances. NEVER raises > always returns cached data on error."""
        await self._async_maybe_renew_jwt()
        now = dt_util.utcnow()
        skip_ids = {
            stable_id
            for stable_id, ab in self._cached.items()
            if ab.rate_limited_until is not None and ab.rate_limited_until > now
        }
        if skip_ids:
            _LOGGER.debug(
                "Skipping %d rate-limited account(s) this cycle: %s",
                len(skip_ids),
                sorted(s[:8] for s in skip_ids),
            )

        try:
            fresh, rate_limited_ids = await self.client.async_get_all_balances(
                fallback=self._cached,
                skip_ids=skip_ids,
                legacy_by_uid=self._legacy_by_uid or None,
            )
        except EnableBankingAuthenticationError as err:
            self.last_error = "auth"
            _LOGGER.warning("JWT rejected: %s > triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingSessionError as err:
            self.last_error = "consent_expired"
            _LOGGER.warning("Session expired: %s > triggering reauth", err)
            self.config_entry.async_start_reauth(self.hass)
            return self._cached_snapshot()
        except EnableBankingRateLimitError as err:
            self.last_error = "rate_limited"
            _LOGGER.warning("Session-level PSD2 rate limit; keeping cached balances: %s", err)
            return self._cached_snapshot()
        except EnableBankingConnectionError as err:
            self.last_error = "network"
            _LOGGER.warning("Network error; keeping cached balances: %s", err)
            return self._cached_snapshot()
        except EnableBankingAPIError as err:
            self.last_error = "api"
            _LOGGER.warning("API error; keeping cached balances: %s", err)
            return self._cached_snapshot()

        self.last_error = ""
        self.last_refresh = now
        back_off_until = now + _BACK_OFF

        for stable_id, ab in fresh.items():
            if stable_id in rate_limited_ids:
                ab.rate_limited_until = back_off_until
            else:
                ab.last_polled_at = now
                ab.rate_limited_until = None
            self._cached[stable_id] = ab

        # Drop legacy (pre-0.6.5) entries we've now adopted into the stable
        # cache so they aren't re-considered on later polls.
        if self._legacy_by_uid:
            for adopted in {ab.account_id for ab in fresh.values() if ab.account_id}:
                self._legacy_by_uid.pop(adopted, None)

        await self._save_cache()

        await self._async_update_transactions(fresh, rate_limited_ids, now)

        consent_expires_at = self._parse_consent_expires()
        self._maybe_warn_expiry(consent_expires_at)

        return EnableBankingData(
            accounts=dict(self._cached),
            consent_expires_at=consent_expires_at,
        )

    async def _async_update_transactions(
        self,
        fresh: dict[str, AccountBalance],
        rate_limited_ids: set[str],
        now: datetime,
    ) -> None:
        """Fetch new transactions for every account whose balance just updated.

        NEVER raises. Errors are recorded per account in ``_tx_state`` and the
        stored transactions are kept as they are.
        """
        today = dt_util.as_local(now).date()
        changed = False
        for stable_id, ab in fresh.items():
            if stable_id in rate_limited_ids or not ab.account_id:
                continue
            state = self._tx_state.setdefault(stable_id, {})
            backoff_until = _parse_iso(state.get("rate_limited_until"))
            if backoff_until is not None and backoff_until > now:
                _LOGGER.debug("Transactions for %s: back-off until %s", ab.name, backoff_until)
                continue

            stored = self._transactions.get(stable_id, {})
            latest = latest_booked_date(stored)
            date_from: date = (
                latest - timedelta(days=TRANSACTIONS_OVERLAP_DAYS)
                if latest is not None
                else today - timedelta(days=TRANSACTIONS_INITIAL_DAYS)
            )
            try:
                fetched = await self.client.async_get_account_transactions(ab.account_id, date_from)
            except EnableBankingRateLimitError as err:
                state["last_error"] = "rate_limited"
                state["rate_limited_until"] = (now + _BACK_OFF).isoformat()
                _LOGGER.warning("Transactions for %s rate-limited: %s", ab.name, err)
                changed = True
                continue
            except EnableBankingConnectionError as err:
                state["last_error"] = "network"
                _LOGGER.warning("Transactions for %s: network error: %s", ab.name, err)
                changed = True
                continue
            except (
                EnableBankingAuthenticationError,
                EnableBankingSessionError,
                EnableBankingAPIError,
            ) as err:
                # Balances went through with the same session, so this is an
                # endpoint-specific refusal (e.g. the consent does not cover
                # transactions), not something reauth would fix.
                state["last_error"] = "api"
                _LOGGER.warning("Transactions for %s not available: %s", ab.name, err)
                changed = True
                continue

            merged = merge_transactions(stored, fetched, date_from)
            self._transactions[stable_id] = prune_transactions(
                merged, today, TRANSACTIONS_KEEP_DAYS
            )
            state["last_fetched_at"] = now.isoformat()
            state["last_error"] = ""
            state["rate_limited_until"] = None
            changed = True

            coverage = field_coverage(fetched)
            _LOGGER.info(
                "Enable Banking: %d transaction(s) since %s for %s (%d stored). Field coverage: %s",
                len(fetched),
                date_from.isoformat(),
                ab.name,
                len(self._transactions[stable_id]),
                {k: v for k, v in coverage["filled"].items() if v},
            )

        if changed:
            await self._save_transactions()
        self.recategorize()

    def _cached_snapshot(self) -> EnableBankingData:
        return EnableBankingData(
            accounts=dict(self._cached),
            consent_expires_at=self._parse_consent_expires(),
        )

    # ------------------------------------------------------------------ #
    # Consent expiry                                                       #
    # ------------------------------------------------------------------ #

    def _parse_consent_expires(self) -> datetime | None:
        return _parse_iso(self.config_entry.data.get(CONF_CONSENT_EXPIRES_AT))

    def _maybe_warn_expiry(self, consent_expires_at: datetime | None) -> None:
        if consent_expires_at is None or self._warned_expiry:
            return
        notification_id = f"{DOMAIN}_expiry_{self.config_entry.entry_id}"
        days_remaining = (consent_expires_at - dt_util.utcnow()).days
        if days_remaining > CONSENT_WARNING_DAYS:
            # Renewed (reconfigure or reauth reloads the entry with the new
            # expiry), so the warning from the old consent no longer applies.
            persistent_notification.async_dismiss(self.hass, notification_id)
            return
        aspsp_name = self.config_entry.data.get(CONF_ASPSP_NAME, "your bank")
        # Reconfigure lives in the entry's three-dot menu; there is no button on
        # the integration card. The link opens the integration page directly.
        persistent_notification.async_create(
            self.hass,
            message=(
                f"Your {aspsp_name} Enable Banking consent expires in "
                f"{days_remaining} day(s). Open "
                f"[Enable Banking](/config/integrations/integration/{DOMAIN}), "
                f"click the **⋮** menu next to **{self.config_entry.title}** and "
                "choose **Reconfigure** to renew it at your bank before it expires "
                "and balances go stale."
            ),
            title="Enable Banking consent expiring soon",
            notification_id=notification_id,
        )
        self._warned_expiry = True


def period_bounds(period: str, today: date) -> tuple[date | None, date]:
    """First and last day (inclusive) of a browser period; None = no limit."""
    if period == "today":
        return today, today
    if period == "7d":
        return today - timedelta(days=6), today
    if period == "30d":
        return today - timedelta(days=29), today
    if period == "month":
        return today.replace(day=1), today
    if period == "last_month":
        first = previous_month_start(today)
        return first, today.replace(day=1) - timedelta(days=1)
    if period == "3m":
        return previous_month_start(previous_month_start(today)), today
    if period == "year":
        return today.replace(month=1, day=1), today
    return None, today


# ---------------------------------------------------------------------- #
# Cache serialisation helpers                                             #
# ---------------------------------------------------------------------- #


def _balance_from_stored(data: dict[str, Any]) -> AccountBalance | None:
    try:
        # ``stable_id`` may be absent in pre-0.6.5 cache files; such entries
        # get an empty stable_id and are held as legacy (adopted on first poll).
        stable_id = data.get("stable_id", "")
        if not isinstance(stable_id, str):
            stable_id = ""
        return AccountBalance(
            account_id=str(data["account_id"]),
            stable_id=stable_id,
            iban=str(data.get("iban", "")),
            name=str(data.get("name", "")),
            product=data.get("product") if isinstance(data.get("product"), str) else None,
            currency=str(data.get("currency", "EUR")),
            balance=float(data["balance"]),
            balance_type=data.get("balance_type")
            if isinstance(data.get("balance_type"), str)
            else None,
            reference_date=data.get("reference_date")
            if isinstance(data.get("reference_date"), str)
            else None,
            last_polled_at=_parse_iso(data.get("last_polled_at")),
            rate_limited_until=_parse_iso(data.get("rate_limited_until")),
        )
    except (KeyError, TypeError, ValueError):
        _LOGGER.debug("Skipping malformed cached entry: %r", data)
        return None


def _balance_to_stored(ab: AccountBalance) -> dict[str, Any]:
    return {
        "account_id": ab.account_id,
        "stable_id": ab.stable_id,
        "iban": ab.iban,
        "name": ab.name,
        "product": ab.product,
        "currency": ab.currency,
        "balance": ab.balance,
        "balance_type": ab.balance_type,
        "reference_date": ab.reference_date,
        "last_polled_at": ab.last_polled_at.isoformat() if ab.last_polled_at else None,
        "rate_limited_until": ab.rate_limited_until.isoformat() if ab.rate_limited_until else None,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
