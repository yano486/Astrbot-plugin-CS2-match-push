# CS2 Multi-Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve existing PandaScore functionality and add HLTV-only and dual-source operation in the existing management panel.

**Architecture:** Keep PandaScoreClient unchanged behind a source router. HLTV returns the same match/team/tournament shape; the router qualifies HLTV IDs, persists conservative cross-source identities, reports per-source health, and presents canonical records to the existing scheduler. Existing numeric PandaScore IDs remain valid and mean PandaScore, while HLTV-only IDs use `hltv:<positive integer>`.

**Tech Stack:** Python 3.12+, asyncio, aiohttp, Pillow, optional curl_cffi/Playwright, selectolax, HTML/ES modules, standard-library unittest and node:test.

**Spec:** `docs/superpowers/specs/2026-10-29-cs2-multi-source-design.md`

## Global Constraints

- Use the independent `astrbot_plugin_cs2` identifier and data directory; preserve this installation's user configuration, group subscriptions, notifications, and existing Chinese/English text conventions.
- Default old configurations to `data_source_mode=pandascore`, `primary_source=pandascore`.
- No phone access or real QQ sends during local development; Git publication requires explicit user authorization.
- Keep PandaScore numeric IDs backwards-compatible; qualify HLTV IDs before they enter persistent state.
- Confirmed duplicates share a persisted canonical ID; ambiguous matches remain separate and visibly flagged.
- Separate HLTV star filtering from PandaScore S/A/B tiers.
- Do not downgrade Pillow or AstrBot dependencies; isolate local development dependencies.
- No automatic CAPTCHA solving, proxy rotation, global mounts, or public unauthenticated VNC listener.

## Shared Interfaces

```python
# hltv_client.py: raw IDs are positive integers; router qualifies them.
class HltvClient:
    def __init__(self, config: dict): ...
    async def get_upcoming_matches(self, per_page=50, *, until=None): ...
    async def get_upcoming_tournaments(self, per_page=50): ...
    async def search_teams(self, query, per_page=20): ...
    async def get_team(self, team_id_or_slug): ...
    async def get_match_result(self, match_id): ...
    async def fetch_bytes(self, url, max_bytes=2 * 1024 * 1024): ...
    async def manual_verify(self): ...
    async def close(self): ...
    def status(self) -> dict: ...

# data_sources.py: matches PandaScoreClient public methods above.
class SourceRouter:
    def __init__(self, config: dict, store, *, clients=None): ...
    def status(self) -> dict: ...
    async def check_sources(self) -> dict: ...
    async def manual_verify(self) -> dict: ...
    async def close(self): ...
```

Source config names: `data_source_mode`, `primary_source`, `hltv_proxy_url`, `hltv_browser_fallback`, `hltv_headful`, `hltv_request_min_gap`, `hltv_cache_ttl`, `hltv_min_stars`. Defaults are `pandascore`, `pandascore`, empty proxy, true, false, 90 seconds, 180 seconds, 1 star.

`GET /api/config` adds `source_configured` (configuration completeness, not connectivity) and `source_status` mapping. `POST /api/setup/check` accepts candidate mode/primary/token/HLTV settings and returns `source`, `sources`, `groups`, `platform`. `POST /api/sources/check` checks saved sources; `POST /api/sources/verify` starts explicit human verification. Source health includes `enabled`, `state`, `message`, `last_success`; no credentials.

### Task 1: Test baseline and configuration/persistence

**Files:** create `tests/support.py`, `tests/test_baseline.py`, `tests/test_source_config.py`; modify `config.py`, `store.py`, `_conf_schema.json`.

- [ ] Characterize existing store save/load, delivery target receipts, and PandaScore configuration validation using real temporary files.
- [ ] Add a failing test proving source mode validation and source-safe string IDs are missing:

```python
self.assertEqual(validate_config({'data_source_mode': 'hltv'})['data_source_mode'], 'hltv')
with self.assertRaises(ConfigError):
    validate_config({'data_source_mode': 'unknown'})
```

- [ ] Run `python -m unittest discover -s tests -p 'test_source_config.py' -v` and record RED.
- [ ] Implement validated source settings, secret redaction, source-safe identity parsing and idempotent additive persistent fields without rewriting legacy IDs.
- [ ] Repeat baseline and configuration tests and record GREEN.

### Task 2: HLTV transport/parser/client (independent worker)

**Files:** create `hltv_fetcher.py`, `hltv_parser.py`, `hltv_client.py`, `tests/test_hltv.py`, `tests/fixtures/hltv/`, `THIRD_PARTY_NOTICES.md`; modify no existing business files.

- [ ] Write local fixture tests for upcoming match, event/team/detail parsing, missing fields, a genuine empty page and a Cloudflare challenge; run them before implementation.
- [ ] Implement public HLTV client interfaces above. Emit PandaScore-compatible records with `_source='hltv'`, `_stars`, source URL and UTC timestamps, never an invented S/A tier.
- [ ] Implement cached, globally throttled curl_cffi transport with lazy shared Chromium fallback, allowlisted HTTPS targets, challenge detection, bounded backoff and credential-safe errors. Human verification is explicit and does not open network listeners.
- [ ] Verify basic offline behavior with an injected transport and real parsers:

```python
result = await client.get_upcoming_matches(until=cutoff)
self.assertEqual(result[0]['opponents'][0]['opponent']['name'], 'Alpha')
self.assertIsNone(await blocked_client.get_upcoming_matches())
```

- [ ] Run `python -m unittest discover -s tests -p 'test_hltv.py' -v` and document RED/GREEN.

### Task 3: Identity and source routing

**Files:** create `source_identity.py`, `data_sources.py`, `tests/test_sources.py`.

- [ ] Test all three modes with injected raw clients, including the absence of any HLTV initialization in PandaScore-only mode.
- [ ] Test namespaced ID collisions, strict one-to-one cross-source association, ambiguous duplicate warnings, persisted identities after reload, primary field conflicts and missing-source failures.

```python
rows = await router.get_upcoming_matches(until=cutoff)
self.assertEqual(len(rows), 1)
self.assertEqual(rows[0]['_sources'], ['pandascore', 'hltv'])
```

- [ ] Run router tests RED before adding modules. Implement conservative matching: exact normalized teams (including academy/women qualifiers), compatible event and BO, bounded kickoff gap, no multi-candidate association.
- [ ] Keep canonical routing aliases persisted, reconcile legacy per-target receipts when association is discovered, and expose independent source health. Treat failed fetch as `None`, valid empty fetch as `[]`.
- [ ] Run Task 1-3 tests GREEN.

### Task 4: Scheduler and authenticated Web APIs

**Files:** modify `main.py`, `web_panel.py`, `plugin_view.py`; create `tests/test_integration.py`.

- [ ] Write failing integration tests for token-free HLTV mode, HLTV star filtering, old notifications after source/primary switches, partial-source failure retention, candidate source checks and opaque Web API IDs.
- [ ] Swap source clients by generation, cancel old pending background work, retain notification records, and prevent source-switch-only reschedule notifications.
- [ ] Preserve future reminders, result polling, daily reports, team follow/unfollow, images and tournament announcements against normalized records.
- [ ] Add the shared source endpoints and config fields; both embedded and independent panels call the same methods. A config change that cannot be applied restores the previous runtime settings and saved values.
- [ ] Run all Python tests; no real QQ messages or website traffic.

### Task 5: Management UI and guided setup (independent worker)

**Files:** modify only `pages/management/index.html`, `app.js`, `setup.js`, `style.css`; create `pages/management/source-ui.js`, `tests/test_source_ui.mjs`.

- [ ] Use node:test for pure mode/form/status helpers and run RED before implementation.
- [ ] Add mode/primary controls and HLTV options within existing forms, per-source checks/status, source/conflict/cache badges and source-aware team/match actions.
- [ ] Make onboarding Token requirement conditional on the selected mode and persist the mode even without a new Token. Use `source_configured` for readiness; never treat connectivity as implicit from config completeness.
- [ ] Source config/current health contract is defined under Shared Interfaces. Encode opaque path IDs instead of converting match/team IDs with `Number`.
- [ ] Run `node --test tests/test_source_ui.mjs` plus JS syntax checks; keep existing visual language and mobile layout.

### Task 6: Whole-feature verification, documentation and review

**Files:** modify `README.md`, `CHANGELOG.md`, `requirements.txt`, `metadata.yaml`; create local review/verification notes under `docs/superpowers/`.

- [ ] Declare compatible HLTV dependencies without a Pillow downgrade. Preserve plugin identifier and persistent data path; point repository metadata at the user's fork only when preparing this fork's release.
- [ ] Document source choice, conditional Token setup, browser installation, manual verification, stale data, ID association limits, migration backup and rollback.
- [ ] Run Python suite, node tests, Python AST/compile checks and JS syntax checks; visually verify current panel at desktop/mobile if browser tooling is available.
- [ ] Perform an independent review of correctness, security and full spec coverage, fix findings with covering tests and rerun all checks.
- [ ] Report actual verification results and remaining real-network/phone checks. Do not claim installation success, live HLTV access or push/deployment.

## Execution Decisions

- Existing source archive remains the implementation workspace; no additional branch/worktree or phone modification is authorized.
- Use standard-library unittest so tests do not require AstrBot's full server stack. Test-only AstrBot API stubs live in `tests/support.py`; production never contains stubs.
- Skills' automatic commits and scratch deletion are omitted because the user explicitly requires separate permission for those operations.
