"""Transaction categorisation.

Pure Python (no Home Assistant imports) so it can be tested on its own.

French banks (BNP Paribas, Hello bank!, …) send no merchant category code and
no counterparty name over PSD2: the label (``remittance_information``) is the
only usable signal. It is very regular, e.g.

    FACTURE CARTE DU 140326 BOULANGERIE DU PORT CARTE 1234XXXXXXXX0000
    FACTURE CARTE LE PETIT BISTROT LYON 69001          (pending)
    PRLV SEPA ACME ENERGIE ECH/140326 ID EMETTEUR/…
    VIR CPTE A CPTE RECU /DE DUPONT JEAN /MOTIF …
    VIR SEPA RECU /DE ACME SAS /MOTIF SALAIRE …

so categorisation works in three steps:

1. ``parse_label`` splits the label into an operation kind (card payment,
   direct debit, transfer, withdrawal, fees), the merchant and, for card
   payments, the real purchase date (the booking date can be days later).
2. ``Categorizer.categorize`` applies rules in order, first match wins:
   the user's own rules, then kind-based rules (internal transfers,
   withdrawals, fees, income), then the default keyword rules, then "other".
3. ``summarize`` adds up a month per category.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .transactions import label as tx_label
from .transactions import signed_amount, transaction_date

# --------------------------------------------------------------------------- #
# Categories                                                                   #
# --------------------------------------------------------------------------- #

# Special categories (decided by kind, not by keywords).
CAT_INTERNAL = "virements_internes"
CAT_INCOME = "revenus"
CAT_OTHER = "autre"
CAT_CASH = "especes"
CAT_FEES = "frais_bancaires"
CAT_TRANSFERS_OUT = "virements_emis"

# slug -> (display name, icon). Order is the display order.
DEFAULT_CATEGORIES: dict[str, tuple[str, str]] = {
    "courses": ("Courses", "mdi:cart"),
    "restaurants": ("Restaurants et cafés", "mdi:silverware-fork-knife"),
    "transport": ("Transport", "mdi:car"),
    "logement": ("Logement et énergie", "mdi:home-lightning-bolt"),
    "abonnements": ("Abonnements et tech", "mdi:repeat"),
    "shopping": ("Shopping", "mdi:shopping"),
    "sante": ("Santé", "mdi:medical-bag"),
    "loisirs": ("Loisirs et sorties", "mdi:ticket"),
    "voyages": ("Voyages", "mdi:airplane"),
    "impots": ("Impôts et administration", "mdi:bank"),
    CAT_CASH: ("Espèces", "mdi:cash"),
    CAT_FEES: ("Frais bancaires", "mdi:bank-minus"),
    CAT_TRANSFERS_OUT: ("Virements émis", "mdi:bank-transfer-out"),
    CAT_OTHER: ("Autre", "mdi:help-circle-outline"),
    CAT_INCOME: ("Revenus", "mdi:cash-plus"),
    CAT_INTERNAL: ("Virements internes", "mdi:swap-horizontal"),
}

# Categories that never count as spending.
NON_EXPENSE_CATEGORIES = frozenset({CAT_INCOME, CAT_INTERNAL})

# Default keyword rules, checked in this order (first match wins), so put the
# more specific keyword first when two categories could both match
# (e.g. "UBER EATS" before "UBER"). Keywords are matched as whole words on the
# normalised label (upper case, no accents, punctuation turned into spaces).
DEFAULT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "restaurants",
        (
            "UBER EATS",
            "DELIVEROO",
            "JUST EAT",
            "KAYSER",
            "PAUL",
            "BRIOCHE DOREE",
            "BOULANGERIE",
            "PATISSERIE",
            "STARBUCKS",
            "MAXICOFFEE",
            "SELECTA",
            "MCDONALD",
            "MCDONALDS",
            "MC DONALD",
            "BURGER KING",
            "KFC",
            "QUICK",
            "FIVE GUYS",
            "SUBWAY",
            "DOMINOS",
            "PIZZA",
            "SUSHI",
            "RESTO",
            "REST",
            "RESTAURANT",
            "BRASSERIE",
            "BISTROT",
            "BISTRO",
            "CAFE",
            "TRAITEUR",
            "CREPERIE",
            "KEBAB",
        ),
    ),
    (
        "courses",
        (
            "MONOPRIX",
            "CARREFOUR",
            "LECLERC",
            "AUCHAN",
            "INTERMARCHE",
            "LIDL",
            "ALDI",
            "FRANPRIX",
            "CASINO",
            "SUPER U",
            "HYPER U",
            "U EXPRESS",
            "PICARD",
            "NATURALIA",
            "BIOCOOP",
            "GRAND FRAIS",
            "LA GRANDE EPICERIE",
            "G20",
            "COCCINELLE",
            "NETTO",
            "LEADER PRICE",
            "HELLOFRESH",
            "QUITOQUE",
        ),
    ),
    (
        "transport",
        (
            "UBER",
            "BOLT",
            "HEETCH",
            "G7",
            "TAXI",
            "SNCF",
            "OUIGO",
            "TRAINLINE",
            "RATP",
            "NAVIGO",
            "IDFM",
            "ILE DE FRANCE MOBILITES",
            "TRANSILIEN",
            "ULYS",
            "VINCI AUTOROUTES",
            "SANEF",
            "APRR",
            "PEAGE",
            "TOTALENERGIES",
            "ESSO",
            "SHELL",
            "AVIA",
            "CARBURANT",
            "BLABLACAR",
            "LIME",
            "DOTT",
            "PARKING",
            "INDIGO",
            "SAEMES",
            "ONEPARK",
            "ZENPARK",
            "EASYPARK",
            "PAYBYPHONE",
            "FLOWBIRD",
        ),
    ),
    (
        "logement",
        (
            "LOYER",
            "EDF",
            "ENGIE",
            "EKWATEUR",
            "VEOLIA",
            "SUEZ",
            "EAU DE PARIS",
            "FONCIA",
            "NEXITY",
            "SYNDIC",
            "ASSURANCE HABITATION",
            "LEROY MERLIN",
            "CASTORAMA",
            "BRICORAMA",
        ),
    ),
    (
        "abonnements",
        (
            "NETFLIX",
            "SPOTIFY",
            "DEEZER",
            "DISNEY PLUS",
            "DISNEYPLUS",
            "CANAL",
            "PRIME VIDEO",
            "AMAZON PRIME",
            "APPLE COM",
            "APPLE",
            "ITUNES",
            "GOOGLE",
            "YOUTUBE",
            "MISTRAL AI",
            "MISTRAL",
            "OPENAI",
            "CHATGPT",
            "ANTHROPIC",
            "CLAUDE AI",
            "NVIDIA",
            "ELEVENLABS",
            "GITHUB",
            "MICROSOFT",
            "ADOBE",
            "OVH",
            "FREE MOBILE",
            "FREE TELECOM",
            "FREE",
            "ORANGE",
            "SOSH",
            "SFR",
            "BOUYGUES",
            "RED BY SFR",
        ),
    ),
    (
        "shopping",
        (
            "AMAZON",
            "ZALANDO",
            "FNAC",
            "DARTY",
            "BOULANGER",
            "DECATHLON",
            "IKEA",
            "ACTION",
            "PRIMARK",
            "ZARA",
            "UNIQLO",
            "KIABI",
            "VINTED",
            "CDISCOUNT",
            "ALIEXPRESS",
            "SHEIN",
            "TEMU",
            "SEPHORA",
            "NOCIBE",
            "LEBONCOIN",
        ),
    ),
    (
        "sante",
        (
            "PHARMACIE",
            "PHARMA",
            "DOCTOLIB",
            "MEDECIN",
            "DENTISTE",
            "DENTAIRE",
            "OPTICIEN",
            "OPTIC",
            "KRYS",
            "AFFLELOU",
            "LABORATOIRE",
            "LABO",
            "HOPITAL",
            "CLINIQUE",
            "MUTUELLE",
            "KINE",
        ),
    ),
    (
        "loisirs",
        (
            "PARC ASTERIX",
            "DISNEYLAND",
            "SHOTGUN",
            "TICKETMASTER",
            "FNAC SPECTACLES",
            "UGC",
            "PATHE",
            "GAUMONT",
            "MK2",
            "CINEMA",
            "STEAM",
            "PLAYSTATION",
            "NINTENDO",
            "XBOX",
            "BASIC FIT",
            "FITNESS PARK",
            "NEONESS",
            "MUSEE",
            "BOWLING",
        ),
    ),
    (
        "voyages",
        (
            "AIRBNB",
            "BOOKING",
            "HOTEL",
            "IBIS",
            "NOVOTEL",
            "AIR FRANCE",
            "EASYJET",
            "RYANAIR",
            "TRANSAVIA",
            "VOLOTEA",
            "VUELING",
            "EXPEDIA",
            "AEROPORT",
        ),
    ),
    (
        "impots",
        (
            "DGFIP",
            "IMPOT",
            "IMPOTS",
            "TRESOR PUBLIC",
            "FINANCES PUBLIQUES",
            "ANTAI",
            "AMENDE",
            "URSSAF",
            "ANTS",
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Label parsing                                                                #
# --------------------------------------------------------------------------- #

KIND_CARD = "card"
KIND_DIRECT_DEBIT = "direct_debit"
KIND_TRANSFER_IN = "transfer_in"
KIND_TRANSFER_OUT = "transfer_out"
KIND_INTERNAL = "internal_transfer"
KIND_WITHDRAWAL = "withdrawal"
KIND_FEES = "fees"
KIND_OTHER = "other"

# "FACTURE CARTE DU 140326 <merchant> [CARTE 1234XXXXXXXX0000 [FRA 11,95EUR]]"
_CARD_RE = re.compile(r"^(?:FACTURE|PAIEMENT|ACHAT)\s+(?:PAR\s+)?CARTE\s+(?:DU\s+(\d{6})\s+)?(.*)$")
_CARD_SUFFIX_RE = re.compile(r"\s+CARTE\s+\d{4}X+\d{2,4}\b.*$")
_TRAILING_ZIP_RE = re.compile(r"\s+\d{5}$")
_TRAILING_AMOUNT_RE = re.compile(r"\s+[A-Z]{3}\s+[\d.,]+[A-Z]{3}$")
_DIRECT_DEBIT_RE = re.compile(
    r"^PRLV\s+(?:SEPA\s+)?(.*?)(?:\s+(?:ECH|ID EMETTEUR|MDT|REF|LIB)/.*)?$"
)
_TRANSFER_PARTY_RE = re.compile(r"/(?:DE|BEN|A|POUR)\s+([^/]+)")
_TRANSFER_REASON_RE = re.compile(r"/MOTIF\s+([^/]+)")
_WITHDRAWAL_RE = re.compile(r"^(?:RETRAIT|RET)\s+(?:DAB|GAB|ESPECES)")
_FEES_RE = re.compile(
    r"^(?:COMMISSION|COMMISSIONS|FRAIS|COTISATION|COTIS|AGIOS|INTERETS DEBITEURS|"
    r"PRIME ASSURANCE|FORFAIT)\b"
)


def normalize(text: str) -> str:
    """Upper case, no accents, punctuation as spaces, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^A-Z0-9]+", " ", no_accents.upper())
    return cleaned.strip()


@dataclass(slots=True)
class ParsedLabel:
    kind: str
    merchant: str
    purchase_date: date | None = None
    reason: str = ""


def _parse_ddmmyy(raw: str) -> date | None:
    try:
        return date(2000 + int(raw[4:6]), int(raw[2:4]), int(raw[0:2]))
    except ValueError:
        return None


def parse_label(raw_label: str, is_debit: bool) -> ParsedLabel:
    """Split a bank label into kind, merchant and purchase date."""
    text = " ".join(raw_label.upper().split())

    if match := _CARD_RE.match(text):
        purchase = _parse_ddmmyy(match.group(1)) if match.group(1) else None
        merchant = _CARD_SUFFIX_RE.sub("", match.group(2))
        merchant = _TRAILING_AMOUNT_RE.sub("", merchant)
        merchant = _TRAILING_ZIP_RE.sub("", merchant).strip()
        return ParsedLabel(KIND_CARD, merchant, purchase)

    if text.startswith("PRLV"):
        match = _DIRECT_DEBIT_RE.match(text)
        merchant = match.group(1).strip() if match else text
        return ParsedLabel(KIND_DIRECT_DEBIT, merchant)

    if _WITHDRAWAL_RE.match(text):
        return ParsedLabel(KIND_WITHDRAWAL, "Retrait")

    if _FEES_RE.match(text):
        return ParsedLabel(KIND_FEES, text)

    if text.startswith(("VIR ", "VIREMENT")):
        party = _TRANSFER_PARTY_RE.search(text)
        reason = _TRANSFER_REASON_RE.search(text)
        kind = KIND_TRANSFER_OUT if is_debit else KIND_TRANSFER_IN
        if "CPTE A CPTE" in text:
            kind = KIND_INTERNAL
        return ParsedLabel(
            kind,
            party.group(1).strip() if party else "",
            reason=reason.group(1).strip() if reason else "",
        )

    return ParsedLabel(KIND_OTHER, text)


# --------------------------------------------------------------------------- #
# Rules                                                                        #
# --------------------------------------------------------------------------- #


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    words = normalize(keyword).split()
    return re.compile(r"(?<![A-Z0-9])" + r" ".join(map(re.escape, words)) + r"(?![A-Z0-9])")


def slugify_category(name: str) -> str:
    return normalize(name).lower().replace(" ", "_") or CAT_OTHER


def parse_user_rules(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse ``KEYWORD = Catégorie`` lines.

    Returns ``(rules, errors)`` where each rule is ``(keyword, category name)``.
    Blank lines and lines starting with ``#`` are ignored.
    """
    rules: list[tuple[str, str]] = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, sep, category = stripped.partition("=")
        if not sep or not normalize(keyword) or not category.strip():
            errors.append(f"line {number}: {stripped}")
            continue
        rules.append((keyword.strip(), category.strip()))
    return rules, errors


@dataclass(slots=True)
class CategorizedTransaction:
    raw: dict[str, Any]
    category: str
    kind: str
    merchant: str
    day: date | None
    amount: float
    account: str = ""
    account_id: str = ""


@dataclass(slots=True)
class Categorizer:
    """Applies the user's rules, then built-in ones. Build once, reuse."""

    user_rules: list[tuple[str, str]] = field(default_factory=list)
    owner_names: Iterable[str] = ()
    # slug -> display name, defaults plus any category the user introduced.
    categories: dict[str, str] = field(init=False)
    _compiled: list[tuple[re.Pattern[str], str]] = field(init=False)
    _owners: list[re.Pattern[str]] = field(init=False)
    _user_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.categories = {slug: name for slug, (name, _icon) in DEFAULT_CATEGORIES.items()}
        by_display = {normalize(name): slug for slug, name in self.categories.items()}
        compiled: list[tuple[re.Pattern[str], str]] = []
        for keyword, category_name in self.user_rules:
            slug = by_display.get(normalize(category_name)) or slugify_category(category_name)
            if slug not in self.categories:
                self.categories[slug] = category_name
                by_display[normalize(category_name)] = slug
            compiled.append((_keyword_pattern(keyword), slug))
        self._user_count = len(compiled)
        for slug, keywords in DEFAULT_RULES:
            compiled.extend((_keyword_pattern(k), slug) for k in keywords)
        self._compiled = compiled
        self._owners = [_keyword_pattern(n) for n in self.owner_names if normalize(n)]

    def _match(self, text: str, user_only: bool = False) -> str | None:
        rules = self._compiled[: self._user_count] if user_only else self._compiled
        for pattern, slug in rules:
            if pattern.search(text):
                return slug
        return None

    def categorize(
        self, tx: dict[str, Any], account: str = "", account_id: str = ""
    ) -> CategorizedTransaction:
        amount = signed_amount(tx) or 0.0
        raw_label = tx_label(tx)
        parsed = parse_label(raw_label, is_debit=amount < 0)
        text = normalize(raw_label)
        day = parsed.purchase_date or transaction_date(tx)

        def result(category: str) -> CategorizedTransaction:
            return CategorizedTransaction(
                raw=tx,
                category=category,
                kind=parsed.kind,
                merchant=parsed.merchant.title() if parsed.merchant else raw_label,
                day=day,
                amount=amount,
                account=account,
                account_id=account_id,
            )

        # 1. The user's own rules always win.
        if slug := self._match(text, user_only=True):
            return result(slug)

        # 2. Kind-based rules.
        if parsed.kind == KIND_INTERNAL or (
            parsed.kind in (KIND_TRANSFER_IN, KIND_TRANSFER_OUT)
            and any(p.search(normalize(parsed.merchant)) for p in self._owners)
        ):
            return result(CAT_INTERNAL)
        if parsed.kind == KIND_WITHDRAWAL:
            return result(CAT_CASH)
        if parsed.kind == KIND_FEES:
            return result(CAT_FEES)
        if parsed.kind == KIND_TRANSFER_IN:
            return result(CAT_INCOME)

        # 3. Default keyword rules. Card refunds land in the merchant's
        #    category as a positive amount, reducing that category's spending.
        #    Card, direct debit and outgoing transfer labels carry free text
        #    (transfer reason, mandate refs) that must not trigger a keyword,
        #    so match on the parsed merchant for those.
        target = (
            normalize(parsed.merchant)
            if parsed.kind in (KIND_CARD, KIND_DIRECT_DEBIT, KIND_TRANSFER_OUT)
            else text
        )
        if slug := self._match(target):
            return result(slug)

        if parsed.kind == KIND_TRANSFER_OUT:
            return result(CAT_TRANSFERS_OUT)
        if amount > 0 and parsed.kind != KIND_CARD:
            return result(CAT_INCOME)
        return result(CAT_OTHER)


# --------------------------------------------------------------------------- #
# Monthly summary                                                              #
# --------------------------------------------------------------------------- #


def month_start(day: date) -> date:
    return day.replace(day=1)


def previous_month_start(day: date) -> date:
    first = month_start(day)
    return (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )


def in_month(item: CategorizedTransaction, first_day: date) -> bool:
    return item.day is not None and month_start(item.day) == first_day


def category_total(items: Iterable[CategorizedTransaction], category: str) -> float:
    """Net amount for a category, positive = money out (spending)."""
    total = -sum(i.amount for i in items if i.category == category)
    if category == CAT_INCOME:
        total = -total
    return round(total, 2)


def spending_total(items: Iterable[CategorizedTransaction]) -> float:
    return round(-sum(i.amount for i in items if i.category not in NON_EXPENSE_CATEGORIES), 2)


def income_total(items: Iterable[CategorizedTransaction]) -> float:
    return round(sum(i.amount for i in items if i.category == CAT_INCOME), 2)


def compact(item: CategorizedTransaction, names: dict[str, str] | None = None) -> dict[str, Any]:
    default = DEFAULT_CATEGORIES.get(item.category)
    name = (names or {}).get(item.category) or (default[0] if default else item.category)
    return {
        "date": item.day.isoformat() if item.day else None,
        "amount": item.amount,
        "merchant": item.merchant,
        "category": item.category,
        "category_name": name,
        "icon": default[1] if default else "mdi:tag",
        "account": item.account,
        "label": tx_label(item.raw),
        "booking_date": item.raw.get("booking_date"),
        "pending": item.raw.get("status") == "PDNG",
    }


def by_category(items: Iterable[CategorizedTransaction], names: dict[str, str]) -> dict[str, float]:
    """Spending per category display name, largest first (non-zero only)."""
    item_list = list(items)
    totals = {
        name: total
        for slug, name in names.items()
        if slug not in NON_EXPENSE_CATEGORIES and (total := category_total(item_list, slug))
    }
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))
