# Changelog

Releases up to and including 0.6.11 are described in the
[GitHub release notes](https://github.com/SurfHost/ha-enablebanking/releases).

## [0.6.12] - 2026-10-11

### Fixed
- **CI failed on Home Assistant 2026.10.** Home Assistant replaced voluptuous with probatio in 2026.9, and since 2026.10 `async_show_form` is typed to take a `probatio.Schema`. The config flow still imported `voluptuous`, so strict `mypy` rejected every form schema. It now imports `probatio` and falls back to `voluptuous` on Home Assistant 2026.4 to 2026.8, which do not ship probatio. Behaviour is unchanged: at runtime Home Assistant already redirects `voluptuous` imports to probatio.
