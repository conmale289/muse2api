# Observability & Metrics — Round-2 Stability Follow-ups + Cheap New Features

**Date**: 08-11-26
**Complexity**: Complex (two mandatory phases, one optional/stretch item at the end of Phase 2 — not a phase program; no `process/features/` folder exists for this and none is warranted)
**Status**: ✅ VERIFIED — all 7 items (Phase 1 items 1-4, Phase 2 items 5-7 including the optional/stretch `/metrics` endpoint) implemented and verified in the 08-11-26 EXECUTE session. `pytest -q` is green (206 passed, 0 failed). Manual-evidence checks (consecutive-failure recycle + reset, 429 headers, `/admin/status` observability block, `/readyz` liveness swap, `/metrics` body) were run end-to-end via direct Python calls in lieu of a live Chromium/server session (no real browser available in this execution environment) and all behaved as specified. No deviations from the plan.

**Context note**: this repo has no `process/context/all-context.md` router (only `process/general-plans/` exists under `process/`). All context for this plan was gathered directly from the source files cited below, read live at plan time (08-11-26). A sibling plan, `process/general-plans/active/stability_performance_PLAN_08-11-26.md`, was already executed and committed in this session — this plan builds on top of its already-merged code (`_admit_task`/`TASK_ADMISSION_LOCK`, `_admit_chat`/`_release_chat`/`_CHAT_ACTIVE`, `MuseEngine.is_healthy()`/`.recycle()`/`.uptime_seconds`, `_browser_health_check_sync()`) but is a **separate, independent plan file** — do not merge the two.

## Quick Links

- [Overview](#overview)
- [Scope](#scope)
- [Phase Completion Rules](#phase-completion-rules)
- [Execution Brief](#execution-brief)
- [Touchpoints](#touchpoints)
- [Public Contracts](#public-contracts)
- [Blast Radius](#blast-radius)
- [Phase 1: Round-2 Stability Follow-ups](#phase-1-round-2-stability-follow-ups)
- [Phase 2: Cheap New Features](#phase-2-cheap-new-features)
- [Implementation Checklist](#implementation-checklist)
- [Acceptance Criteria](#acceptance-criteria)
- [Verification Evidence](#verification-evidence)
- [Risks and Mitigations](#risks-and-mitigations)
- [Resume and Execution Handoff](#resume-and-execution-handoff)
- [Cursor + RIPER-5 Guidance](#cursor--riper-5-guidance)

## Overview

muse2api drives one shared headless Chromium via CDP to automate muse.ai as an OpenAI-compatible API (`app.py`, `engine.py`, `store.py`, `proxy.py`, `cdp.py`, `config.py`). Two completed research passes this session identified 7 follow-up items, split into two mandatory phases plus one explicitly optional/stretch item:

**Phase 1 (mandatory — round-2 stability follow-ups)**:
1. A consecutive-failure "wedged page" recycle trigger — `is_healthy()` only pings the browser-process-level `/json/version` endpoint, so a single wedged renderer/page behind an otherwise-healthy process is invisible to it.
2. `/readyz` and `/admin/status` still compute `browser_running` from the old `bool(engine.proc and engine.proc.poll() is None)` check instead of the new `is_healthy()`.
3. Observability counters (admission rejections, recycle count/reason/time) surfaced via `/admin/status`, plus a log line for 401/429/5xx responses that today vanish silently.
4. A regression test proving chat admission (`_admit_chat`/`_CHAT_ACTIVE`) and task admission (`_admit_task`'s `store.tasks` scan) are independent.

**Phase 2 (mandatory — cheap new features)**:
5. OpenAI-style rate-limit headers (`Retry-After`, `X-RateLimit-*`) on every 429 this service already raises.
6. Per-account `use_count` surfaced in the admin UI (`last_used` is already surfaced — see Phase 2 item 6's "already done" finding below).
7. **(Optional/stretch)** A hand-written `/metrics` Prometheus text-exposition endpoint built from items 3/5/6's counters plus `Store.stats()` — only because it composes cleanly with no new dependency; otherwise this would be flagged as a follow-up rather than forced in.

This is a single execution stream: Phase 1 and Phase 2 touch almost entirely disjoint code (Phase 1 lives in `_run_generation`/`safe_chat_stream`/`_browser_health_check_sync`/`config.py`; Phase 2 lives in `_admit_task`/`_admit_chat`'s raise sites, `admin.html`, and a brand-new route), so they can be implemented in either order, but are specified Phase 1 then Phase 2 for a clean linear walkthrough. Item 3 (observability counters) is written once but consumed by both phases (the `/metrics` endpoint in Phase 2 reads Phase 1's counters), so Phase 1 must land before Phase 2 item 7.

## Scope

### In scope (Phase 1 — mandatory)

- Item 1: `_CONSECUTIVE_GEN_FAILURES` counter + `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES` config field + third recycle trigger in `_browser_health_check_sync`.
- Item 2: swap `/readyz` and `/admin/status`'s `browser_running` computation to `engine.is_healthy()[0]`.
- Item 3: `_CHAT_REJECTED`, `_TASK_REJECTED`, `_RECYCLE_COUNT`/`_LAST_RECYCLE_REASON`/`_LAST_RECYCLE_AT` counters; a new `"observability"` block in `/admin/status`; a `log.warning`/`log.error` line in `_http_exc` for 401/429/5xx.
- Item 4: `test_chat_and_task_admission_are_independent`-style regression test(s) in `tests/test_chat_admission.py`.

### In scope (Phase 2 — mandatory)

- Item 5: `_rate_limit_headers()` helper; wired into both `_admit_task`'s and `_admit_chat`'s existing `HTTPException(429, ...)` raises via the `headers=` kwarg (already forwarded by the existing `_http_exc` handler — no change needed there beyond item 3's logging).
- Item 6: one new `<th>`/`<td>` column in `admin.html`'s account pool table displaying `use_count` (already returned by the backend — see [Phase 2 Item 6](#item-6-per-account-use_count-surfaced-in-admin) for the "no backend change needed" finding), plus the matching `I18N_EN` entry.

### In scope (Phase 2 — optional/stretch, implement only after items 1-6 are done and tested)

- Item 7: `GET /metrics`, key-gated like other admin routes, hand-written Prometheus text exposition (stdlib `str`/f-string formatting only — **no `prometheus_client` dependency**, per `requirements.txt`'s intentionally minimal policy).

### Out of scope / deferred

- Item 6's optional sub-item: a per-kind (image/video/chat) usage counter per account, which would require threading a new `kind` parameter through every `store.mark()` call site (`_run_generation` success/failure paths, `safe_chat_stream`'s success/failure paths, `_renew_and_persist`) — 6+ call sites for a nice-to-have breakdown that `use_count` (this plan's mandatory item) already approximates. **Explicitly deferred** — flagged here as a possible future small plan, not pursued now.
- Broader structured/JSON logging, a full audit trail, or an external metrics backend (Prometheus server, Grafana, etc.) — explicitly out of scope per the original research; `/metrics` (item 7) is a passive text endpoint an external scraper can poll, not a push integration.
- `X-RateLimit-Reset-Requests` (OpenAI's time-window-reset header) is deliberately **not** added in item 5 — see [Item 5](#item-5-openai-style-rate-limit-headers-on-429-responses) for why it has no natural meaning for a concurrency admission cap.

## Phase Completion Rules

A phase is NOT complete until:

1. **Integration Test** - Works with other system pieces (existing lock-order, admission, and `_browser_health_check_sync`/`GEN_LOCK` invariants from the prior `stability_performance` plan still hold — see [Blast Radius](#blast-radius)).
2. **Manual Test** - A human (or automated harness) can trigger the new behavior and observe the expected HTTP/log/admin-UI outcome.
3. **Data Verification** - For Phase 1: `app._CONSECUTIVE_GEN_FAILURES` resets to 0 on every successful generation and increments only on a plain `MuseGenerationError`; `/admin/status`'s `observability` block reflects live counter values. For Phase 2: a rejected request's response carries the new headers; `admin.html`'s pool table shows a non-zero `use_count` for an account that has generated at least once.
4. **Error Handling** - Failure cases (consecutive-failure threshold reached, 429 with headers, `/metrics` requested without a key when one is configured) behave as specified, not by accident.
5. **User Confirmation** - User (or the EXECUTE-phase agent closing this plan) confirms pytest is green and manually exercises at least one 429-with-headers response and one `/admin/status` observability-block read.

Status markers used below:
- ⏳ PLANNED - Not started
- 🔨 CODE DONE - Written but not E2E tested
- 🧪 TESTING - Currently being tested
- ✅ VERIFIED - Tested AND confirmed working (requires explicit user confirmation language in the closeout, not just "tests pass")
- 🚧 BLOCKED - Has issues

## Execution Brief

| Item | What happens | Test | Verify | Done when |
|---|---|---|---|---|
| 1. Consecutive-failure trigger | New `_note_generation_result()` helper + `_CONSECUTIVE_GEN_FAILURES` counter, called from `_run_generation`'s outer try/except (image/video) and `safe_chat_stream`'s consumer loop (chat/responses); new `browser_max_consecutive_failures` config field; third trigger in `_browser_health_check_sync` | Extend `tests/test_browser_health.py` | `app._CONSECUTIVE_GEN_FAILURES` increments on a plain `MuseGenerationError`, not on `MuseAuthError`/`MuseProxyError`/`MuseModelRefusalError`/`RefImageSafetyError`; resets to 0 on success | New tests pass; existing `tests/test_browser_health.py`, `tests/test_lock_order.py`, `tests/test_stream_backpressure.py`, `tests/test_concurrency.py` pass unmodified |
| 2. `/readyz`/`/admin/status` liveness swap | Replace `bool(engine.proc and engine.proc.poll() is None)` with `engine.is_healthy()[0]` at both call sites | No new test required (mechanical swap); existing `tests/test_account_proxy.py`, `tests/test_admin_base_url.py` still pass | Both endpoints' `browser_running` field reflects a real CDP ping, not just process liveness | No existing test asserts the old boolean's exact derivation; Dockerfile's `HEALTHCHECK` targets `/healthz` (untouched), confirmed via `grep -n HEALTHCHECK Dockerfile` |
| 3. Counters + `/admin/status` block + `_http_exc` logging | New module globals `_CHAT_REJECTED`, `_TASK_REJECTED`, `_RECYCLE_COUNT`/`_LAST_RECYCLE_REASON`/`_LAST_RECYCLE_AT`; new `"observability"` key in `admin_status()`'s dict; `log.warning`/`log.error` added to `_http_exc` | New `tests/test_observability.py` | `/admin/status` includes the new block with live values; `caplog` shows a log line for 429/401/5xx and none for 404 | New tests pass; no existing test touches `_http_exc` or asserts on `/admin/status`'s exact key set (confirmed via grep) |
| 4. Admission independence test | New test(s) proving `_admit_chat`/`_CHAT_ACTIVE` and `_admit_task`'s `store.tasks` scan never interfere | Extend `tests/test_chat_admission.py` | Admitting to the chat cap does not block `_admit_task`, and vice versa | New tests pass; existing `tests/test_admission.py`, `tests/test_chat_admission.py` tests pass unmodified |
| 5. Rate-limit headers | New `_rate_limit_headers(limit, active)` helper; wired into `_admit_task`'s and `_admit_chat`'s existing 429 raises via `headers=` | Extend `tests/test_admission.py`, `tests/test_chat_admission.py` | A 429 `HTTPException`'s `.headers` contains `Retry-After`, `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests` | New tests pass; existing `.detail`-only assertions in both files pass unmodified (message text unchanged) |
| 6. `use_count` in admin UI | One new `<th>`/`<td>` pair in `admin.html`'s pool table + one `I18N_EN` entry; **no backend change** (`use_count` already flows through `Store.public_account`/`list_accounts`) | Manual (real-Chromium `tests/test_admin_layout.py` script, run manually, not part of automated `pytest -q`) | Pool table shows a "调用次数"/"Use count" column with a real integer | Column renders in both `zh` and `en` modes; `colspan` on the three empty-state rows bumped from 10 to 11 |
| 7. (Optional) `/metrics` | New `GET /metrics` route, gated by the existing `auth` dependency, hand-written Prometheus text exposition from items 1/3's counters + `store.stats()` | New tests in `tests/test_observability.py` | `curl -H "Authorization: Bearer <key>" /metrics` returns `text/plain; version=0.0.4` body with `# HELP`/`# TYPE` lines and current counter values | New tests pass; no new dependency added to `requirements.txt` |

**Expected Outcome**:
- A single wedged page/renderer that `is_healthy()`'s process-level ping cannot see is now detected after `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES` (default 5) consecutive plain generation failures, and proactively recycled on the next keepalive tick (subject to the same `GEN_LOCK`-gated deferral the existing crash/uptime triggers already use).
- `/readyz` and `/admin/status` report the same liveness definition the prior plan's `is_healthy()` already introduced, instead of a stale process-only check.
- An operator can see admission-rejection counts, the last recycle reason/time, and a running consecutive-failure streak from `/admin/status` (and, optionally, `/metrics`), and sees a log line for every 401/429/5xx response instead of silence.
- OpenAI-SDK clients that already implement 429 backoff using `Retry-After`/`X-RateLimit-*` now get real values from this service instead of a bare error body.
- The admin UI shows per-account usage counts alongside the already-existing "last used" column.
- All existing tests continue to pass unmodified except where this plan explicitly calls for an addition (no existing test file needs an edit to its existing test bodies — only new test functions are added to existing files, plus one new file).

## Touchpoints

Exact files and functions this plan will touch, with line numbers as read at plan time (08-11-26) — **re-verify before editing in case of drift**.

### Phase 1

- `config.py`
  - Insert a new `browser_max_consecutive_failures` field in `class Config`, placed immediately after `browser_max_uptime_hours` (currently lines 183-184), before the `data_dir` property (line 186).
- `app.py`
  - Insert `_CONSECUTIVE_GEN_FAILURES = 0`, `_GEN_FAILURE_LOCK = threading.Lock()`, and `_note_generation_result()` right after `_pos()` ends (currently line 1406), before `def _run_generation(...)` (line 1409).
  - `_run_generation()` (currently lines 1409-1424): wrap its single `return _run_generation_locked(...)` statement in a `try/except Exception` that calls `_note_generation_result()` on both paths.
  - `safe_chat_stream()`'s consumer loop (currently lines 645-655, inside the function that starts at line 494): add `_note_generation_result(val)` before `raise val` in the `elif kind == "error":` branch, and `_note_generation_result(None)` before `break` in the `else:` branch.
  - `_browser_health_check_sync()` (currently lines 3178-3201): add a second `if reason is None and CFG.browser_max_consecutive_failures > 0:` check between the existing `is_healthy()` check and the existing `browser_max_uptime_hours` check; track a `consecutive_trigger` bool; reset the counter via `_note_generation_result(None)` after a successful recycle caused by this specific trigger.
  - `/readyz` (currently lines 1541-1546): swap `bool(engine.proc and engine.proc.poll() is None)` → `engine.is_healthy()[0]`.
  - `/admin/status` / `admin_status()` (currently lines 2457-2477): swap the same expression (currently line 2466); add a new `"observability"` key to the returned dict (after `"corrupt_files": list(store.corrupt_files),` at line 2471, before `"config": {...}` at line 2472).
  - Insert `_CHAT_REJECTED = 0` right after `_CHAT_ACTIVE = 0` (currently line 225); insert `_TASK_REJECTED: dict[str, int] = {"image": 0, "video": 0}` immediately after it.
  - Insert `_RECYCLE_COUNT = 0`, `_LAST_RECYCLE_REASON: str | None = None`, `_LAST_RECYCLE_AT: float = 0.0` right before `_browser_health_check_sync()`'s def (currently line 3178).
  - `_admit_task()` (currently lines 1618-1629): increment `_TASK_REJECTED[kind]` right before the existing `raise HTTPException(429, ...)`.
  - `_admit_chat()` (currently lines 1632-1647): increment `_CHAT_REJECTED` right before its existing `raise HTTPException(429, ...)`.
  - `_http_exc()` (currently lines 286-297): add a `log.warning`/`log.error` call at the top of the function, before the existing `if request.url.path.startswith("/v1/"):` branch.
- `tests/test_browser_health.py` (extend existing file).
- `tests/test_chat_admission.py` (extend existing file; add `from types import SimpleNamespace` to its imports).
- `tests/test_observability.py` (new file).

### Phase 2

- `app.py`
  - Insert `_RATE_LIMIT_RETRY_AFTER_SECONDS = 5` and a new `_rate_limit_headers(limit: int, active: int) -> dict` helper right before `_admit_task()`'s def (currently line 1618).
  - `_admit_task()` (currently lines 1618-1629): add `headers=_rate_limit_headers(limit, active)` to the existing `raise HTTPException(429, ...)` call.
  - `_admit_chat()` (currently lines 1632-1647): add `headers=_rate_limit_headers(CFG.chat_queue_max, _CHAT_ACTIVE)` to the existing `raise HTTPException(429, ...)` call.
  - New route `GET /metrics` (optional/stretch, item 7), inserted after `admin_status()` ends (currently line 2477 plus its trailing blank lines), before the `# ------------------------- Admin: account pool -------------------------` comment (currently line 2480).
- `admin.html`
  - Header row (currently lines 371-374): add `<th>调用次数</th>` after the existing `<th>最后使用</th>`, before `<th>备注</th>`.
  - Row template (currently inside the `accounts.map(a => {...})` block, around lines 1839-1867): add `<td class="mono">${a.use_count ?? 0}</td>` after the existing `last_used` `<td>` (around line 1854), before the `note` `<td>` (around line 1855).
  - `I18N_EN` dictionary (currently lines 987-995): add `"调用次数": "Use count",` after `"最后使用": "Last used",` (line 993), before `"备注": "Notes",` (line 994).
  - Bump `colspan="10"` → `colspan="11"` at all three/four `#poolBody` empty/loading-state rows (currently lines 375, 1836, 1870, 1871 — re-grep `colspan="10"` at edit time to confirm the exact count and locations, since the loading-state row at line 375 is in the static markup, not the JS template literals at 1836/1870/1871).
- `README.md`: add a new row to the environment-variable table, mirroring the existing `MUSE2API_BROWSER_MAX_UPTIME_HOURS` row (currently line 468).
- `.env.example`: add `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES=5` mirroring the existing `MUSE2API_BROWSER_MAX_UPTIME_HOURS=0` line (currently line 74).
- `tests/test_admission.py` (extend existing file).
- `tests/test_chat_admission.py` (extend existing file, same edit session as Phase 1 item 4).
- `tests/test_observability.py` (extend the Phase 1 file with item 7's tests, if item 7 is implemented).

## Public Contracts

Everything new introduced by this plan, and the compatibility constraints each one must satisfy:

- **New env var `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES`** (config.py `browser_max_consecutive_failures`, default `5`). Follows the exact `max(0, int(_env(...)))` pattern already used by `browser_max_uptime_hours`. `0` disables this specific trigger (crash/CDP-unresponsive detection and, if configured, uptime-based recycling remain active regardless — same "0 disables only this one trigger" contract as `browser_max_uptime_hours`).
- **New `MuseEngine`-adjacent module state in `app.py`** (`_CONSECUTIVE_GEN_FAILURES`, `_CHAT_REJECTED`, `_TASK_REJECTED`, `_RECYCLE_COUNT`, `_LAST_RECYCLE_REASON`, `_LAST_RECYCLE_AT`): internal process-local counters, reset to their initial values on every process restart (not persisted to disk — this is acceptable for this plan's purpose: operational visibility into the *current* process's recent behavior, not a durable audit trail, which is explicitly out of scope).
- **New `"observability"` key in `/admin/status`'s response**: purely additive — existing keys (`accounts`, `stats`, `tasks`, `media_count`, `browser_running`, `essential_cookies`, `base_url`, `corrupt_files`, `config`) are unchanged in name, type, and meaning (only `browser_running`'s *computation* changes per item 2, not its type — still a `bool`).
- **New `Retry-After`/`X-RateLimit-Limit-Requests`/`X-RateLimit-Remaining-Requests` headers on every existing 429** from `_admit_task` (image/video) and `_admit_chat` (chat/responses): additive headers on an existing response shape; the JSON body (`{"error": {"message": ..., "type": "rate_limit_error", ...}}` via the existing `_http_exc` handler) is completely unchanged. Any client that ignores unknown headers (the overwhelming majority) sees no behavior change at all.
- **New `use_count` column in `admin.html`**: purely additive UI; no existing column, button, or JS function signature changes. The underlying data (`account["use_count"]`) already existed server-side (set by `store.py`'s `_record_use()`) and already flowed through `Store.public_account()`/`list_accounts()`/`/admin/accounts`/`/admin/status` **before this plan** — this plan only adds the missing display.
- **New `GET /metrics` route (optional/stretch)**: gated by the existing `auth` dependency (same API key as every other inference/admin endpoint except `/healthz`/`/readyz`). Returns `text/plain; version=0.0.4` (Prometheus exposition format), built with stdlib string formatting only — **no new entry in `requirements.txt`**.
- **No change** to any existing public contract from the prior plan or earlier: `_admit_task`'s/`_admit_chat`'s message text and `.detail`, `ACCOUNT_POOL`-before-`GEN_LOCK` lock ordering, `TASK_ADMISSION_LOCK`'s existing use, `is_healthy()`/`recycle()`/`uptime_seconds`'s existing signatures, `_browser_health_check_sync()`'s existing return shape for the two pre-existing triggers, `/healthz`'s existing shape, or the Docker `HEALTHCHECK` target (`/healthz`, confirmed unaffected by item 2's `/readyz` change).

## Blast Radius

What existing behavior/tests could be affected, and why they are expected to stay green:

- **`tests/test_browser_health.py`** (all 4 existing `app.py`-level wiring tests: `test_health_check_noop_when_healthy_and_uptime_disabled`, `test_health_check_recycles_when_gen_lock_free`, `test_health_check_defers_when_gen_lock_held`, `test_max_uptime_trigger`) — none of them touch `app.CFG.browser_max_consecutive_failures` or `app._CONSECUTIVE_GEN_FAILURES`. The new field defaults to `5`; the module-level counter starts at `0`; `0 >= 5` is always `False` in every existing test's default state, so the new `if reason is None and CFG.browser_max_consecutive_failures > 0: if _CONSECUTIVE_GEN_FAILURES >= CFG.browser_max_consecutive_failures:` branch never fires for these 4 tests. **Expected: unaffected, no edits needed** (only new tests are appended).
- **`tests/test_lock_order.py`** — `test_generation_releases_both_locks_on_failure` calls `app._run_generation("a lighthouse", "image", 5)` with a stubbed `engine.generate` that raises `app.MuseGenerationError("boom")`, and asserts `pytest.raises(app.MuseGenerationError)`. This plan wraps `_run_generation`'s body in `try: ... except Exception as exc: _note_generation_result(exc); raise`; a bare `raise` inside `except` re-raises the exact same exception object (same type, same traceback), so the test's `pytest.raises(app.MuseGenerationError)` still matches. `_note_generation_result()` only touches a brand-new, independent `_GEN_FAILURE_LOCK` — it never touches `ACCOUNT_POOL`/`GEN_LOCK`, so this test's existing lock-order assertions (`pool.violations == []`, `not app.GEN_LOCK.locked()`) are unaffected. **Expected: unaffected, no edits needed.**
- **`tests/test_stream_backpressure.py`** — `test_disconnect_releases_the_browser_slot` closes the stream mid-iteration, injecting `GeneratorExit` at the suspended `yield val` inside `safe_chat_stream`'s consumer loop. `GeneratorExit` is a `BaseException`, not caught by the new `elif kind == "error":`/`else:` branches (which only run when `q.get()` actually returns an `"error"` or sentinel item) — the generator exits via the exception before either new line executes. `test_complete_stream_still_terminates` drains the stream fully and hits the `else: break` path, where `_note_generation_result(None)` now fires — this only resets a counter the test never inspects. **Expected: unaffected, no edits needed.**
- **`tests/test_concurrency.py`** — exercises `_run_generation` via a simulated multi-account scenario (line 136) and separately catches `app.MuseGenerationError` (line 77); does not assert on `app._CONSECUTIVE_GEN_FAILURES` or on `store.mark` call counts. **Expected: unaffected.**
- **`tests/test_responses_api.py`** — calls `app.responses_api(req, None)` directly with `app.safe_chat_stream` **monkeypatched** (replaced wholesale), so the new code inside the real `safe_chat_stream` (item 1's consumer-loop hook) never executes in this file's tests. **Expected: unaffected, no edits needed.**
- **`tests/test_admission.py`** — `test_admission_counts_queued_and_processing` asserts `"Video queue is full" in str(exc.value.detail)`; item 5 adds a `headers=` kwarg to the same `raise HTTPException(429, ..., headers=...)` call without changing the message string or `.detail`. Item 3 adds a `_TASK_REJECTED[kind] += 1` line one statement before the same raise — a plain dict mutation, no side effect on `.detail`/`.status_code`. `test_both_endpoints_go_through_the_shared_helper` asserts `source.count("_admit_task(") >= 2` and specific substrings inside `create_video`'s block — neither item adds or removes an `_admit_task(` call site, so this count-based assertion is unaffected. **Expected: unaffected, no edits needed** (only new tests are appended).
- **`tests/test_chat_admission.py`** — same reasoning as `test_admission.py` for the `_admit_chat` 429 message/`.detail`; the existing `reset_chat_active` autouse fixture already resets `app._CHAT_ACTIVE = 0` before/after every test in this file, so a new `_CHAT_REJECTED` global does not need to be reset by that fixture (nothing in this plan requires `_CHAT_REJECTED` to be exactly 0 at test start — the new tests that check it will read its value immediately before and after the specific call under test, not rely on a global baseline of 0). **Expected: unaffected, no edits needed** (only new tests are appended; `SimpleNamespace` import is added for item 4's independence test).
- **`tests/test_account_proxy.py`** (`AccountProxyTests.test_validation_and_secret_preservation`) — hits `/admin/status` via a real ASGI request and asserts only `status == 200` and no secret leakage in the JSON body. `app.engine` in this test environment has `proc=None`/`browser=None` (no real Chromium started), so `engine.is_healthy()` short-circuits to `(False, "not started")` immediately (no network call — see `is_healthy()`'s own `if not self.proc or not self.browser: return False, "not started"` guard), so item 2's swap does not risk this test hanging or timing out. **Expected: unaffected, no edits needed.**
- **`tests/test_admin_base_url.py`** — `if __name__ == "__main__":`-gated script (no pytest-collected test functions); asserts `/admin/status` returns 200 and that `base_url`-derived fields agree with `/admin/apikey`. Does not touch `browser_running` or the new `observability` key. **Expected: unaffected.**
- **`tests/test_admin_layout.py`** — explicitly excluded from pytest collection via `tests/conftest.py`'s `collect_ignore` (real-Chromium manual script). Item 6 adds a column; the script's own assertions check row count (`#poolBody tr`), button count per row (`buttons==8`, unaffected by an added column), and the Notes column's `display`/`white-space` via the `.ellip` class selector (position-independent). **Expected: unaffected when manually re-run**, but it is manual/non-automated either way, so it is not a pytest gate for this plan.
- **`tests/test_admin_i18n.py`** — only asserts that every `data-i18n`-tagged element has a matching `I18N_EN_HTML` entry, that no `I18N_EN_HTML` entry is orphaned, and that `I18N_EN`/`I18N_EN_PATTERNS` have no duplicate keys / contain no stray Chinese. The new `<th>调用次数</th>` is **not** tagged with `data-i18n` (matching 7 of the existing 8 headers in that same table, which are translated via the whole-sentence `I18N_EN` dictionary and `translateTree`'s generic text-node walk, not via `data-i18n`), so `test_every_data_i18n_key_has_an_english_block`/`test_no_orphan_english_blocks` are unaffected. The new `"调用次数": "Use count",` entry is added to `I18N_EN` (not `I18N_EN_HTML`), so it is also outside those two tests' scope; `test_whole_sentence_dictionary_has_no_duplicate_keys` only fails on an actual duplicate key, and `"调用次数"` is a new, unique key. **Expected: unaffected, no edits needed.**
- **`tests/test_config_defaults.py`** — only asserts on `MUSE2API_CONCURRENCY`/`max_concurrency`; does not touch the new `browser_max_consecutive_failures` field. **Expected: unaffected.**
- **Dockerfile / docker-compose.yml** — not touched. Confirmed via `grep -n HEALTHCHECK Dockerfile`: the container's `HEALTHCHECK` targets `/healthz` (unaffected by item 2's `/readyz` change), not `/readyz`.
- **`requirements.txt`** — not touched. Item 7's `/metrics` endpoint uses only stdlib string formatting (f-strings, `"\n".join(...)`) and the already-imported `Response` class from `fastapi.responses` — no `prometheus_client` or any other new dependency.

## Phase 1: Round-2 Stability Follow-ups

### Item 1: Consecutive-failure "wedged page" recycle trigger

#### Design decisions

- **Single funnel points, not scattered call sites.** Both `_run_generation_locked` (image/video's actual retry loop) and `safe_chat_stream`'s internal `worker()` thread have multiple internal raise/return points, and `_run_generation_locked` even has one `raise MuseGenerationError(...)` that bypasses its own trailing `except`/`raise last_exc` block entirely (the "Overall task deadline reached" check at the top of its `for attempt in range(2):` loop, lines ~1451-1452). Hooking inside either of those functions at multiple points would be invasive and risks missing an edge case. Instead, this plan hooks at the **one place each already has that every caller routes through unconditionally**:
  - For image/video: `_run_generation()` (lines 1409-1424) is the **only** caller of `_run_generation_locked()` (confirmed via `grep -n "_run_generation_locked\|_run_generation(" app.py` — exactly one call site at line 1422), and `_run_generation()` itself is the only thing `_queue_image`'s worker, `images_generations`, `images_edits`, and `create_video`'s worker ever call for the actual generation. Wrapping `_run_generation`'s body in one `try/except Exception` catches every possible failure path inside `_run_generation_locked`, including the early-raise deadline shortcut.
  - For chat/responses: `safe_chat_stream()`'s **consumer-side** loop (lines 645-655, in the main thread, not the worker thread) is the one place every terminal outcome funnels through: the worker thread's `finally:` block always calls `_emit_done()` (success sentinel) after queuing either nothing more (success) or an `("error", last_exc)` item (failure) via `_emit(("error", last_exc))` at line 635. The consumer's `while True:` loop either `raise`s the error (failure) or `break`s on the `("done", None)` sentinel (success) — exactly one of those two outcomes happens exactly once per call, for every endpoint that uses `safe_chat_stream` (`chat_completions` streaming/non-streaming, `responses_api` streaming/non-streaming), without touching those four call sites at all.
  - A client disconnect (`GeneratorExit`, injected when the downstream `StreamingResponse` or test calls `.close()`/`.aclose()` on the generator) is a `BaseException`, not caught by either new line — it propagates straight through the `while True:` loop without reaching the `elif`/`else` branches. This is **intentional**: a disconnect is not a signal that the browser/page is wedged, so it must not count as a failure (and does not, by construction, not by an extra check).
- **Which exceptions count as a "failure" for this specific trigger.** Only a plain `MuseGenerationError` (not one of its three subclasses) increments the counter:
  - `MuseAuthError` (a separate `RuntimeError` subclass, not related to `MuseGenerationError`) — an account/session problem, not a wedged page. The browser itself may be perfectly healthy.
  - `MuseProxyError(MuseGenerationError)` — a proxy/network problem specific to one account's route, not an engine problem.
  - `MuseModelRefusalError(MuseGenerationError)` — the assistant/model declined to answer; the page and browser worked correctly. Counting refusals would false-trigger recycles under ordinary content-policy traffic.
  - `RefImageSafetyError(MuseGenerationError)` — an input-validation rejection, not an engine problem (and in practice raised before `_run_generation` is even called, during `_prepare_reference_images`).
  - Any other `MuseGenerationError` instance — including every "Timed out waiting for ..." / "Overall task deadline reached" / "Chromium failed to start" / generic `"Generation failed: {exc}"` wrapper raised inside `engine.py`/`app.py`'s generation paths — **does** count. This is exactly the class of error a wedged renderer/page produces.
- **Reset on success, increment on a counted failure, no-op otherwise.** `_note_generation_result(exc: Exception | None)`: `exc is None` → reset to `0`; `exc` is a plain `MuseGenerationError` per the exclusion list above → `+= 1`; anything else (including `MuseAuthError`, or any exception from an unrelated internal error) → **no-op**, counter unchanged. This means an account-auth failure neither resets nor advances the streak — it is simply orthogonal to this specific signal.
- **Known limitation, accepted by design**: this is a *consecutive-streak* counter, not a flaky-rate detector. A pattern like 4 failures, 1 success, 4 failures, 1 success never reaches a threshold of 5, even though something is clearly unhealthy. This matches the literal request ("N consecutive ... timeouts") and keeps the implementation to a single integer with no time-window bookkeeping. If a rate-based detector is ever wanted, it is a separate, larger feature (sliding window, decay, etc.) — not implemented here.
- **Lock**: a brand-new `_GEN_FAILURE_LOCK = threading.Lock()`, independent of `TASK_ADMISSION_LOCK`/`GEN_LOCK`/`ACCOUNT_POOL`. It is only ever held for the duration of one integer read-or-write — never combined with any other lock, so it cannot introduce a new lock-ordering hazard.
- **Reset after the recycle it causes.** When `_browser_health_check_sync()` recycles the browser specifically because this new trigger fired, it calls `_note_generation_result(None)` right after `engine.recycle(reason)` succeeds, so the fresh browser is not immediately re-flagged as unhealthy by stale failure history before any new generation has had a chance to prove the fresh browser is fine.

#### `config.py` changes

Add, immediately after the existing `browser_max_uptime_hours` field (after line 184), before the `data_dir` property:

```python
    # Detects a "wedged page" that is_healthy() cannot see on its own: is_healthy()
    # only pings the browser-process-level /json/version endpoint, so a single hung
    # renderer/page behind an otherwise-healthy browser process is invisible to it.
    # This counts consecutive generation failures across every image/video/chat
    # request (reset to 0 on any success) and adds a third recycle trigger once the
    # streak reaches this threshold. 0 disables only this trigger -- crash/CDP-
    # unresponsive detection and, if configured, uptime-based recycling still run.
    browser_max_consecutive_failures: int = field(
        default_factory=lambda: max(0, int(_env("MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES", "5"))))
```

#### `app.py` changes

1. Right after `_pos()` ends (after line 1406), before `def _run_generation(...)` (line 1409), add:

```python
_CONSECUTIVE_GEN_FAILURES = 0
_GEN_FAILURE_LOCK = threading.Lock()


def _note_generation_result(exc: Exception | None) -> None:
    """Track consecutive wedged-page-style failures across every image/video/chat
    request, independent of which account was used.

    is_healthy() only pings the browser-process-level /json/version endpoint, so a
    single hung renderer/page behind an otherwise-healthy browser process is invisible
    to it. This counter fills that gap and feeds _browser_health_check_sync's third
    recycle trigger. Only a plain MuseGenerationError (timeout, deadline-exceeded,
    browser-queue-timeout, etc.) counts as a failure signal:
      - MuseAuthError / MuseProxyError: an account or network problem, not a wedged
        page -- the browser itself may be perfectly healthy.
      - MuseModelRefusalError / RefImageSafetyError: an expected content/input
        outcome, not an engine problem.
    Any success (exc=None) resets the streak to 0. Called from exactly two places --
    _run_generation's outer try/except (image/video) and safe_chat_stream's consumer
    loop (chat/responses) -- the single funnel every caller of each already routes
    through, so no other call site ever needs to call this directly.
    """
    global _CONSECUTIVE_GEN_FAILURES
    with _GEN_FAILURE_LOCK:
        if exc is None:
            _CONSECUTIVE_GEN_FAILURES = 0
        elif isinstance(exc, MuseGenerationError) and not isinstance(
                exc, (MuseProxyError, MuseModelRefusalError, RefImageSafetyError)):
            _CONSECUTIVE_GEN_FAILURES += 1
```

2. In `_run_generation()` (currently lines 1409-1424), change the body from:

```python
    deadline = time.monotonic() + max(1, timeout)
    return _run_generation_locked(prompt, kind, timeout, account_id,
                                  on_progress, reference_image, reference_images,
                                  deadline=deadline, on_acquired=on_acquired)
```

to:

```python
    deadline = time.monotonic() + max(1, timeout)
    try:
        result = _run_generation_locked(prompt, kind, timeout, account_id,
                                        on_progress, reference_image, reference_images,
                                        deadline=deadline, on_acquired=on_acquired)
    except Exception as exc:
        _note_generation_result(exc)
        raise
    _note_generation_result(None)
    return result
```

3. In `safe_chat_stream()`'s consumer loop (currently lines 645-655), change:

```python
    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()
```

to:

```python
    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                _note_generation_result(val)
                raise val
            else:
                _note_generation_result(None)
                break
    finally:
        stop_event.set()
```

4. In `_browser_health_check_sync()` (currently lines 3178-3201), change:

```python
def _browser_health_check_sync() -> dict:
    """..."""
    healthy, detail = engine.is_healthy()
    reason = None if healthy else f"unhealthy: {detail}"
    if reason is None and CFG.browser_max_uptime_hours > 0:
        if engine.uptime_seconds > CFG.browser_max_uptime_hours * 3600:
            reason = f"max uptime exceeded ({CFG.browser_max_uptime_hours}h)"
    if reason is None:
        return {"recycled": False}
    if not GEN_LOCK.acquire(blocking=False):
        log.info("[Browser health] %s, but a generation is in-flight; deferring to next cycle", reason)
        return {"recycled": False, "deferred": True, "reason": reason}
    try:
        engine.recycle(reason)
        return {"recycled": True, "reason": reason}
    finally:
        GEN_LOCK.release()
```

to (new lines marked `# NEW`):

```python
def _browser_health_check_sync() -> dict:
    """..."""
    global _RECYCLE_COUNT, _LAST_RECYCLE_REASON, _LAST_RECYCLE_AT  # NEW (also needed by item 3)
    healthy, detail = engine.is_healthy()
    reason = None if healthy else f"unhealthy: {detail}"
    consecutive_trigger = False  # NEW
    if reason is None and CFG.browser_max_consecutive_failures > 0:  # NEW
        if _CONSECUTIVE_GEN_FAILURES >= CFG.browser_max_consecutive_failures:  # NEW
            reason = f"{_CONSECUTIVE_GEN_FAILURES} consecutive generation failures"  # NEW
            consecutive_trigger = True  # NEW
    if reason is None and CFG.browser_max_uptime_hours > 0:
        if engine.uptime_seconds > CFG.browser_max_uptime_hours * 3600:
            reason = f"max uptime exceeded ({CFG.browser_max_uptime_hours}h)"
    if reason is None:
        return {"recycled": False}
    if not GEN_LOCK.acquire(blocking=False):
        log.info("[Browser health] %s, but a generation is in-flight; deferring to next cycle", reason)
        return {"recycled": False, "deferred": True, "reason": reason}
    try:
        engine.recycle(reason)
        _RECYCLE_COUNT += 1  # NEW (item 3)
        _LAST_RECYCLE_REASON = reason  # NEW (item 3)
        _LAST_RECYCLE_AT = time.time()  # NEW (item 3)
        if consecutive_trigger:  # NEW
            _note_generation_result(None)  # NEW -- fresh browser; stale failure history must not re-trigger immediately
        return {"recycled": True, "reason": reason}
    finally:
        GEN_LOCK.release()
```

(The `_RECYCLE_COUNT`/`_LAST_RECYCLE_REASON`/`_LAST_RECYCLE_AT` globals and their `global` declaration are introduced together with this same edit, even though they are conceptually item 3 — splitting this one function's edit across two separate passes would be more error-prone than doing it once. See item 3's globals below.)

#### New/extended tests — `tests/test_browser_health.py`

Mirror the existing `StubEngine`/`gen_lock_released` fixtures already in this file. Required additions:

1. **`_note_generation_result` unit tests** (new, no engine/app stub needed beyond direct calls):
   - `test_note_generation_result_resets_on_success` — set `app._CONSECUTIVE_GEN_FAILURES` to some positive value, call `app._note_generation_result(None)`, assert it is `0`.
   - `test_note_generation_result_increments_on_plain_generation_error` — reset to `0`, call `app._note_generation_result(app.MuseGenerationError("timeout"))` twice, assert it is `2`.
   - `test_note_generation_result_ignores_auth_error` — reset to `0`, call `app._note_generation_result(app.MuseAuthError("bad session"))`, assert it stays `0`.
   - `test_note_generation_result_ignores_proxy_and_refusal_and_ref_image_errors` — parametrize over `app.MuseProxyError`, `app.MuseModelRefusalError`, `app.RefImageSafetyError`; reset to `0` before each; assert the counter stays `0`.
   - Add a small autouse fixture in this file (or extend the existing `gen_lock_released` one) that resets `app._CONSECUTIVE_GEN_FAILURES = 0` before and after every test in this file, mirroring `test_chat_admission.py`'s `reset_chat_active` pattern — needed because `tests/test_lock_order.py`'s `test_generation_releases_both_locks_on_failure` also exercises `_run_generation` and will increment this same module-level counter if that file happens to run earlier in the same pytest session.

2. **`_browser_health_check_sync` third-trigger tests** (extend the existing `StubEngine`-based section):
   - `test_consecutive_failure_trigger_recycles` — `app._CONSECUTIVE_GEN_FAILURES = 5` (or `monkeypatch.setattr`), `app.CFG.browser_max_consecutive_failures = 5`, `StubEngine(healthy=(True, "ok"))`, `GEN_LOCK` unlocked; call `app._browser_health_check_sync()`; assert `result["recycled"] is True`, `"consecutive generation failures" in result["reason"]`, and `app._CONSECUTIVE_GEN_FAILURES == 0` afterward (reset by the new `consecutive_trigger` branch).
   - `test_consecutive_failure_trigger_disabled_by_default_zero` — `app.CFG.browser_max_consecutive_failures = 0`, `app._CONSECUTIVE_GEN_FAILURES = 999`, healthy stub, uptime disabled; call `app._browser_health_check_sync()`; assert `{"recycled": False}`.
   - `test_consecutive_failure_trigger_below_threshold_is_a_noop` — `app.CFG.browser_max_consecutive_failures = 5`, `app._CONSECUTIVE_GEN_FAILURES = 4`; assert `{"recycled": False}`.
   - `test_is_healthy_false_takes_priority_over_consecutive_failures` — unhealthy stub (`(False, "boom")`) *and* `_CONSECUTIVE_GEN_FAILURES` over threshold; assert the recycle reason is `"unhealthy: boom"` (the first, pre-existing check), not the consecutive-failure message — proving the new check is additive, not a replacement, and does not change existing-trigger precedence.

3. **Recycle-counter bookkeeping tests** (item 3, same function):
   - `test_recycle_updates_counters` — healthy=False stub, `GEN_LOCK` unlocked; record `app._RECYCLE_COUNT` before; call `app._browser_health_check_sync()`; assert `app._RECYCLE_COUNT` incremented by exactly 1, `app._LAST_RECYCLE_REASON == "unhealthy: boom"`, and `app._LAST_RECYCLE_AT` is a recent timestamp (`abs(time.time() - app._LAST_RECYCLE_AT) < 5`).
   - `test_deferred_recycle_does_not_touch_counters` — GEN_LOCK held (mirrors the existing `test_health_check_defers_when_gen_lock_held`); assert `app._RECYCLE_COUNT` and `app._LAST_RECYCLE_REASON` are unchanged from before the call.

4. **End-to-end wiring tests using real `_run_generation`/`safe_chat_stream`** (not just the unit-level `_note_generation_result` calls), to prove the hook points in item 1's two edited functions actually fire:
   - `test_run_generation_failure_increments_the_streak` — mirror `tests/test_lock_order.py`'s `FakeEngine`/`RecordingPool` pattern locally (a small local fixture, not a cross-file import): monkeypatch `app.engine`/`app.ACCOUNT_POOL` so `engine.generate` raises `app.MuseGenerationError("boom")`; reset `app._CONSECUTIVE_GEN_FAILURES = 0`; call `app._run_generation(...)` inside `pytest.raises(app.MuseGenerationError)`; assert the counter is now `1`.
   - `test_run_generation_success_resets_the_streak` — same fixture, `engine.generate` returns normally; set `app._CONSECUTIVE_GEN_FAILURES = 3` beforehand; call `app._run_generation(...)`; assert the counter is `0` afterward.
   - `test_safe_chat_stream_failure_increments_the_streak` — mirror `tests/test_stream_backpressure.py`'s `FakeEngine`/`StubPool` pattern locally, with `chat_stream` raising `app.MuseGenerationError("timeout")` instead of yielding; reset the counter to `0`; drain `app.safe_chat_stream(...)` inside `pytest.raises(app.MuseGenerationError)`; assert the counter is `1`.
   - `test_safe_chat_stream_success_resets_the_streak` — same fixture, `chat_stream` yields normally and completes; set the counter to `2` beforehand; drain the generator fully; assert it is `0` afterward.

### Item 2: `/readyz` and `/admin/status` liveness swap

#### Design decisions

- `is_healthy()` (engine.py lines 413-432) is a strictly more thorough liveness check than the old `bool(engine.proc and engine.proc.poll() is None)`: it additionally pings the CDP `/json/version` endpoint (3s timeout) and returns `False` with a reason string if the process is running but CDP is unresponsive. The old check could never catch that case.
- **Confirmed non-breaking for container orchestration**: `Dockerfile`'s `HEALTHCHECK` (line 55-56) targets `http://127.0.0.1:18610/healthz`, not `/readyz`. `/healthz` (app.py lines 1536-1538) is untouched by this plan — it remains a trivial `{"status": "ok", "time": ...}` with no browser check at all. So this swap cannot cause the container to be restarted more often than before.
- **Minor, accepted cost**: `/readyz` now makes one extra local HTTP call (to `127.0.0.1:{cdp_port}/json/version`, 3s timeout) on every poll, instead of a cheap `Popen.poll()` syscall. For an external orchestrator polling `/readyz` frequently (e.g., a Kubernetes readiness probe, if ever added), this is a small added cost per poll, not a correctness problem — `is_healthy()` already uses the same pattern for the existing keepalive-loop health check, so this is not a new code path, just a new caller of an existing one.
- `_admit_task()`/`store.stats()` elsewhere in `/admin/status` are untouched; only the `browser_running` line's computation changes.

#### `app.py` changes

1. `/readyz` (currently lines 1541-1546), change:

```python
@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}
```

to:

```python
@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": engine.is_healthy()[0]}
```

2. `admin_status()` (currently line 2466), change the same expression inside the returned dict:

```python
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
```

to:

```python
        "browser_running": engine.is_healthy()[0],
```

#### Test Procedure (manual)

1. Start the server; confirm `/readyz` and `/admin/status` both report `browser_running: true` once the browser has warmed up.
2. Kill the real Chromium process (`kill -9 <pid>`) without restarting the server; immediately poll `/readyz` — it should now report `browser_running: false` (previously it would have taken until the next request's fast-path check, or shown `true` if CDP was merely hung rather than the process actually dead).
3. Confirm `/healthz` is unaffected (`{"status": "ok", ...}` regardless of browser state) and the Docker `HEALTHCHECK` (if running in a container) does not restart the container from this alone.

### Item 3: Observability counters + `/admin/status` block + `_http_exc` logging

#### Design decisions

- **Reuse existing locks/patterns where one is already held at the exact right place**; add no new lock where none is needed:
  - `_CHAT_REJECTED`/`_TASK_REJECTED` are incremented **inside** `_admit_chat()`/`_admit_task()`, both of which already execute under `TASK_ADMISSION_LOCK` (for `_admit_chat`, the `with TASK_ADMISSION_LOCK:` block; for `_admit_task`, its caller already holds `TASK_ADMISSION_LOCK` per the existing `_queue_image`/`create_video` call sites) — no new lock needed.
  - `_RECYCLE_COUNT`/`_LAST_RECYCLE_REASON`/`_LAST_RECYCLE_AT` are written only from `_browser_health_check_sync()`, which the existing `_keepalive_loop`'s single `while True:` already serializes (only one call in flight at a time, same precedent as the existing unlocked `KEEPALIVE_STATE["running"] = False`-style writes elsewhere in this module) — no new lock needed.
- **Per-kind task-rejection counter, not one combined counter.** `_admit_task(kind, limit)` already receives `kind`; tracking `{"image": N, "video": M}` costs the same one extra line as a combined counter would, and is strictly more useful for an operator trying to tell which queue is actually under pressure.
- **`/admin/status`'s new block is read-only, process-local state** — no new file I/O, no new background thread, reusing the existing response dict's shape (purely additive key).
- **`_http_exc` logging escalation**: 401 and 429 are logged at `WARNING` (client-correctable: bad/missing key, or back off and retry); any `>= 500` (including the existing 502 from `MuseGenerationError`) is logged at `ERROR` (server-side failure, worth paging attention on in aggregate). Everything else (400, 404, 409, 422, etc.) is deliberately **not** logged here — those are routine client-input errors (malformed request, task not found, idempotency conflict) that would otherwise flood logs with noise disproportionate to their operational value. This matches the task's own framing ("add a log line... for 429/401... leave 5xx as-is or escalate" — this plan chooses to escalate 5xx to `ERROR`, documented here).

#### `app.py` changes

1. Right after `_CHAT_ACTIVE = 0` (currently line 225), add:

```python
# Observability only -- process-local, reset on restart, not a durable audit trail.
_CHAT_REJECTED = 0
_TASK_REJECTED: dict[str, int] = {"image": 0, "video": 0}
```

2. Right before `_browser_health_check_sync()`'s def (currently line 3178), add:

```python
# Observability only -- process-local, reset on restart, not a durable audit trail.
_RECYCLE_COUNT = 0
_LAST_RECYCLE_REASON: str | None = None
_LAST_RECYCLE_AT: float = 0.0
```

(then apply the `_browser_health_check_sync()` edit already shown under item 1, which uses these three globals.)

3. In `_admit_task()` (currently lines 1618-1629), change:

```python
def _admit_task(kind: str, limit: int) -> None:
    """..."""
    active = sum(1 for task in store.tasks.values()
                 if task.get("kind") == kind
                 and task.get("status") in ("queued", "processing"))
    if active >= limit:
        raise HTTPException(
            429, f"{kind.capitalize()} queue is full ({limit} active); retry later")
```

to (headers addition is item 5; shown together here since both touch the same `raise` line):

```python
def _admit_task(kind: str, limit: int) -> None:
    """..."""
    active = sum(1 for task in store.tasks.values()
                 if task.get("kind") == kind
                 and task.get("status") in ("queued", "processing"))
    if active >= limit:
        _TASK_REJECTED[kind] = _TASK_REJECTED.get(kind, 0) + 1
        raise HTTPException(
            429, f"{kind.capitalize()} queue is full ({limit} active); retry later",
            headers=_rate_limit_headers(limit, active))
```

4. In `_admit_chat()` (currently lines 1632-1647), change:

```python
def _admit_chat() -> None:
    """..."""
    global _CHAT_ACTIVE
    with TASK_ADMISSION_LOCK:
        if _CHAT_ACTIVE >= CFG.chat_queue_max:
            raise HTTPException(
                429, f"Chat queue is full ({CFG.chat_queue_max} active); retry later")
        _CHAT_ACTIVE += 1
```

to:

```python
def _admit_chat() -> None:
    """..."""
    global _CHAT_ACTIVE, _CHAT_REJECTED
    with TASK_ADMISSION_LOCK:
        if _CHAT_ACTIVE >= CFG.chat_queue_max:
            _CHAT_REJECTED += 1
            raise HTTPException(
                429, f"Chat queue is full ({CFG.chat_queue_max} active); retry later",
                headers=_rate_limit_headers(CFG.chat_queue_max, _CHAT_ACTIVE))
        _CHAT_ACTIVE += 1
```

5. `admin_status()` (currently lines 2457-2477): insert a new key after `"corrupt_files": list(store.corrupt_files),` (line 2471), before `"config": {...}` (line 2472):

```python
        "observability": {
            "chat_rejected": _CHAT_REJECTED,
            "task_rejected": dict(_TASK_REJECTED),
            "consecutive_generation_failures": _CONSECUTIVE_GEN_FAILURES,
            "recycle_count": _RECYCLE_COUNT,
            "last_recycle_reason": _LAST_RECYCLE_REASON,
            "last_recycle_at": _LAST_RECYCLE_AT or None,
        },
```

6. `_http_exc()` (currently lines 286-297), change:

```python
@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))
```

to:

```python
@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if exc.status_code in (401, 429):
        log.warning("%s %s -> %d: %s", request.method, request.url.path,
                   exc.status_code, exc.detail)
    elif exc.status_code >= 500:
        log.error("%s %s -> %d: %s", request.method, request.url.path,
                  exc.status_code, exc.detail)
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))
```

(`_rate_limit_headers` referenced above is defined in item 5 below — both items touch `_admit_task`/`_admit_chat`'s same lines, so implement them together in one editing pass even though they are conceptually two items.)

#### New tests — `tests/test_observability.py` (new file)

Mirror the fixture style of `tests/test_admission.py` (a `tasks` fixture monkeypatching `app.store`) and `tests/test_browser_health.py` (`StubEngine`). Required cases:

1. **Counter increments**:
   - `test_task_rejection_increments_the_per_kind_counter` — `tasks` fixture saturated at cap for `"image"`; reset `app._TASK_REJECTED = {"image": 0, "video": 0}`; call `app._admit_task("image", <cap>)` expecting `HTTPException`; assert `app._TASK_REJECTED["image"] == 1` and `app._TASK_REJECTED["video"] == 0`.
   - `test_chat_rejection_increments_the_counter` — mirror `test_chat_admission.py`'s cap-then-reject pattern; reset `app._CHAT_REJECTED = 0`; saturate `app.CFG.chat_queue_max`; call `app._admit_chat()` expecting `HTTPException`; assert `app._CHAT_REJECTED == 1`.
   - Add a small autouse fixture resetting `app._CHAT_REJECTED = 0` / `app._TASK_REJECTED = {"image": 0, "video": 0}` before and after every test in this file.

2. **`/admin/status`'s new block**:
   - `test_admin_status_includes_observability_block` — set known values on `app._CHAT_REJECTED`, `app._TASK_REJECTED`, `app._CONSECUTIVE_GEN_FAILURES`, `app._RECYCLE_COUNT`, `app._LAST_RECYCLE_REASON`, `app._LAST_RECYCLE_AT`; call `app.admin_status(True)` directly (mirrors calling other `Depends(auth)`-decorated endpoints directly, as `test_chat_admission.py` already does for `chat_completions`/`responses_api`); assert `result["observability"]` matches exactly.
   - `test_admin_status_observability_defaults` — with all counters at their process-start defaults (`0`/`{"image": 0, "video": 0}`/`None`), assert the block's shape and that `last_recycle_reason`/`last_recycle_at` are `None`.

3. **`_http_exc` logging** (use pytest's `caplog`):
   - `test_http_exc_logs_429` — raise a 429 through a minimal ASGI call (or call `app._http_exc` directly with a fabricated `Request`/`HTTPException` — check which is simpler given this file's other direct-call patterns; direct call is preferred, mirroring how other tests call decorated functions directly) with `caplog.at_level("WARNING")`; assert a log record exists mentioning the path and `429`.
   - `test_http_exc_logs_401` — same, `caplog.at_level("WARNING")`, status 401.
   - `test_http_exc_logs_5xx_at_error_level` — same, `caplog.at_level("ERROR")`, status 502; assert the record's level is `ERROR`.
   - `test_http_exc_does_not_log_404` — same pattern, status 404; assert no log record was emitted by `_http_exc` for this call (use `caplog.at_level("WARNING")` and assert `not caplog.records` or filter by logger name `"muse2api"`).

### Item 4: Admission independence regression test

#### Design decisions

- `_admit_chat`/`_release_chat` (in-memory `_CHAT_ACTIVE` counter, guarded by `TASK_ADMISSION_LOCK`) and `_admit_task` (a `store.tasks`-dict scan filtered by `kind`/`status`, also guarded by `TASK_ADMISSION_LOCK` at its call sites) share the **same lock** for admission bookkeeping, but count **entirely different state** (an integer vs. a dict scan) — so saturating one must never affect the other's admission decision. This was implicitly true by construction in the prior plan, but was never directly tested; this item closes that gap.
- Added to `tests/test_chat_admission.py` (per the task's own suggestion) since that file already has the right fixtures (`reset_chat_active` autouse) and imports; needs one additional import (`from types import SimpleNamespace`, mirroring `tests/test_admission.py`'s own `tasks` fixture) to build an isolated `store.tasks` stand-in.

#### New tests — `tests/test_chat_admission.py` (append)

```python
from types import SimpleNamespace  # add to the existing import block at the top of the file


@pytest.fixture
def tasks(monkeypatch):
    store = SimpleNamespace(tasks={})
    monkeypatch.setattr(app, "store", store)
    return store


def test_chat_admission_does_not_block_task_admission(tasks, monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 1)
    app._admit_chat()  # chat cap is now full
    # _admit_task's store.tasks scan is untouched by chat admission being saturated.
    app._admit_task("video", 4)
    app._admit_task("image", 8)
    with pytest.raises(app.HTTPException):
        app._admit_chat()  # chat cap is still independently enforced


def test_task_admission_does_not_block_chat_admission(tasks, monkeypatch):
    tasks.tasks = {f"v{n}": {"kind": "video", "status": "queued"} for n in range(4)}
    with pytest.raises(app.HTTPException):
        app._admit_task("video", 4)  # video is now at its cap
    # Chat admission's in-memory counter is untouched by the video queue being full.
    app._admit_chat()
    assert app._CHAT_ACTIVE == 1
```

(The `reset_chat_active` autouse fixture already in this file resets `app._CHAT_ACTIVE` before/after every test, including these two, so no extra cleanup is needed for that counter. The new `tasks` fixture only monkeypatches `app.store` for the duration of each test that requests it.)

## Phase 2: Cheap New Features

### Item 5: OpenAI-style rate-limit headers on 429 responses

#### Design decisions

- **Headers plumb through `_http_exc` with zero changes to that handler beyond item 3's logging** — `_http_exc` already does `headers=getattr(exc, "headers", None)` on both its `/v1/*` and non-`/v1/*` response branches (confirmed at app.py lines 294 and 297), so passing `headers=` into `HTTPException(...)` at the raise site is sufficient; FastAPI's `HTTPException` natively accepts a `headers` kwarg.
- **Header set chosen**: `Retry-After` (seconds, OpenAI and the broader HTTP ecosystem both recognize this), `X-RateLimit-Limit-Requests` (the configured cap), `X-RateLimit-Remaining-Requests` (how many admission slots are left at the moment of rejection — always `0` or very close to it, since the client is seeing this header precisely because the cap was hit). These three are enough for existing OpenAI-SDK retry/backoff logic (which primarily keys off `Retry-After` and treats the `X-RateLimit-*` headers as informational) to engage correctly.
- **`X-RateLimit-Reset-Requests` is deliberately omitted.** OpenAI's real `X-RateLimit-Reset-*` headers describe a token-bucket window's next refill instant. This service's admission caps are a **concurrency** limit (how many requests are in flight right now), not a time-window token bucket — there is no deterministic "reset instant" to report truthfully. Inventing one (e.g., "assume a slot frees in N seconds") would be a guess dressed up as a fact. `Retry-After` already gives the client an actionable signal without that false precision.
- **`Retry-After` value**: a fixed advisory constant (`_RATE_LIMIT_RETRY_AFTER_SECONDS = 5`), not a new config field — there is no way to compute a deterministic "next free slot" time from this service's simple counter-based admission (unlike a token bucket, which can compute an exact refill time), so a configurable-but-still-synthetic value would add a knob without adding real precision. `5` seconds is a reasonable "check back shortly" cadence for both the image/video queue (longer-running tasks, default caps 8/4) and the chat queue (shorter-lived requests, default cap 16).
- **`_rate_limit_headers()` is one small shared helper**, not duplicated logic in `_admit_task`/`_admit_chat` — both already compute `limit` and the current active count right at their raise site, so the helper only needs those two integers.

#### `app.py` changes

Right before `_admit_task()`'s def (currently line 1618), add:

```python
_RATE_LIMIT_RETRY_AFTER_SECONDS = 5


def _rate_limit_headers(limit: int, active: int) -> dict:
    """OpenAI-SDK-compatible headers on a 429 so client-side backoff logic engages.

    This is a concurrency admission cap, not a token-bucket time window, so there is
    no true "reset" instant to report -- X-RateLimit-Reset-Requests is intentionally
    omitted rather than invented. Retry-After is a fixed advisory delay for the same
    reason: this counter-based cap cannot compute a deterministic next-free-slot time
    the way a token bucket can.
    """
    return {
        "Retry-After": str(_RATE_LIMIT_RETRY_AFTER_SECONDS),
        "X-RateLimit-Limit-Requests": str(limit),
        "X-RateLimit-Remaining-Requests": str(max(0, limit - active)),
    }
```

Then wire it into `_admit_task()`/`_admit_chat()`'s existing 429 raises exactly as shown under [Item 3](#item-3-observability-counters-adminstatus-block--http_exc-logging) above (both items touch the same two `raise` statements, so implement them in the same editing pass).

#### New tests — `tests/test_admission.py` (append)

```python
def test_admit_task_429_includes_rate_limit_headers(tasks):
    tasks.tasks = {f"v{n}": {"kind": "video", "status": "queued"} for n in range(4)}
    with pytest.raises(app.HTTPException) as exc:
        app._admit_task("video", 4)
    headers = exc.value.headers
    assert headers["Retry-After"] == str(app._RATE_LIMIT_RETRY_AFTER_SECONDS)
    assert headers["X-RateLimit-Limit-Requests"] == "4"
    assert headers["X-RateLimit-Remaining-Requests"] == "0"
```

#### New tests — `tests/test_chat_admission.py` (append)

```python
def test_admit_chat_429_includes_rate_limit_headers(monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 2)
    app._admit_chat()
    app._admit_chat()
    with pytest.raises(app.HTTPException) as exc:
        app._admit_chat()
    headers = exc.value.headers
    assert headers["Retry-After"] == str(app._RATE_LIMIT_RETRY_AFTER_SECONDS)
    assert headers["X-RateLimit-Limit-Requests"] == "2"
    assert headers["X-RateLimit-Remaining-Requests"] == "0"
```

#### Test Procedure (manual)

1. Start the server with `MUSE2API_CHAT_QUEUE_MAX=1`.
2. Send one slow chat request in the background; while it is in flight, send a second request.
3. Confirm the second response is HTTP 429 and includes `Retry-After: 5`, `X-RateLimit-Limit-Requests: 1`, `X-RateLimit-Remaining-Requests: 0` (e.g. `curl -i`).

### Item 6: Per-account `use_count` surfaced in admin

#### Design decisions — **important finding: most of this item is already done**

- `store.py`'s `_record_use()` (lines 260-272) already sets `account["last_used"]` and `account["use_count"]` on every pick.
- `Store.public_account()` (lines 186-191) copies **every** key from the account dict except `"cookies"`/`"proxy"` — it does **not** need an explicit allowlist update, because it already passes `use_count` through today, unmodified by this plan.
- `Store.list_accounts()` (lines 193-206) calls `public_account()` and adds a few more derived fields; it already returns `use_count` today.
- `/admin/accounts` (app.py `list_accounts()`, lines 2481-2483) and `/admin/status` (`admin_status()`) both already return `store.list_accounts()`'s output (directly, or as `"accounts": store.list_accounts()`), so `use_count` is **already present in every admin API response today**, before this plan.
- `admin.html` already renders `last_used` (via `ago(a.last_used)`, line 1854, under a "最后使用"/"Last used" column, line 372/993) — but there is **no column anywhere that displays `use_count`**.
- **Conclusion**: the only change this item actually needs is a **pure front-end display addition** — one new `<th>`/`<td>` pair and one i18n dictionary entry. No `store.py` or `app.py` change is needed for the mandatory part of this item.
- The optional per-kind usage breakdown (mentioned in the original research as a stretch sub-item) is deferred — see [Scope](#scope)'s "Out of scope / deferred" section for why.
- **i18n placement choice**: the new header is added as a plain (untagged) `<th>`, matching 7 of the existing 8 headers in this exact table (only "启用"/`col-enabled` uses `data-i18n`) — `admin.html`'s `translateTree()` already walks every text node and translates via the whole-sentence `I18N_EN` dictionary regardless of `data-i18n` tagging, so adding the Chinese→English pair there is sufficient and consistent with the table's existing convention (not a departure from it, and not an attempt to "fix" the table's inconsistent `data-i18n` usage, which is out of scope here).

#### `admin.html` changes

1. Header row (currently lines 371-374), change:

```html
        <thead><tr>
          <th>标签</th><th>账号 ID</th><th>状态</th><th>有效期</th><th>额度</th><th data-i18n="col-enabled">启用</th><th>Cookie</th>
          <th>最后使用</th><th>备注</th><th style="min-width:250px">操作</th>
        </tr></thead>
```

to:

```html
        <thead><tr>
          <th>标签</th><th>账号 ID</th><th>状态</th><th>有效期</th><th>额度</th><th data-i18n="col-enabled">启用</th><th>Cookie</th>
          <th>最后使用</th><th>调用次数</th><th>备注</th><th style="min-width:250px">操作</th>
        </tr></thead>
```

2. Row template inside `loadPool()`'s `accounts.map(a => {...})` (currently around lines 1846-1866), change:

```html
        <td class="mono">${a.cookie_count}</td>
        <td class="mono" title="${a.last_used ? fmtTime(a.last_used) : ''}">${ago(a.last_used)}</td>
        <td class="mono ellip" title="${esc(a.note||'')}">${esc(a.note || '—')}</td>
```

to:

```html
        <td class="mono">${a.cookie_count}</td>
        <td class="mono" title="${a.last_used ? fmtTime(a.last_used) : ''}">${ago(a.last_used)}</td>
        <td class="mono">${a.use_count ?? 0}</td>
        <td class="mono ellip" title="${esc(a.note||'')}">${esc(a.note || '—')}</td>
```

3. `I18N_EN` dictionary (currently lines 987-995), change:

```js
  "最后使用": "Last used",
  "备注": "Notes",
```

to:

```js
  "最后使用": "Last used",
  "调用次数": "Use count",
  "备注": "Notes",
```

4. Bump `colspan="10"` → `colspan="11"` at all occurrences tied to `#poolBody`'s empty/loading states (re-grep `grep -n 'colspan="10"' admin.html` at edit time to get the exact current set — at plan time these are lines 375, 1836, 1870, and 1871):

```html
<!-- line 375, static markup -->
<tbody id="poolBody"><tr><td colspan="10" class="empty">加载中…</td></tr></tbody>
```
→ `colspan="11"`

```js
// line 1836
$('poolBody').innerHTML = '<tr><td colspan="10" class="empty">还没有账号。去「获取 Cookie」页签按三步导入一个</td></tr>';
```
→ `colspan="11"`

```js
// lines 1870-1871
? '<tr><td colspan="10" class="empty">🔑 请先在右上角填入 API Key 并点「保存」</td></tr>'
: `<tr><td colspan="10" class="empty">加载失败：${esc(e.message)}</td></tr>`;
```
→ both `colspan="11"`

#### Verification (manual — no automated pytest coverage for this item)

- Load `/admin` with at least one account that has generated at least once; confirm a "调用次数"/"Use count" column appears with a non-zero integer, positioned between "最后使用"/"Last used" and "备注"/"Notes".
- Toggle the language switch to English; confirm the header reads "Use count" (not raw Chinese).
- Optionally, run `tests/test_admin_layout.py` manually (`python3 tests/test_admin_layout.py --probe-only`) to confirm the added column does not break the existing responsive-layout assertions (button count, Notes column `display`/`white-space`) — this script is excluded from automated `pytest -q` collection (see `tests/conftest.py`'s `collect_ignore`), so it is not a CI gate, but it is cheap to run manually as extra confidence.

### Item 7 (Optional/Stretch): `GET /metrics` Prometheus text exposition

**Implement this item only after items 1-6 are code-complete and their tests pass.** If, once there, it does not compose as cleanly as expected (e.g., a counter from item 3 turns out to need a shape change), flag it as a follow-up in this plan's closeout instead of forcing it in.

#### Design decisions

- **Hand-written exposition, stdlib only.** `requirements.txt` is intentionally minimal (confirmed by reading it — no metrics/observability library present today); this plan does **not** add `prometheus_client`. The Prometheus text exposition format is simple enough to build with f-strings and `"\n".join(...)`.
- **Auth: gated behind the existing `auth` dependency**, the same API key every `/v1/*` and `/admin/*` route (except `/healthz`/`/readyz`) already requires. This is a deliberate choice, not the more common "unauthenticated `/metrics` for internal-network scraping" pattern, because: (a) this service is frequently deployed directly on the open internet (it is a single API-key-gated OpenAI-compatible endpoint, not necessarily behind a private network boundary), and (b) the counters exposed (recycle counts/reasons, rejection rates) are mildly sensitive operational detail, consistent with this app's existing policy of gating everything except the two liveness probes. Prometheus scrape configs support a bearer token (`authorization: {type: Bearer, credentials: <key>}` or `bearer_token_file`), so this does not block real-world scraping — it just requires one extra line in the scrape config, which is documented in this plan's README addition (see Implementation Checklist).
- **Route placement**: `GET /metrics` (not `/admin/metrics`), matching the literal, widely-recognized Prometheus convention, even though it sits alongside the `/admin/*` routes in file organization and shares their auth dependency.
- **Content**: counters from items 1 and 3 (`_CHAT_REJECTED`, `_TASK_REJECTED` per kind, `_RECYCLE_COUNT`, `_CONSECUTIVE_GEN_FAILURES`) plus `store.stats()`'s existing pool-health gauges (`total`, `enabled`, `disabled`, `healthy`, `error`, `expiring`, `expired`). `use_count` (item 6) is deliberately **not** exposed per-account here — Prometheus labels are meant for low-cardinality dimensions, and per-account metrics for a pool that could grow to many accounts is a poor fit for a single scrape target; `/admin/status`/`/admin/accounts` remain the right place to inspect individual accounts.

#### `app.py` changes

Insert after `admin_status()` ends (currently line 2477, plus its trailing blank lines), before the `# ------------------------- Admin: account pool -------------------------` comment (currently line 2480):

```python
@app.get("/metrics")
def metrics(_=Depends(auth)):
    """Hand-written Prometheus text exposition (stdlib only; no prometheus_client
    dependency -- requirements.txt stays minimal). Gated by the same API key as
    every /admin/* route; Prometheus scrape configs support a bearer token, so this
    does not require an unauthenticated surface."""
    stats = store.stats()
    lines = [
        "# HELP muse2api_chat_rejected_total Chat/responses admission rejections (HTTP 429) since process start.",
        "# TYPE muse2api_chat_rejected_total counter",
        f"muse2api_chat_rejected_total {_CHAT_REJECTED}",
        "",
        "# HELP muse2api_task_rejected_total Image/video admission rejections (HTTP 429) since process start, by kind.",
        "# TYPE muse2api_task_rejected_total counter",
    ]
    for kind, count in sorted(_TASK_REJECTED.items()):
        lines.append(f'muse2api_task_rejected_total{{kind="{kind}"}} {count}')
    lines += [
        "",
        "# HELP muse2api_browser_recycle_total Proactive Chromium recycles since process start.",
        "# TYPE muse2api_browser_recycle_total counter",
        f"muse2api_browser_recycle_total {_RECYCLE_COUNT}",
        "",
        "# HELP muse2api_consecutive_generation_failures Current consecutive generation-failure streak (resets to 0 on any success).",
        "# TYPE muse2api_consecutive_generation_failures gauge",
        f"muse2api_consecutive_generation_failures {_CONSECUTIVE_GEN_FAILURES}",
        "",
        "# HELP muse2api_accounts Account pool counts by status.",
        "# TYPE muse2api_accounts gauge",
    ]
    for key in ("total", "enabled", "disabled", "healthy", "error", "expiring", "expired"):
        lines.append(f'muse2api_accounts{{status="{key}"}} {stats.get(key, 0)}')
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
```

(`Response` is already imported from `fastapi.responses` at the top of `app.py` — no new import needed.)

#### New tests — `tests/test_observability.py` (append, if this item is implemented)

```python
def test_metrics_returns_prometheus_text(monkeypatch):
    monkeypatch.setattr(app.CFG, "api_key", "")  # unauthenticated in this test env, matches other admin-route tests
    response = app.metrics(True)
    body = response.body.decode()
    assert "# TYPE muse2api_chat_rejected_total counter" in body
    assert "muse2api_task_rejected_total{kind=\"image\"}" in body
    assert "muse2api_task_rejected_total{kind=\"video\"}" in body
    assert "# TYPE muse2api_accounts gauge" in body
    assert response.media_type == "text/plain; version=0.0.4"


def test_metrics_requires_auth_when_key_configured(monkeypatch):
    monkeypatch.setattr(app.CFG, "api_key", "secret-key")
    with pytest.raises(app.HTTPException) as exc:
        app.auth(authorization=None)
    assert exc.value.status_code == 401
    # /metrics depends on the same `auth` callable FastAPI would inject per-request;
    # this proves the dependency itself rejects a missing key the same way it does
    # for every other /admin/* route -- app.metrics's own Depends(auth) wiring is
    # exercised by FastAPI's dependency injection at request time, not by a direct
    # Python call in this test.
```

#### `README.md` addition (documents the auth requirement for scrape configs)

Add one short note near the environment-variable table or a new "Monitoring" subsection: `GET /metrics` returns Prometheus text exposition, gated by the same `Authorization: Bearer <MUSE2API_KEY>` header as other endpoints; a Prometheus scrape config needs an `authorization: {credentials: <key>}` (or equivalent `bearer_token`) entry for this target.

#### Test Procedure (manual)

1. `curl -H "Authorization: Bearer <key>" http://127.0.0.1:18610/metrics` — confirm a 200 with `# HELP`/`# TYPE` lines and current counter values.
2. `curl http://127.0.0.1:18610/metrics` (no header, with `MUSE2API_KEY` set) — confirm 401.
3. Trigger one 429 (per Phase 1's manual test) and confirm `muse2api_chat_rejected_total` (or `muse2api_task_rejected_total{kind="..."}`) increments on the next scrape.

## Implementation Checklist

1. Re-verify current line numbers for all [Touchpoints](#touchpoints) above (code may have drifted since 08-11-26) before editing.
2. `config.py`: add `browser_max_consecutive_failures` field (after `browser_max_uptime_hours`).
3. `app.py`: add `_CONSECUTIVE_GEN_FAILURES`, `_GEN_FAILURE_LOCK`, `_note_generation_result()` (before `_run_generation`).
4. `app.py`: wrap `_run_generation()`'s body in `try/except Exception` calling `_note_generation_result()`.
5. `app.py`: add `_note_generation_result()` calls to `safe_chat_stream()`'s consumer loop (`elif`/`else` branches).
6. `app.py`: add `_CHAT_REJECTED`/`_TASK_REJECTED` globals (after `_CHAT_ACTIVE`); add `_RECYCLE_COUNT`/`_LAST_RECYCLE_REASON`/`_LAST_RECYCLE_AT` globals (before `_browser_health_check_sync`).
7. `app.py`: extend `_browser_health_check_sync()` with the third (consecutive-failure) trigger and the recycle-counter bookkeeping, per item 1/item 3's combined diff above.
8. `app.py`: swap `/readyz` and `admin_status()`'s `browser_running` computation to `engine.is_healthy()[0]`.
9. `app.py`: add `_rate_limit_headers()` + `_RATE_LIMIT_RETRY_AFTER_SECONDS`; wire `headers=` and the `_TASK_REJECTED`/`_CHAT_REJECTED` increments into `_admit_task()`/`_admit_chat()`'s existing 429 raises (one combined editing pass for items 3 and 5).
10. `app.py`: add the `"observability"` block to `admin_status()`'s returned dict.
11. `app.py`: add the `log.warning`/`log.error` lines to `_http_exc()`.
12. Extend `tests/test_browser_health.py` with items 1 and 3's new tests (listed under [Item 1](#item-1-consecutive-failure-wedged-page-recycle-trigger)).
13. Extend `tests/test_chat_admission.py` with item 4's independence tests and item 5's header test (add `from types import SimpleNamespace` to its imports).
14. Extend `tests/test_admission.py` with item 5's header test.
15. Create `tests/test_observability.py` with item 3's counter/`/admin/status`/`_http_exc`-logging tests.
16. Run `pytest tests/test_browser_health.py tests/test_lock_order.py tests/test_stream_backpressure.py tests/test_concurrency.py tests/test_admission.py tests/test_chat_admission.py tests/test_observability.py tests/test_responses_api.py tests/test_account_proxy.py tests/test_config_defaults.py tests/test_admin_i18n.py -q` — all green.
17. Manual test: force a wedged-page scenario (or directly call `app._browser_health_check_sync()` with the counter preset, per the new unit tests) and confirm the third recycle trigger fires and resets the counter.
18. Manual test: hit a 429 on both an image/video endpoint and a chat endpoint; confirm `Retry-After`/`X-RateLimit-*` headers are present (Phase 2 item 5's manual test).
19. Manual test: read `/admin/status` and confirm the new `observability` block is present with live values.
20. `admin.html`: add the `use_count` column (header, row template, `I18N_EN` entry, four `colspan` bumps).
21. Manual verification: load `/admin`, confirm the new column renders correctly in both `zh` and `en` modes (optionally run `tests/test_admin_layout.py --probe-only` manually).
22. `README.md`/`.env.example`: document `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES` (mirroring the existing `MUSE2API_BROWSER_MAX_UPTIME_HOURS` entries).
23. **(Optional/stretch)** `app.py`: add the `GET /metrics` route; `README.md`: add the scrape-auth note; extend `tests/test_observability.py` with item 7's tests.
24. **(Optional/stretch)** Manual test: `curl` `/metrics` with and without the API key; confirm 200/401 and that counters reflect a triggered 429 from step 18.
25. Run the full test suite (`pytest -q`) once more to confirm no regressions anywhere.
26. Validate this plan artifact: `node /Users/crash/.claude/skills/vc-generate-plan/scripts/validate-plan-artifact.mjs process/general-plans/active/observability_and_metrics_PLAN_08-11-26.md` (script found at the global `~/.claude/skills/vc-generate-plan/` location — this repo has no local `.claude/skills/`).

## Acceptance Criteria

1. `app._CONSECUTIVE_GEN_FAILURES` increments by exactly 1 on a plain `MuseGenerationError` surfaced through either `_run_generation` (image/video) or `safe_chat_stream` (chat/responses), and resets to `0` on any success through either path; it does **not** change on `MuseAuthError`, `MuseProxyError`, `MuseModelRefusalError`, or `RefImageSafetyError`.
2. Once `app._CONSECUTIVE_GEN_FAILURES >= CFG.browser_max_consecutive_failures` (default `5`) and `CFG.browser_max_consecutive_failures > 0`, `_browser_health_check_sync()` recycles the browser (subject to the existing `GEN_LOCK`-gated deferral) and resets the counter to `0` immediately after a successful recycle caused by this trigger.
3. `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES` defaults to `5`; `0` disables only this trigger, leaving the pre-existing crash/CDP-unresponsive and (if configured) uptime triggers active.
4. `/readyz` and `/admin/status` both report `browser_running` via `engine.is_healthy()[0]`; the Docker `HEALTHCHECK` (targeting `/healthz`) is unaffected.
5. `/admin/status` includes a new `"observability"` key with live `chat_rejected`, `task_rejected` (per-kind dict), `consecutive_generation_failures`, `recycle_count`, `last_recycle_reason`, `last_recycle_at` values; all other existing keys are unchanged in name/type.
6. `_http_exc` logs a `WARNING` for every 401/429 response and an `ERROR` for every `>= 500` response, with no log line for 400/404/409/422-class responses.
7. A new regression test proves `_admit_chat`/`_CHAT_ACTIVE` and `_admit_task`'s `store.tasks` scan never block or interfere with each other.
8. Every 429 raised by `_admit_task` or `_admit_chat` includes `Retry-After`, `X-RateLimit-Limit-Requests`, and `X-RateLimit-Remaining-Requests` headers, with the existing JSON error body and message text unchanged.
9. `admin.html`'s account pool table displays a `use_count` value per account (already returned by the backend pre-plan), in both `zh` and `en` modes, without breaking the existing empty/loading-state rows (`colspan` updated consistently).
10. **(If item 7 is implemented)** `GET /metrics` returns `text/plain; version=0.0.4` Prometheus exposition text reflecting items 1/3's counters and `store.stats()`'s pool-health gauges, gated by the same `auth` dependency as other admin routes, with no new entry added to `requirements.txt`.
11. All existing tests listed in [Blast Radius](#blast-radius) pass unmodified; all new tests listed under each item pass.

## Verification Evidence

Required evidence before this plan can be marked VERIFIED (not just CODE DONE):

- **Automated**: full output of `pytest -q` (or at minimum the targeted subset listed in Implementation Checklist step 16) showing all pass, pasted or summarized in the EXECUTE-phase closeout.
- **New-behavior proof**: `tests/test_browser_health.py`'s new tests, `tests/test_chat_admission.py`'s new tests, `tests/test_admission.py`'s new test, and `tests/test_observability.py` (new file) all pass, demonstrating each of the 7 items actually works, not just that nothing else broke.
- **Manual evidence for item 1**: a server log line showing `[Browser health] Recycling Chromium: N consecutive generation failures` after the threshold is reached (or the equivalent unit-test-level proof if a live reproduction is impractical in the execution environment).
- **Manual evidence for item 2**: `/readyz`'s `browser_running` field changes to `false` immediately after killing the real Chromium process, without waiting for the next request's fast-path check.
- **Manual evidence for item 3**: a captured `/admin/status` response showing the `observability` block with non-default values after triggering at least one rejection and one recycle; a captured log line for a 429/401 and for a 5xx.
- **Manual evidence for item 5**: a captured 429 response's headers (`curl -i`) showing `Retry-After`/`X-RateLimit-*`.
- **Manual evidence for item 6**: a screenshot or description of the admin page showing the new "调用次数"/"Use count" column with a real value.
- **Manual evidence for item 7 (if implemented)**: a captured `/metrics` response body.
- **No regression evidence**: explicit confirmation that every file listed in [Blast Radius](#blast-radius) passes without modification to its existing test bodies (their pre-existing content should be byte-identical before/after this plan's execution, aside from new test functions appended to the four extended files) — if any existing test needed an edit, that is a signal this plan's design assumptions were wrong and must be revisited before claiming done.
- This repo has no `process/context/tests/all-tests.md` router; the test commands above are read directly from `pytest.ini` (`testpaths = tests`, `asyncio_mode = auto`) and `tests/conftest.py` (documents which standalone scripts are excluded from collection).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The consecutive-failure counter false-triggers on an ordinary burst of unrelated transient errors (e.g., several different accounts' sessions expiring around the same time, which raise `MuseAuthError`, not `MuseGenerationError`) | The exclusion list (`MuseAuthError`, `MuseProxyError`, `MuseModelRefusalError`, `RefImageSafetyError`) specifically filters these out; only a plain `MuseGenerationError` (timeout/deadline/CDP-level failure) counts, and those are precisely the symptoms a wedged page produces |
| The counter is a global shared across image/video/chat; a genuinely wedged page recycling the browser mid-way through an unrelated account's auth-retry flow | `_browser_health_check_sync`'s existing `GEN_LOCK` non-blocking-acquire guard (unchanged by this plan) still defers the recycle whenever a generation is actually in flight, exactly as it already does for the crash/uptime triggers |
| A new `_GEN_FAILURE_LOCK` introduces a lock-ordering bug | It is never acquired together with any other lock (`ACCOUNT_POOL`, `GEN_LOCK`, `TASK_ADMISSION_LOCK`) — confirmed by inspection of every call site added by this plan; `tests/test_lock_order.py` is unmodified and still passes |
| `/readyz` becomes slightly slower (one extra local HTTP ping) under frequent polling | Accepted and documented; `is_healthy()`'s 3s timeout bounds the worst case, and the existing keepalive loop already pays this exact cost every ~15 minutes with no reported issue |
| `_http_exc`'s new logging floods logs under a 429 storm (e.g., a client retrying aggressively against a saturated queue) | Scoped to only 401/429/5xx (not 400/404/409/422), and this is the operationally useful signal this item exists to surface; if log volume becomes a real problem in practice, rate-limiting the log line itself would be a natural, separate follow-up — not pursued speculatively here |
| Rate-limit headers' `Retry-After: 5` is a guess, not a precise computation, and could mislead a very strict client | Documented explicitly as an advisory fixed delay in both the code comment and this plan; it is strictly better than today's total absence of any retry signal, and OpenAI-SDK clients generally treat `Retry-After` as a floor/hint, not a guarantee |
| `admin.html`'s new column shifts `colspan` and someone misses one of the (currently) four occurrences | [Touchpoints](#touchpoints) explicitly calls out re-grepping `colspan="10"` at edit time to catch the exact current count and locations, rather than relying on this plan's line numbers alone |
| `/metrics` (item 7) exposes operational detail (rejection/recycle counts) that a bad actor could use for reconnaissance | Gated behind the same API key as every other non-liveness endpoint, consistent with this app's existing security posture; not exposed unauthenticated |
| New env var silently does nothing if not documented | Implementation Checklist step 22 requires README/`.env.example` updates before calling this plan done |

## Resume and Execution Handoff

If this plan is picked up cold (new session, compacted context, or a different agent):

1. **Read this file in full first** — it is the single source of truth; no companion phase files exist (this is a standard complex plan, not a phase program).
2. **Re-verify line numbers** in [Touchpoints](#touchpoints) against the current state of `app.py`, `engine.py`, `config.py`, `admin.html` before editing anything — this plan was written against the repo state as of 08-11-26 (which already includes the prior `stability_performance` plan's merged changes) and line numbers may have drifted further since.
3. **Order of work**: Phase 1 (items 1-4) and Phase 2 items 5-6 are independent of each other and can be done in either order; item 7 is optional/stretch and depends on items 1 and 3's counters existing first (so it must come last). Within Phase 1, items 3 and 5 both edit the exact same two `raise HTTPException(429, ...)` statements in `_admit_task`/`_admit_chat` — implement them together in one editing pass per item's instructions (both are written as a single combined diff above) rather than touching those two lines twice.
4. **Key invariant to never break**: `ACCOUNT_POOL` is always acquired before `GEN_LOCK`, everywhere (unchanged by this plan — `_note_generation_result`'s new `_GEN_FAILURE_LOCK` is never combined with either). `engine.recycle()` must never be called without the caller already holding `GEN_LOCK` via a non-blocking acquire it personally performed and will personally release (unchanged, pre-existing invariant from the prior plan).
5. **Key new invariant for this plan**: `_note_generation_result()` must only ever be called from `_run_generation`'s outer try/except and `safe_chat_stream`'s consumer loop — not from any other call site, and not from inside `_run_generation_locked`'s or `safe_chat_stream`'s `worker()`'s internal retry logic. Adding a third call site risks double-counting a single logical failure (e.g., once per retry attempt instead of once per overall request).
6. **Test commands to run before declaring done**: see [Verification Evidence](#verification-evidence) above; there is no CI config discovered in this repo pass beyond `pytest.ini`/`requirements-dev.txt`, so local `pytest -q` is the authoritative gate.
7. **Plan-validator script**: `node /Users/crash/.claude/skills/vc-generate-plan/scripts/validate-plan-artifact.mjs process/general-plans/active/observability_and_metrics_PLAN_08-11-26.md` — this repo itself has no local `.claude/skills/`, so the script is invoked from its global location.
8. **One other active plan exists** in `process/general-plans/active/` as of this plan's creation: `stability_performance_PLAN_08-11-26.md` (already executed/committed). This plan is independent of it and does not need to be merged or reconciled with it — only built on top of its already-landed code.
9. **This plan file itself is the execute-anchor** — not a legacy `PLAN.md`/`phase-*.md` shape; it is a single direct `*_PLAN_*.md` artifact.

## Cursor + RIPER-5 Guidance

- **Cursor Plan mode**: import the [Implementation Checklist](#implementation-checklist) directly; work through items 1-19 (Phase 1 + Phase 2 items 5/3's shared editing pass) before items 20-22 (item 6's UI change + docs), then items 23-24 only if pursuing the optional item 7, then item 25 (final full-suite run) and item 26 (plan-artifact validation).
- **RIPER-5 mode**: this plan was produced in PLAN mode. Review carefully, then say **"ENTER EXECUTE MODE"** to begin implementation exactly as specified above. No further RESEARCH or INNOVATE work is expected to be needed — all touchpoints, line numbers, and test strategies were verified by reading the live source files at plan time.
- If scope changes mid-execution (e.g., a touchpoint's surrounding code has materially changed since 08-11-26, not just shifted line numbers, or item 7 turns out not to compose cleanly as expected), pause and update this plan file's relevant section before continuing, rather than silently deviating.
