# Changelog

Releases up to and including 0.6.11 are described in the
[GitHub release notes](https://github.com/SurfHost/ha-enablebanking/releases).

## [0.6.13] - 2026-10-11

### Fixed
- **Consent could not be renewed before it expired.** The 14-day expiry warning told you to click **Reconfigure**, but the integration had no reconfigure step, so Home Assistant never offered one. Renewal was only possible after the consent had lapsed and the reauth card appeared, by which time balances were already going stale. Each bank entry now has **Reconfigure** in its **⋮** menu. It reuses the stored private key and application ID, goes straight to a new bank link, and swaps in the new session once you paste the code. Reauth could not do this: its fast path revalidates the still-live session and finishes without reaching the bank.
- **The expiry warning pointed at a button that does not exist.** It now links to the Enable Banking integration page and names the **⋮** menu, and it disappears by itself once the consent is renewed instead of lingering until dismissed.
- **The reauth success message was not shown.** Since Home Assistant 2026.10, a flow that leaves the abort reason out gets Home Assistant's generic text instead of the integration's own. The reason is now passed explicitly, so "your balances refresh on the next scheduled poll" shows again after reauth, and after reconfigure too.

## [0.6.12] - 2026-10-11

### Fixed
- **CI failed on Home Assistant 2026.10.** Home Assistant replaced voluptuous with probatio in 2026.9, and since 2026.10 `async_show_form` is typed to take a `probatio.Schema`. The config flow still imported `voluptuous`, so strict `mypy` rejected every form schema. It now imports `probatio` and falls back to `voluptuous` on Home Assistant 2026.4 to 2026.8, which do not ship probatio. Behaviour is unchanged: at runtime Home Assistant already redirects `voluptuous` imports to probatio.
