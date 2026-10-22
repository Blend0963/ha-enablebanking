# Changelog

Releases up to and including 0.6.11 are described in the
[GitHub release notes](https://github.com/SurfHost/ha-enablebanking/releases).

## [0.10.2] - 2026-10-22

### Added
- **Transaction search**: "Rechercher une opération" text entity. Every word must appear in the exact label, the merchant, the category or the account name (case and accents ignored); a number ("12,60") also matches the amount. Combines with the category and period filters; empty shows everything. The browser sensor gains a `search` attribute, and the example dashboard a search box on the Opérations view.

## [0.10.1] - 2026-10-22

### Added
- **Account names** in the Configure dialog: one field per account (shown with its IBAN end and current balance). The name is used in every transaction list, the transaction browser and the "Solde total" account list. Without a name, an account shows as "Compte …1234" instead of the holder's name, which two accounts of the same person share.

### Fixed
- Example dashboard: transactions no longer all show as coming from the same account; they use the account name from the integration instead of comparing against a placeholder entity.

## [0.10.0] - 2026-10-22

### Added
- **Monthly budgets.** One "Budget <category>" number per spending category (0 = no budget, value kept across restarts). Category sensors gain `budget`, `budget_remaining`, `budget_used_pct` and `over_budget`; a "Budgets dépassés" sensor counts the categories over budget and lists every budget with its progress, ready for a notification automation.
- **Period filter** for the transaction browser: "Période des opérations" (today, 7 days, 30 days, this month, last month, 3 months, this year, all). The browser sensor now also gives `period`, `sum_out` and `sum_in` for the filtered list.
- **Classify from the dashboard.** "Classer l'opération" shows the selected transaction's category; picking another one stores `MERCHANT = Category` at the top of the rules (the same list as the Configure dialog), re-sorts the whole history at once, without reload or bank call.
- `scripts/generate_dashboard.py` builds the example dashboard; tapping a category tile opens its transactions in a Browser Mod pop-up with the period filter, and a "Budgets du mois" section shows progress bars and lets you set each budget.

### Changed
- Documentation and code examples use fictitious data only.

## [0.9.1] - 2026-10-22

### Added
- **"Solde total" sensor** per bank entry: the signed sum of every account balance (negative when overdrawn), with `overdrawn`, `overdrawn_accounts` and a per-account list.
- `overdrawn` attribute on each balance sensor.

### Fixed
- Overdrafts are always negative: an unsigned balance amount carrying a `DBIT` indicator is now negated, and amounts written with a comma or spaces still parse.

### Changed
- Example dashboard: the total uses "Solde total", overdrawn balances turn red with an "À DÉCOUVERT" label, and the balance chart draws a dashed zero line.

## [0.9.0] - 2026-10-22

### Added
- **Transaction browser**, per bank entry, entirely local (no bank call): a "Filtre des opérations" select (all or one category), "Opération précédente" / "Opération suivante" buttons, and an "Opération sélectionnée" sensor whose attributes hold every detail of the selected transaction, including the exact bank label, plus the filtered list (`operations`, 100 most recent).
- Every transaction in attributes now carries its exact bank `label` and `booking_date`.
- Second view "Opérations" in `examples/dashboard-banque.yaml`.

## [0.8.1] - 2026-10-22

### Added
- `by_category` attribute on each account's transactions sensor (this month's spending per category, for that account only).
- `by_category` and `recent_transactions` (20 most recent, all accounts) on "Dépenses totales du mois". Recent transactions now carry `category_name` and an `icon`.
- `examples/dashboard-banque.yaml`: a banking-app style dashboard (Mushroom + ApexCharts) with balances, monthly spending, category donut, per-account breakdown and latest transactions.

## [0.8.0] - 2026-10-22

### Added
- **Spending categories.** Every stored transaction is sorted into a category from its bank label, since French banks (BNP Paribas, Hello bank!) send no merchant category code over PSD2. The label is split into kind (card payment, direct debit, transfer, withdrawal, fees), merchant and real purchase date (`FACTURE CARTE DU 191026 …`), then rules are applied in order: your own rules, then kind rules (internal transfers, cash, fees, income), then built-in French merchant keywords, then "Autre".
- **Monthly sensors per bank entry**, all accounts together: one "Dépenses <category>" sensor per category (net of refunds, reset on the 1st, `previous_month` and the month's transactions as attributes), "Revenus du mois", "Virements internes du mois" and "Dépenses totales du mois" (with a `by_category` breakdown).
- **Your own rules** in the entry's **Configure** dialog, one `KEYWORD = Category` per line. They are checked first, apply at once to the whole history (no bank call), and a new category name creates a new sensor. The "Dépenses Autre" sensor lists the merchants no rule caught in `unmatched_merchants`.
- Transfers between your own accounts (`VIR CPTE A CPTE`, or a transfer from/to the account holder's name) count neither as spending nor as income.

### Changed
- Months are counted by purchase date, not booking date, so a card payment on the 30th booked on the 2nd stays in the right month.
- The transactions sensor's recent list now shows merchant, category and account.

## [0.7.0] - 2026-10-22

### Added
- **Transactions.** Each scheduled poll now also lists the transactions of every account whose balance it just refreshed: 90 days back on the first run, then from 7 days before the newest booked transaction. They ride on the existing 10:00 / 14:00 / 18:00 / 22:00 polls, after the balances, so there is no extra poll and a transactions failure (429, unsupported endpoint) can never cost a balance update. Transactions have their own 4-hour back-off and their own on-disk store (`.storage/enablebanking.<entry_id>.transactions`, 400 days kept). Pending transactions are replaced on each fetch so they are not counted twice once booked.
- **Transactions sensor per account.** State: number of transactions this month. Attributes: `month_spent`, `month_received`, `stored_transactions`, `last_fetched_at`, `last_error`, the 10 most recent transactions in compact form, and `field_coverage` (how many transactions carry each field). The two bulky attributes are kept out of the recorder.
- **Diagnostics download** with per-account field coverage and up to 30 raw transactions (IBANs and credentials redacted), to see what each bank really sends before building categories on top of it.

## [0.6.13] - 2026-10-11

### Fixed
- **Consent could not be renewed before it expired.** The 14-day expiry warning told you to click **Reconfigure**, but the integration had no reconfigure step, so Home Assistant never offered one. Renewal was only possible after the consent had lapsed and the reauth card appeared, by which time balances were already going stale. Each bank entry now has **Reconfigure** in its **⋮** menu. It reuses the stored private key and application ID, goes straight to a new bank link, and swaps in the new session once you paste the code. Reauth could not do this: its fast path revalidates the still-live session and finishes without reaching the bank.
- **The expiry warning pointed at a button that does not exist.** It now links to the Enable Banking integration page and names the **⋮** menu, and it disappears by itself once the consent is renewed instead of lingering until dismissed.
- **The reauth success message was not shown.** Since Home Assistant 2026.10, a flow that leaves the abort reason out gets Home Assistant's generic text instead of the integration's own. The reason is now passed explicitly, so "your balances refresh on the next scheduled poll" shows again after reauth, and after reconfigure too.

## [0.6.12] - 2026-10-11

### Fixed
- **CI failed on Home Assistant 2026.10.** Home Assistant replaced voluptuous with probatio in 2026.9, and since 2026.10 `async_show_form` is typed to take a `probatio.Schema`. The config flow still imported `voluptuous`, so strict `mypy` rejected every form schema. It now imports `probatio` and falls back to `voluptuous` on Home Assistant 2026.4 to 2026.8, which do not ship probatio. Behaviour is unchanged: at runtime Home Assistant already redirects `voluptuous` imports to probatio.
