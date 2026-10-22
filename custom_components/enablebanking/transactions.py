"""Pure helpers for Enable Banking transactions.

Kept free of Home Assistant imports so they can be unit-tested on their own.

Transactions are stored per account as ``{key: raw_transaction}``. The raw
Enable Banking object is kept as-is (no lossy mapping) because the first goal
is to see which fields each ASPSP really fills in, before building
categorisation on top of them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

# Fields worth checking per ASPSP when deciding how to categorise.
COVERAGE_FIELDS: tuple[str, ...] = (
    "transaction_id",
    "entry_reference",
    "booking_date",
    "value_date",
    "transaction_date",
    "status",
    "credit_debit_indicator",
    "transaction_amount",
    "remittance_information",
    "creditor",
    "debtor",
    "creditor_account",
    "debtor_account",
    "bank_transaction_code",
    "merchant_category_code",
    "balance_after_transaction",
    "note",
)

_PENDING_STATUSES = frozenset({"PDNG", "PENDING", "pending"})


def transaction_date(tx: dict[str, Any]) -> date | None:
    """Best date for a transaction: booking, then value, then transaction date."""
    for key in ("booking_date", "value_date", "transaction_date"):
        raw = tx.get(key)
        if isinstance(raw, str) and raw:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def is_pending(tx: dict[str, Any]) -> bool:
    return tx.get("status") in _PENDING_STATUSES


def signed_amount(tx: dict[str, Any]) -> float | None:
    """Amount as a signed float: negative for debits (money out)."""
    amount_obj = tx.get("transaction_amount")
    if not isinstance(amount_obj, dict):
        return None
    try:
        value = float(amount_obj.get("amount"))
    except (TypeError, ValueError):
        return None
    if tx.get("credit_debit_indicator") == "DBIT":
        return -abs(value)
    return abs(value)


def currency(tx: dict[str, Any]) -> str | None:
    amount_obj = tx.get("transaction_amount")
    if isinstance(amount_obj, dict) and isinstance(amount_obj.get("currency"), str):
        return str(amount_obj["currency"])
    return None


def label(tx: dict[str, Any]) -> str:
    """Human-readable label: remittance information joined, else the note."""
    remittance = tx.get("remittance_information")
    if isinstance(remittance, list):
        parts = [p.strip() for p in remittance if isinstance(p, str) and p.strip()]
        if parts:
            return " ".join(parts)
    elif isinstance(remittance, str) and remittance.strip():
        return remittance.strip()
    note = tx.get("note")
    return note.strip() if isinstance(note, str) else ""


def counterparty(tx: dict[str, Any]) -> str:
    """Name of the other side: creditor for money out, debtor for money in."""
    side = "creditor" if tx.get("credit_debit_indicator") == "DBIT" else "debtor"
    party = tx.get(side)
    if isinstance(party, dict) and isinstance(party.get("name"), str):
        return str(party["name"])
    return ""


def transaction_key(tx: dict[str, Any]) -> str:
    """Stable dedup key for a transaction.

    Prefers the ASPSP's own ids. Falls back to a hash of the content, which is
    stable across polls for a booked transaction.
    """
    for field in ("transaction_id", "entry_reference"):
        value = tx.get(field)
        if isinstance(value, str) and value:
            return f"{field}:{value}"
    fingerprint = json.dumps(
        [
            tx.get("booking_date"),
            tx.get("value_date"),
            tx.get("transaction_amount"),
            tx.get("credit_debit_indicator"),
            tx.get("remittance_information"),
            tx.get("status"),
        ],
        sort_keys=True,
        default=str,
    )
    return "hash:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]


def merge_transactions(
    stored: dict[str, dict[str, Any]],
    fetched: list[dict[str, Any]],
    date_from: date,
) -> dict[str, dict[str, Any]]:
    """Merge a fresh listing into the stored set.

    Pending transactions inside the fetched window are dropped first and
    replaced by whatever the bank reports now: a pending item usually comes
    back booked under a different id, and keeping the old one would count it
    twice.
    """
    merged: dict[str, dict[str, Any]] = {}
    for key, tx in stored.items():
        tx_day = transaction_date(tx)
        if is_pending(tx) and (tx_day is None or tx_day >= date_from):
            continue
        merged[key] = tx
    for tx in fetched:
        merged[transaction_key(tx)] = tx
    return merged


def prune_transactions(
    stored: dict[str, dict[str, Any]],
    today: date,
    keep_days: int,
) -> dict[str, dict[str, Any]]:
    """Drop transactions older than ``keep_days`` (undated ones are kept)."""
    cutoff = today - timedelta(days=keep_days)
    return {
        key: tx
        for key, tx in stored.items()
        if (tx_day := transaction_date(tx)) is None or tx_day >= cutoff
    }


def latest_booked_date(stored: dict[str, dict[str, Any]]) -> date | None:
    days = [d for tx in stored.values() if not is_pending(tx) and (d := transaction_date(tx))]
    return max(days) if days else None


def sorted_transactions(stored: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first; undated transactions last."""
    return sorted(
        stored.values(),
        key=lambda tx: transaction_date(tx) or date.min,
        reverse=True,
    )


def simplify(tx: dict[str, Any]) -> dict[str, Any]:
    """Compact view of a transaction for sensor attributes."""
    tx_day = transaction_date(tx)
    btc = tx.get("bank_transaction_code")
    return {
        "date": tx_day.isoformat() if tx_day else None,
        "amount": signed_amount(tx),
        "currency": currency(tx),
        "label": label(tx),
        "counterparty": counterparty(tx),
        "status": tx.get("status"),
        "bank_transaction_code": btc if isinstance(btc, dict) else None,
        "merchant_category_code": tx.get("merchant_category_code"),
    }


def _is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_is_filled(v) for v in value.values())
    if isinstance(value, list):
        return any(_is_filled(v) for v in value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def field_coverage(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """How many transactions carry each interesting field, plus a few examples.

    This is the diagnostic that tells us whether categorisation can rely on
    ``merchant_category_code`` / ``bank_transaction_code`` or must fall back
    to keyword rules on the label.
    """
    total = len(transactions)
    counts = {
        field: sum(1 for tx in transactions if _is_filled(tx.get(field)))
        for field in COVERAGE_FIELDS
    }
    extra_fields = sorted({key for tx in transactions for key in tx if key not in COVERAGE_FIELDS})
    btc_examples: list[Any] = []
    mcc_examples: list[Any] = []
    for tx in transactions:
        btc = tx.get("bank_transaction_code")
        if _is_filled(btc) and btc not in btc_examples and len(btc_examples) < 10:
            btc_examples.append(btc)
        mcc = tx.get("merchant_category_code")
        if _is_filled(mcc) and mcc not in mcc_examples and len(mcc_examples) < 10:
            mcc_examples.append(mcc)
    return {
        "total": total,
        "filled": counts,
        "other_fields_seen": extra_fields,
        "bank_transaction_code_examples": btc_examples,
        "merchant_category_code_examples": mcc_examples,
    }
