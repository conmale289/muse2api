# Stability & Performance Hardening — Chat Admission Cap + Chromium Health Policy

**Date**: 08-11-26
**Complexity**: Complex (single execution stream — not a phase program; no `process/features/` folder exists for this and none is warranted)
**Status**: 🔨 CODE DONE (automated tests green; manual Test Procedures for Phase 1 and Phase 2 not yet exercised against a live server/Chromium in this execution environment)

**Context note**: this repo does not have a `process/context/all-context.md` router (only `process/general-plans/` exists under `process/`). All context for this plan was gathered directly from the source files cited below and from `docs/enhancement-audit-2026-11-06.md` (prior audit — informational history only, not re-derived here).

## Quick Links

- [Overview](#overview)
- [Scope](#scope)
- [Phase Completion Rules](#phase-completion-rules)
- [Execution Brief](#execution-brief)
- [Touchpoints](#touchpoints)
- [Public Contracts](#public-contracts)
- [Blast Radius](#blast-radius)
- [Phase 1: Chat/Responses Admission Cap](#phase-1-chatresponses-admission-cap)
- [Phase 2: Chromium Health / Restart Policy](#phase-2-chromium-health--restart-policy)
- [Phase 3 (Optional/Stretch): cdp.py pump() dead code](#phase-3-optionalstretch-cdppy-pump-dead-code)
- [Deferred / Out of Scope](#deferred--out-of-scope)
- [Implementation Checklist](#implementation-checklist)
- [Acceptance Criteria](#acceptance-criteria)
- [Verification Evidence](#verification-evidence)
- [Risks and Mitigations](#risks-and-mitigations)
- [Resume and Execution Handoff](#resume-and-execution-handoff)
- [Cursor + RIPER-5 Guidance](#cursor--riper-5-guidance)

## Overview

muse2api drives one shared headless Chromium via CDP to automate muse.ai as an OpenAI-compatible API (`app.py`, `engine.py`, `store.py`, `proxy.py`, `cdp.py`, `config.py`). A prior research pass (`docs/enhancement-audit-2026-11-06.md`) fixed 48 issues and left 5 new, still-open findings. This plan addresses the two that matter for production stability under load:

1. **No admission cap on `/v1/chat/completions` / `/v1/responses`.** Images and video already refuse new work past a configurable queue cap (`_admit_task`, HTTP 429). Chat/responses have no equivalent — a burst of concurrent chat requests spawns unbounded `threading.Thread(daemon=True)` workers, each holding an account lease and blocking up to `MUSE2API_CHAT_TIMEOUT` (default 300s).
2. **No proactive Chromium health/restart policy.** `engine.py`'s `_start_locked()` only detects a dead Chromium reactively (on the next request's fast-path check), and that check doesn't even ping CDP — a hung-but-still-running Chromium process passes the fast path forever.

A third item (deleting or locking `cdp.py`'s unused `pump()`) is included as a cheap optional stretch fix. Two further items from the audit (i18n drift test breadth; missing `tini` init process in Docker) are explicitly **out of scope** for this plan — see [Deferred / Out of Scope](#deferred--out-of-scope).

This is a single execution stream: both mandatory phases are independent of each other (different files, no shared new state), so they can be implemented and tested in either order, but are specified here as Phase 1 then Phase 2 for a clean linear walkthrough.

## Scope

### In scope (mandatory)

- **Phase 1**: admission cap for `/v1/chat/completions` and `/v1/responses`, mirroring the existing image/video 429 contract.
- **Phase 2**: background Chromium liveness probe + proactive recycle, coordinated with `GEN_LOCK` so no in-flight generation is disrupted.

### In scope (optional/stretch, do only if Phase 1 and 2 are done and tested first)

- **Phase 3**: resolve `cdp.py`'s unprotected `pump()` vs `send()` race. Default resolution: delete `pump()` (confirmed zero call sites repo-wide, verified via `grep -rn "\.pump(" .` at plan time). Alternative (if whoever executes this prefers to keep it): make it acquire `self._lock` like `send()` does. This plan recommends deletion.

### Out of scope / deferred (see [Deferred / Out of Scope](#deferred--out-of-scope) for rationale)

- Item 4: broadening `tests/test_admin_i18n.py` to catch CJK literals embedded directly in `app.py` strings or untagged `admin.html` text.
- Item 5: adding `tini` (or another init process) to the Docker image to reap orphaned Chromium children on an abnormal crash.

## Phase Completion Rules

A phase is NOT complete until:

1. **Integration Test** - Works with other system pieces (existing lock-order, admission, and backpressure invariants still hold — see [Blast Radius](#blast-radius)).
2. **Manual Test** - A human (or automated harness) can trigger the new behavior and observe the expected HTTP/log outcome.
3. **Data Verification** - For Phase 1: the in-memory admission counter returns to 0 after every request path (success, error, disconnect). For Phase 2: `engine._started_at` / `is_healthy()` reflect real process state after a forced recycle.
4. **Error Handling** - Failure cases (cap exceeded, Chromium crashed, CDP unresponsive, recycle attempted mid-generation) behave as specified, not by accident.
5. **User Confirmation** - User (or the EXECUTE-phase agent closing this plan) confirms pytest is green and manually exercises at least one 429 cap-hit and one simulated Chromium-unhealthy recycle.

Status markers used below:
- ⏳ PLANNED - Not started
- 🔨 CODE DONE - Written but not E2E tested
- 🧪 TESTING - Currently being tested
- ✅ VERIFIED - Tested AND confirmed working (requires explicit user confirmation language in the closeout, not just "tests pass")
- 🚧 BLOCKED - Has issues

## Execution Brief

| Phase | What happens | Test | Verify | Done when |
|---|---|---|---|---|
| 1. Chat admission cap | Add a counter-based admission gate (`_admit_chat`/`_release_chat`) to `/v1/chat/completions` and `/v1/responses`, both streaming and non-streaming branches | New `tests/test_chat_admission.py`; existing `tests/test_admission.py`, `tests/test_lock_order.py`, `tests/test_stream_backpressure.py` still pass | `app._CHAT_ACTIVE == 0` after every request path in tests; 429 body matches existing `{"error": {...}}` shape via `_http_exc` | All new + existing tests pass; manual curl burst past `MUSE2API_CHAT_QUEUE_MAX` returns 429 |
| 2. Chromium health policy | Add `MuseEngine.is_healthy()` / `.recycle()` / `.uptime_seconds`; wire a GEN_LOCK-gated check into the existing `_keepalive_loop` tick | New `tests/test_browser_health.py`; existing `tests/test_keepalive_scheduling.py` still passes | Forced-unhealthy engine triggers `recycle()` only when `GEN_LOCK` is free; deferred when held | All new + existing tests pass; manual test kills the real Chromium process and confirms the next keepalive tick recycles it without touching in-flight requests |
| 3. (Optional) `cdp.py` pump() | Delete `CDP.pump()` (dead code) or lock it, execute-agent's choice | No new test required for deletion (removing unused code); if kept, add a test asserting `self._lock` is held during `pump()` | N/A for deletion | Deletion: file compiles, no import/reference breaks (there are none). Keep: new test passes |

**Expected Outcome**:
- `/v1/chat/completions` and `/v1/responses` return HTTP 429 (OpenAI-shaped error) once `MUSE2API_CHAT_QUEUE_MAX` concurrent requests are in flight, instead of spawning unbounded threads.
- A crashed-but-not-yet-requested, or hung-but-still-running, shared Chromium process is proactively detected and recycled within one keepalive tick (~15 minutes, or sooner if the admin triggers keepalive manually), without ever tearing down the browser while a generation or chat request is mid-flight.
- All existing tests continue to pass unmodified except where this plan explicitly calls for an update (none of the existing files require edits — only new test files are added).

## Touchpoints

Exact files and functions this plan will touch, with line numbers as read at plan time (08-11-26) — re-verify before editing in case of drift:

### Phase 1

- `config.py`
  - Insert a new `chat_queue_max` field in `class Config` (currently lines 68-167), placed after the `video_queue_max` field (lines 131-132), before the `task_retention` field (line 136-137).
- `app.py`
  - Module globals around line 218-222 (`GEN_LOCK`, `ACCOUNT_POOL`, `IMAGE_TASK_LOCK`, `TASK_ADMISSION_LOCK`): add one new module-level counter `_CHAT_ACTIVE = 0` right after line 222.
  - New helper functions `_admit_chat()` / `_release_chat()`: insert after `_admit_task()` (currently defined at lines 1615-1626), before `_reject_multiple_images()` (line 1629).
  - `chat_completions()` (currently lines 2063-2208):
    - Admission call: insert `_admit_chat()` right after the `if not prompt: raise HTTPException(400, "messages is empty")` guard (currently lines 2082-2083), before `tool_note = ...` (line 2087).
    - Streaming branch: `sync_stream()` (def at line 2113, body through line 2179) — add `finally: _release_chat()` after the last `except Exception as exc:` block (currently ending line 2179), before `return StreamingResponse(...)` (line 2180).
    - Non-streaming branch: the `try: text = await asyncio.to_thread(run) / except MuseAuthError / except MuseGenerationError` block (currently lines 2186-2191) — add `finally: _release_chat()`.
  - `responses_api()` (currently lines 2246-2404+):
    - Admission call: insert `_admit_chat()` right after `if not prompt: raise HTTPException(400, "input is empty")` (currently lines 2261-2262), before `model = resolve_model(...)` (line 2264).
    - Streaming branch: `sync_stream()` (def at line 2301, body through the `except Exception as exc:` block ending at line 2388) — add `finally: _release_chat()` before `return StreamingResponse(...)` (line 2389).
    - Non-streaming branch: the `try: text = await asyncio.to_thread(run) / except MuseAuthError / except MuseGenerationError` block (currently lines 2395-2400) — add `finally: _release_chat()`.
- `tests/test_chat_admission.py` (new file).

### Phase 2

- `engine.py`
  - `MuseEngine.__init__` (currently lines 298-313): add `self._started_at: float = 0.0` after `self._log = None` (line 311), before `os.makedirs(cfg.profile_dir, exist_ok=True)` (line 312).
  - `_start_locked()` (currently lines 357-408):
    - Reuse-existing-endpoint branch: after `self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)` (line 380), before `return` (line 381) — set `self._started_at = time.monotonic()`.
    - Fresh-spawn success branch: after `self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)` (line 403), before `return` (line 404) — set `self._started_at = time.monotonic()`.
  - New methods, inserted after `_start_locked()` (after line 408), before `def stop(self):` (line 410): `is_healthy() -> tuple[bool, str]`, `uptime_seconds` (property), `recycle(reason: str) -> None`.
- `config.py`: new `browser_max_uptime_hours` field, inserted after `keepalive_disabled_accounts` (currently lines 166-167), before the `data_dir` property (line 169).
- `app.py`
  - New function `_browser_health_check_sync() -> dict`, inserted after `_warmup_browser_sync()` (currently ending line 3138), before `async def _keepalive_loop():` (line 3141).
  - `_keepalive_loop()` (currently lines 3141-3166): inside the `while True:` loop (line 3154), add a new `try/except` calling `await asyncio.to_thread(_browser_health_check_sync)` immediately before the existing `try: await run_keepalive_all(force=False)` block (line 3155-3158).
- `tests/test_browser_health.py` (new file).

### Phase 3 (optional)

- `cdp.py`: delete `CDP.pump()` (currently lines 53-63), or (alternative) wrap its body in `with self._lock:` like `send()` (lines 25-26) already does.

## Public Contracts

Everything new introduced by this plan, and the compatibility constraints each one must satisfy:

- **New env var `MUSE2API_CHAT_QUEUE_MAX`** (config.py `chat_queue_max`, default `16`). Follows the exact pattern of `MUSE2API_IMAGE_QUEUE_MAX`/`MUSE2API_VIDEO_QUEUE_MAX` (`max(1, int(_env(...)))`). Must be documented in README the same way those two are (check README/`.env.example` for existing `MUSE2API_IMAGE_QUEUE_MAX`/`MUSE2API_VIDEO_QUEUE_MAX` entries and add a matching line — execute-agent should grep for `MUSE2API_IMAGE_QUEUE_MAX` in `README.md` and `.env.example` and mirror the entry).
- **New 429 response shape on `/v1/chat/completions` and `/v1/responses`**: must be byte-for-byte consistent with the existing image/video 429 (`HTTPException(429, f"{kind.capitalize()} queue is full ({limit} active); retry later")`), run through the existing `_http_exc` handler (app.py lines 283-294) which wraps it as `{"error": {"message": ..., "type": "rate_limit_error", "param": null, "code": 429}}` for any `/v1/*` path. Use message text `f"Chat queue is full ({CFG.chat_queue_max} active); retry later"` for parity. This is a new rejection path for existing public endpoints — it only activates at sustained high concurrency (above the default of 16 simultaneous chat/responses requests), so it is additive and should not change behavior for any existing single-digit-concurrency client.
- **New env var `MUSE2API_BROWSER_MAX_UPTIME_HOURS`** (config.py `browser_max_uptime_hours`, default `0` = disabled). Opt-in only; default behavior adds crash/unresponsive detection but no age-based recycling, so no existing deployment's browser lifecycle changes unless the admin explicitly sets this.
- **New `MuseEngine` methods** (`is_healthy`, `uptime_seconds`, `recycle`): internal API, not exposed over HTTP. Any future caller must respect the same GEN_LOCK-gating contract documented in `_browser_health_check_sync`'s docstring — `recycle()` must never be called without the caller already holding `GEN_LOCK` (non-blocking acquire, so it can also legitimately fail to acquire and skip).
- **No change** to any existing public contract: `_admit_task`, `/v1/images/generations`, `/v1/videos`, their 429 shape, `ACCOUNT_POOL`/`GEN_LOCK` lock ordering (`ACCOUNT_POOL` before `GEN_LOCK`, everywhere, unchanged), or `TASK_ADMISSION_LOCK`'s existing use for image/video idempotency.

## Blast Radius

What existing behavior/tests could be affected, and why they are expected to stay green:

- **`tests/test_admission.py`** — asserts `source.count("_admit_task(") >= 2` and scans specifically around `create_video`/`get_video` for `_admit_task("video"` and `TASK_ADMISSION_LOCK`. This plan adds `_admit_chat()`/`_release_chat()` as separate, differently-named functions and does not touch `_admit_task`, the video/image endpoints, or `TASK_ADMISSION_LOCK`'s existing call sites. **Expected: unaffected, no edits needed.**
- **`tests/test_lock_order.py`** — asserts the account lease is always acquired before `GEN_LOCK` (`ACCOUNT_POOL.acquire` must never be called while `app.GEN_LOCK.locked()`), and that no admin path contains the literal substring `"with GEN_LOCK, _account_operation"`. Phase 1's admission check (`_admit_chat`) runs before any `ACCOUNT_POOL`/`GEN_LOCK` interaction (it only touches `TASK_ADMISSION_LOCK` and an integer counter) and `_release_chat()` likewise never touches those locks — so lock ordering is untouched. Phase 2's `_browser_health_check_sync` only ever takes `GEN_LOCK` alone (never combined with `ACCOUNT_POOL`), matching the existing `_warmup_browser_sync` pattern at app.py lines 3111-3138 which already does exactly this (`GEN_LOCK.acquire(blocking=False)` with no account lease). **Expected: unaffected, no edits needed.**
- **`tests/test_stream_backpressure.py`** — drives `app.safe_chat_stream(...)` **directly**, not through `chat_completions`/`responses_api`. The new admission gate lives in the endpoint functions, not in `safe_chat_stream`, so this test exercises GEN_LOCK-release-on-disconnect behavior exactly as before, untouched by this plan. **Expected: unaffected, no edits needed.** (Noted as a coverage gap closed by the new `tests/test_chat_admission.py`, which must exercise the endpoint-level wiring that this file does not.)
- **`tests/test_concurrency.py`** — exercises `ConcurrencyLock`, `AccountPoolManager`, and a simulated multi-account `_run_generation` scenario; does not touch chat endpoints or the keepalive loop. **Expected: unaffected.**
- **`tests/test_keepalive_scheduling.py`** — calls `app.run_keepalive_all()` directly, not `app._keepalive_loop()`. Phase 2's new health check is wired into `_keepalive_loop`'s `while True:` body, not into `run_keepalive_all` itself, so this test's monkeypatched `app.store`/`app.CFG`/`_probe_account_sync` flow is untouched. **Expected: unaffected, no edits needed.**
- **`tests/test_responses_api.py`** — calls `app.responses_api(req, None)` directly with `app.safe_chat_stream` monkeypatched and a `no_engine` fixture that replaces `app.engine` with a bare stub exposing only `current_acc_id`. Adding `_admit_chat()`/`_release_chat()` calls inside `responses_api` does **not** touch `app.engine` at all (admission is purely `TASK_ADMISSION_LOCK` + an int counter), so these existing tests keep working unmodified as long as `app._CHAT_ACTIVE` starts at 0 and `app.CFG.chat_queue_max >= 1` (true by default). **Risk**: if `test_responses_api.py`'s existing tests run many times in the same pytest session without resetting `app._CHAT_ACTIVE`, a leaked increment from a failed earlier test could falsely trip the cap for later tests. Mitigate by making `_release_chat()` exception-safe in `finally` for every code path (this plan's design already does this) and, in the new test file, explicitly resetting `app._CHAT_ACTIVE = 0` in a fixture `teardown`/`autouse` fixture scoped to that file only — do not rely on cross-file ordering.
- **Dockerfile / docker-compose.yml** — not touched by Phases 1-2-3. Item 5 (tini) deliberately deferred; no edit.
- **`tests/test_admin_i18n.py`** — not touched; item 4 deliberately deferred; no edit.
- **README.md / .env.example** — Phase 1 and Phase 2 each add one new env var; README/`.env.example` should gain one documentation line each (see [Public Contracts](#public-contracts)), mirroring the existing `MUSE2API_IMAGE_QUEUE_MAX` documentation pattern. This is additive documentation, not a behavior change.

## Phase 1: Chat/Responses Admission Cap

### Design decisions

- Chat/responses have **no persisted task row** (unlike image/video, which write to `store.tasks`), so `_admit_task(kind, limit)` — which counts `store.tasks` entries by `kind`/`status` — cannot be reused as-is. Reusing its name/signature would also break `tests/test_admission.py`'s `source.count("_admit_task(") >= 2` assumption if abused incorrectly; this plan deliberately uses **new, separately named** functions `_admit_chat()` / `_release_chat()` instead of overloading `_admit_task`.
- Admission is a simple in-memory counter (`_CHAT_ACTIVE`), incremented on admit and decremented on release, guarded by the **existing** `TASK_ADMISSION_LOCK` (no new lock — this mutex is already shared between image/video admission and idempotency bookkeeping; reusing it here is consistent and avoids a second global lock).
- One admission slot is held for the **entire request duration**, including the full SSE stream for streaming requests — not just until the worker thread spawns. This matches the spirit of "how many chat/responses requests may be queued or processing at once" and bounds real concurrent thread/account/queue usage, not just admission-instant throughput.
- Release **must** happen exactly once per admitted request, on every exit path: normal completion, any exception, and — for the streaming generator — `GeneratorExit` raised when the client disconnects and Starlette calls `.close()` on the generator mid-stream. A plain `try/except/finally` with the release in `finally` satisfies all three, because Python's `finally` runs on `GeneratorExit` too (it is only *not* caught by `except Exception`, which is correct — it must not be caught, just not block the `finally` from running).
- No change to `ACCOUNT_POOL`/`GEN_LOCK` lock ordering: `_admit_chat()` is called **before** any account/browser-lock code in the endpoint, and never while holding `TASK_ADMISSION_LOCK` across a blocking operation (it acquires, checks, increments, releases immediately — same pattern as `_admit_task`).

### `config.py` changes

Add, immediately after the existing `video_queue_max` field (after line 132), before the `task_retention` field:

```
    # Chat/responses have no persisted task row like image/video do, so this bounds
    # in-flight /v1/chat/completions + /v1/responses requests directly (counted for the
    # full request duration, including streaming) rather than a store.tasks scan.
    # Each request is lighter than an image/video generation (no file I/O, no task
    # polling) but still holds one account lease and a worker thread for up to
    # MUSE2API_CHAT_TIMEOUT, so a higher default than image/video is reasonable.
    chat_queue_max: int = field(
        default_factory=lambda: max(1, int(_env("MUSE2API_CHAT_QUEUE_MAX", "16"))))
```

### `app.py` changes

1. Right after line 222 (`TASK_ADMISSION_LOCK = IMAGE_TASK_LOCK`), add:

```python
# Chat/responses have no persisted task row; count in-flight requests directly.
# Guarded by TASK_ADMISSION_LOCK, same mutex image/video admission already shares.
_CHAT_ACTIVE = 0
```

2. Right after `_admit_task()` (after line 1626), before `_reject_multiple_images()`, add:

```python
def _admit_chat() -> None:
    """Admission cap for /v1/chat/completions and /v1/responses.

    Chat has no store.tasks row to scan (unlike _admit_task), so this counts in-flight
    requests directly via a module-level counter. Call exactly once per request, right
    after validating the prompt/input is non-empty and before any account/browser work.
    Must be paired with exactly one _release_chat() call in a `finally` block so the
    slot is always freed -- on success, on any exception, and on client disconnect
    (GeneratorExit during streaming).
    """
    global _CHAT_ACTIVE
    with TASK_ADMISSION_LOCK:
        if _CHAT_ACTIVE >= CFG.chat_queue_max:
            raise HTTPException(
                429, f"Chat queue is full ({CFG.chat_queue_max} active); retry later")
        _CHAT_ACTIVE += 1


def _release_chat() -> None:
    global _CHAT_ACTIVE
    with TASK_ADMISSION_LOCK:
        _CHAT_ACTIVE = max(0, _CHAT_ACTIVE - 1)
```

3. In `chat_completions()`:
   - After the existing `if not prompt: raise HTTPException(400, "messages is empty")` (line 2083), insert `_admit_chat()` as its own statement.
   - In the streaming branch, wrap `sync_stream()`'s body: change the existing `try: ... except MuseAuthError ...: ... except MuseGenerationError ...: ... except Exception as exc: ...` into `try: ... except ...: ... finally: _release_chat()` (add the `finally` clause; keep all three existing `except` blocks exactly as they are).
   - In the non-streaming branch, change the existing `try: text = await asyncio.to_thread(run) except MuseAuthError as exc: raise HTTPException(401, str(exc)) except MuseGenerationError as exc: raise HTTPException(502, str(exc))` into the same shape plus `finally: _release_chat()`.

4. In `responses_api()`: apply the identical pattern — `_admit_chat()` right after `if not prompt: raise HTTPException(400, "input is empty")` (line 2262); `finally: _release_chat()` added to the streaming `sync_stream()`'s try/except (currently ending with `except Exception as exc: ... yield failed event` at line 2388) and to the non-streaming `try/except MuseAuthError/except MuseGenerationError` block (lines 2395-2400).

### New tests — `tests/test_chat_admission.py`

Mirror the existing style from `tests/test_admission.py` (unit tests on the admission primitive) and `tests/test_responses_api.py` (direct-call endpoint tests with a `no_engine`-style fixture and `monkeypatch.setattr(app, "safe_chat_stream", ...)`). Required cases:

1. **Unit tests on the primitive** (mirror `test_admission.py`):
   - `test_admit_chat_blocks_at_cap` — monkeypatch `app.CFG.chat_queue_max = 2` (and reset `app._CHAT_ACTIVE = 0` in setup), call `app._admit_chat()` twice successfully, a third call raises `app.HTTPException` with `status_code == 429` and `"Chat queue is full"` in `exc.value.detail`.
   - `test_release_chat_frees_a_slot` — admit to the cap, release one via `app._release_chat()`, confirm a subsequent `app._admit_chat()` succeeds.
   - `test_release_chat_never_goes_negative` — call `app._release_chat()` with no prior admit (several times); confirm `app._CHAT_ACTIVE == 0` afterward, never negative.
   - `test_chat_queue_max_is_configurable` — `assert app.CFG.chat_queue_max >= 1` (mirrors `test_caps_are_configurable`).

2. **Endpoint-wiring tests** (mirror `test_responses_api.py`'s direct-call pattern, needed because `test_stream_backpressure.py` calls `safe_chat_stream` directly and never exercises this wiring):
   - `test_chat_completions_releases_slot_after_success` — `no_engine`-style fixture, monkeypatch `app.safe_chat_stream` to return a short fixed generator, call `asyncio.run(app.chat_completions(req, True))` with a non-streaming `ChatRequest`, assert `app._CHAT_ACTIVE == 0` afterward.
   - `test_chat_completions_releases_slot_after_streaming` — same but `stream=True`, collect the `StreamingResponse.body_iterator` fully (same `_collect` pattern as `test_responses_api.py` lines 70-73), assert `app._CHAT_ACTIVE == 0` afterward.
   - `test_chat_completions_releases_slot_on_early_stream_close` — start iterating the streaming generator, stop after a few chunks and call `.close()` on the generator/response body iterator (mirroring `test_stream_backpressure.py`'s `stream.close()` pattern) without draining it fully; assert `app._CHAT_ACTIVE == 0` afterward (this is the `GeneratorExit` path).
   - `test_responses_api_releases_slot_after_success` / `test_responses_api_releases_slot_after_streaming` — same two cases for `app.responses_api`.
   - `test_chat_completions_429_when_cap_reached` — monkeypatch `app.CFG.chat_queue_max = 1` and pre-set `app._CHAT_ACTIVE = 1`; call `app.chat_completions(req, True)`; assert it raises `app.HTTPException` with `status_code == 429` before ever touching `safe_chat_stream` (monkeypatch it to raise `AssertionError("should not be called")` to prove this).
   - Add an `autouse` fixture in this file that resets `app._CHAT_ACTIVE = 0` before and after every test, so a leaked counter from one test (or an unrelated test file running in the same session) can never falsely trip the cap for another test.

### Test Procedure (manual)

1. Start the server locally with `MUSE2API_CHAT_QUEUE_MAX=1`.
2. Send one slow chat request (e.g., a long `stream=true` request) in the background.
3. While it is in flight, send a second `/v1/chat/completions` request; confirm it returns HTTP 429 with `{"error": {"message": "Chat queue is full (1 active); retry later", "type": "rate_limit_error", ...}}`.
4. After the first request completes, confirm a new request succeeds immediately (no lingering 429).

### Verification Queries

- `curl -s -o /dev/null -w "%{http_code}\n" <second request>` returns `429`.
- Server log shows no unhandled exception during either request.

### Done Criteria

- All new tests in `tests/test_chat_admission.py` pass.
- `tests/test_admission.py`, `tests/test_lock_order.py`, `tests/test_stream_backpressure.py`, `tests/test_responses_api.py`, `tests/test_concurrency.py` all still pass unmodified.
- Manual test above confirms the 429 contract end-to-end.

## Phase 2: Chromium Health / Restart Policy

### Design decisions

- **Mandatory scope**: crash detection (Chromium process exited) and unresponsive detection (process still running but the CDP `/json/version` HTTP endpoint doesn't answer) — both proactive, run on the existing keepalive loop's cadence (every ~15 minutes via `_keepalive_loop`'s `while True:` body, app.py lines 3141-3166), not only reactively on the next request.
- **Optional extra trigger**: a configurable max-uptime threshold (`MUSE2API_BROWSER_MAX_UPTIME_HOURS`, default `0` = disabled) for admins who want scheduled recycling regardless of health. Kept to a single timestamp comparison — no new background timer, no new lock.
- **Explicitly NOT implemented in this plan**: max-request-count-based recycling. It would require plumbing a counter increment through every `engine.start()`/`engine.generate()`/`engine.chat_stream()` call site across `_run_generation_locked`, `safe_chat_stream`'s worker, `_warmup_browser_sync`, and the admin quota/probe paths — a materially larger diff for a trigger that uptime-based recycling already covers approximately as well for this single-browser-process deployment model. Noted here as a possible future enhancement, not pursued now (see [Deferred / Out of Scope](#deferred--out-of-scope) framing — this specific sub-item is a scope decision within Phase 2, not a top-level deferred item).
- **Coordination with `GEN_LOCK`**: every existing `engine.start()` call site in this codebase already holds `GEN_LOCK` before calling it (confirmed by reading all 9 call sites: app.py lines 602, 1487, 2598, 2665, 2692, 2989, 3127 are all inside a `with GEN_LOCK:`/`GEN_LOCK.acquire()` block; the only two call sites that do **not** hold it — `engine.stop()` at line 3488 inside `/admin/update/upgrade`'s delayed-restart thread, and line 3557 in the `@app.on_event("shutdown")` handler — are both immediately followed by process termination, so no concurrent generation can observe a half-torn-down engine). Therefore: a non-blocking `GEN_LOCK.acquire()` that **succeeds** in the health-check thread proves no generation is currently inside its CDP-using critical section, making it safe to call `engine.stop()`/`engine.recycle()`. If the non-blocking acquire **fails**, a generation is in flight; skip this cycle and retry on the next tick.
- **No new interaction with `ACCOUNT_POOL`** is needed: recycling only tears down the browser process/CDP connection, not account leases. An account can remain "busy" (leased) during a recycle if that thread happens to be between `ACCOUNT_POOL.acquire()` and its own `GEN_LOCK` acquisition (e.g., mid HTTP cookie-renewal) — this is safe because that thread hasn't touched the browser yet, and will call `engine.start()` fresh after the recycle completes and releases `GEN_LOCK`.
- **Existing precedent for calling `engine.stop()` off the main request thread**: `/admin/update/upgrade` (app.py line 3488) already does this with **no** `GEN_LOCK` coordination at all (safe there only because `os._exit(0)` follows immediately). This plan's `GEN_LOCK`-gated approach is strictly safer than that existing precedent, not a new risk class for the codebase.
- `is_healthy()` reads `self.proc`/`self.browser` without taking `self._start_lock`, by design — it's a cheap, tolerant-of-staleness probe (worst case: one extra skipped/retried cycle), not a mutation. All actual mutation (`recycle()` → `stop()`) is already internally guarded by `self._start_lock` inside `stop()` itself (existing code, unchanged).

### `engine.py` changes

1. In `__init__` (after line 311, before line 312):

```python
self._started_at: float = 0.0  # monotonic time of last successful CDP connect
```

2. In `_start_locked()`, set `self._started_at = time.monotonic()` immediately after each of the two successful-connection points (after line 380 in the reuse-existing-endpoint branch; after line 403 in the fresh-spawn branch) — before their respective `return` statements.

3. New methods, inserted after `_start_locked()` ends (after line 408), before `def stop(self):`:

```python
def is_healthy(self) -> tuple[bool, str]:
    """Liveness probe for the background health check (app.py's keepalive loop).

    Read-only; does not take self._start_lock, so it can run concurrently with a
    legitimate start/stop without blocking either side. A stale read just means this
    cycle's verdict is skipped or retried next tick -- never a crash, because every
    mutation of self.proc/self.browser happens under GEN_LOCK held by the caller
    (see _start_locked's call sites in app.py), except the process-exiting restart
    path in /admin/update/upgrade, which calls os._exit(0) right after.
    """
    if not self.proc or not self.browser:
        return False, "not started"
    code = self.proc.poll()
    if code is not None:
        return False, f"process exited (code {code})"
    try:
        http_json(self._debug_url(), timeout=3)
    except Exception as exc:  # noqa: BLE001
        return False, f"CDP endpoint unresponsive: {exc}"
    return True, "ok"

@property
def uptime_seconds(self) -> float:
    return time.monotonic() - self._started_at if self._started_at else 0.0

def recycle(self, reason: str) -> None:
    """Forcefully tear down the shared Chromium so the next start() spawns fresh.

    Caller MUST already hold GEN_LOCK via a non-blocking acquire -- see app.py's
    _browser_health_check_sync, the only intended caller. Reuses stop() entirely;
    no new teardown logic is added here.
    """
    log.warning("[Browser health] Recycling Chromium: %s", reason)
    self.stop()
```

### `config.py` changes

Add, immediately after `keepalive_disabled_accounts` (after line 167), before the `data_dir` property:

```python
    # Background Chromium health check always runs (piggybacked on the keepalive
    # loop's ~15-minute tick) and recycles a crashed or CDP-unresponsive browser.
    # This field adds an *optional* extra trigger: proactively recycle on uptime
    # alone, even if currently healthy. 0 (default) disables it -- only genuinely
    # unhealthy browsers get recycled out of the box.
    browser_max_uptime_hours: float = field(
        default_factory=lambda: max(0.0, float(_env("MUSE2API_BROWSER_MAX_UPTIME_HOURS", "0"))))
```

### `app.py` changes

1. New function, inserted after `_warmup_browser_sync()` ends (after line 3138), before `async def _keepalive_loop():`:

```python
def _browser_health_check_sync() -> dict:
    """Detect a crashed or unresponsive shared Chromium and proactively recycle it.

    Runs from the background keepalive loop, not from a request thread. Only acts when
    GEN_LOCK is free (non-blocking acquire): every engine.start()/stop() call elsewhere
    in this module already runs under GEN_LOCK held by that caller, so a successful
    non-blocking acquire here proves no generation is in flight. If GEN_LOCK is held,
    this cycle is skipped and retried on the next tick.
    """
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

2. In `_keepalive_loop()`'s `while True:` body (line 3154), add a new step immediately before the existing `try: await run_keepalive_all(force=False) except Exception as e: ...` block (lines 3155-3158):

```python
        try:
            await asyncio.to_thread(_browser_health_check_sync)
        except Exception as e:  # noqa: BLE001
            log.warning("[Browser health] Check failed: %s", e)
```

### New tests — `tests/test_browser_health.py`

1. **`engine.py`-level unit tests** (construct a real `MuseEngine(app.CFG)` or an equivalent fake with `.proc`/`.browser`/`._started_at` attributes set directly — avoid spawning a real Chromium):
   - `test_is_healthy_false_when_not_started` — fresh engine (`proc=None`) → `is_healthy() == (False, "not started")`.
   - `test_is_healthy_false_when_process_exited` — fake `proc` object with `.poll()` returning `1`, `browser` set to any truthy stub → `(False, "process exited (code 1)")`.
   - `test_is_healthy_false_when_cdp_ping_fails` — `proc.poll()` returns `None`, `browser` set; monkeypatch the `engine` **module's** `http_json` symbol (`import engine as engine_module; monkeypatch.setattr(engine_module, "http_json", raiser)` — note this is the module, distinct from `app.engine`, the singleton instance) to raise → `(False, "CDP endpoint unresponsive: ...")`.
   - `test_is_healthy_true_when_ping_succeeds` — same setup, monkeypatch `http_json` to return a dict → `(True, "ok")`.
   - `test_uptime_seconds_zero_before_start` — fresh engine, `_started_at == 0.0` → `uptime_seconds == 0.0`.
   - `test_recycle_calls_stop` — monkeypatch the engine instance's `.stop` with a call recorder, call `.recycle("reason")`, assert `stop` was called exactly once.

2. **`app.py`-level wiring tests** (use a lightweight stub object for `app.engine`, mirroring `test_stream_backpressure.py`'s `FakeEngine` style — expose only `.is_healthy()`, `.uptime_seconds`, `.recycle()`):
   - `test_health_check_noop_when_healthy_and_uptime_disabled` — stub `is_healthy` → `(True, "ok")`, `app.CFG.browser_max_uptime_hours = 0`; call `app._browser_health_check_sync()`; assert `{"recycled": False}` and the stub's `recycle` was never called.
   - `test_health_check_recycles_when_gen_lock_free` — stub `is_healthy` → `(False, "boom")`; ensure `app.GEN_LOCK` is unlocked; call `app._browser_health_check_sync()`; assert the stub's `recycle` was called once with a reason containing `"boom"`, the result is `{"recycled": True, "reason": ...}`, and `app.GEN_LOCK.locked() is False` afterward (released, not leaked).
   - `test_health_check_defers_when_gen_lock_held` — stub `is_healthy` → `(False, "boom")`; acquire `app.GEN_LOCK` in the test itself first; call `app._browser_health_check_sync()`; assert result is `{"recycled": False, "deferred": True, "reason": "unhealthy: boom"}` and the stub's `recycle` was never called; release `app.GEN_LOCK` at test end (in a `finally`/fixture teardown) so later tests are unaffected.
   - `test_max_uptime_trigger` — stub `is_healthy` → `(True, "ok")`; set `app.CFG.browser_max_uptime_hours` to a tiny value and the stub's `uptime_seconds` to a large value; `app.GEN_LOCK` unlocked; call `app._browser_health_check_sync()`; assert `recycle` called with a reason containing `"max uptime exceeded"`.

### Test Procedure (manual)

1. Start the server; confirm the Chromium process PID via `ps` or the admin page.
2. Kill the Chromium process directly (`kill -9 <pid>`) without restarting the server.
3. Wait for (or manually trigger, if an admin endpoint exists — otherwise wait up to 15 minutes, or shorten `_keepalive_loop`'s sleep for this one manual test only) the next keepalive tick.
4. Confirm the server log shows `[Browser health] Recycling Chromium: unhealthy: process exited (code ...)`.
5. Confirm a subsequent `/v1/chat/completions` or `/v1/images/generations` request succeeds against a freshly spawned Chromium (new PID).
6. Separately: start a slow generation, and while it is in flight, manually invoke `_browser_health_check_sync()` (or simulate via the new unit tests) with the engine forced unhealthy; confirm it defers (does not kill the browser) because `GEN_LOCK` is held by the in-flight generation, and that the generation completes successfully.

### Verification Queries

- Log line `[Browser health] Recycling Chromium: ...` appears after step 2-3 above.
- `engine.proc.pid` differs before and after the recycle.
- No `MuseGenerationError`/500 is raised for the in-flight generation in step 6.

### Done Criteria

- All new tests in `tests/test_browser_health.py` pass.
- `tests/test_keepalive_scheduling.py`, `tests/test_concurrency.py`, `tests/test_lock_order.py` all still pass unmodified.
- Manual test above confirms both the crash-recycle path and the in-flight-deferral path.

## Phase 3 (Optional/Stretch): `cdp.py` pump() dead code

`CDP.pump()` (cdp.py lines 53-63) reads from the same websocket as `send()` (lines 25-42) but, unlike `send()`, does not acquire `self._lock` first. Verified at plan time: zero call sites repo-wide (`grep -rn "\.pump(" .` under the project root returns nothing outside `cdp.py`'s own definition).

**Default recommendation: delete it.** It is unused, so removing it eliminates the race entirely with a one-line diff and no behavior change.

**Alternative, if the execute-agent prefers to keep it for future use**: wrap its body in `with self._lock:` exactly as `send()` does (lines 26-42), e.g.:

```python
def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
    """Read events for `seconds` seconds and dispatch them to the callback."""
    end = time.time() + seconds
    with self._lock:
        self.ws.settimeout(sock_timeout)
        while time.time() < end:
            try:
                ev = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                continue
            if on_event and "method" in ev:
                on_event(ev)
```

Note this alternative would serialize `pump()` for its *entire* duration against any concurrent `send()` call, which is a behavior change from today's (already broken) concurrent-read semantics — acceptable only because nothing currently calls `pump()`, so there is no real-world caller to regress.

This phase requires no new test if deletion is chosen (removing confirmed-unused code). If the lock alternative is chosen instead, add one test in a new or existing `cdp`-focused test file asserting `self._lock.locked()` is true for the duration of a `pump()` call (e.g., via a slow fake `self.ws.recv` and a background thread probing `self._lock.locked()`).

## Deferred / Out of Scope

### Item 4: broader i18n drift coverage (deferred, not implemented in this plan)

`tests/test_admin_i18n.py` only checks that every `data-i18n`-tagged element in `admin.html` has a matching English block; it does not scan for Chinese literals embedded directly in `app.py`'s error/status strings, or untagged Chinese text in `admin.html`. A broader check would need to: (a) scan `app.py` string literals for CJK character ranges and flag ones that reach an HTTP response or log visible to an English-mode admin, and (b) scan `admin.html` for CJK text outside `data-i18n` attributes and outside the `I18N_EN_HTML`/`I18N_EN_JS`-style translation tables. This is explicitly **not implemented** in this plan: it is a test-authoring effort orthogonal to the stability/performance goals here, and bundling it would blur this plan's verification story. Recommended as a separate, future small plan if pursued.

### Item 5: no `tini`/init process in Docker (deferred, investigate-only)

Neither `Dockerfile` nor `docker-compose.yml` currently runs an init process (`tini` or equivalent) as PID 1, so orphaned Chromium child processes would not be reaped if the main Chromium process crashes abnormally while the container's main process (presumably the Python server) keeps running. Impact is **unverified** — this requires runtime observation (e.g., checking for zombie/orphaned processes in a long-running deployment) rather than a speculative code change. **Explicitly deferred**: recommend monitoring actual container process trees in production before deciding whether to add `tini`, rather than adding dependency/entrypoint complexity against an unconfirmed problem. If Phase 2's health-check (this plan) already recycles a crashed Chromium proactively, the practical urgency of this item is further reduced, which is an additional reason to defer rather than bundle it here.

## Implementation Checklist

1. Re-verify current line numbers for all Touchpoints above (code may have drifted since 08-11-26) before editing.
2. `config.py`: add `chat_queue_max` field (after `video_queue_max`).
3. `app.py`: add `_CHAT_ACTIVE = 0` global (after `TASK_ADMISSION_LOCK`).
4. `app.py`: add `_admit_chat()` / `_release_chat()` helpers (after `_admit_task`).
5. `app.py`: wire `_admit_chat()` + `finally: _release_chat()` into `chat_completions()` (both streaming and non-streaming branches).
6. `app.py`: wire `_admit_chat()` + `finally: _release_chat()` into `responses_api()` (both streaming and non-streaming branches).
7. Add `tests/test_chat_admission.py` covering the primitive and endpoint-wiring cases listed under Phase 1.
8. Run `pytest tests/test_chat_admission.py tests/test_admission.py tests/test_lock_order.py tests/test_stream_backpressure.py tests/test_responses_api.py tests/test_concurrency.py -q` — all green.
9. Manual test: `MUSE2API_CHAT_QUEUE_MAX=1`, confirm the 429 contract (Phase 1 Test Procedure).
10. `config.py`: add `browser_max_uptime_hours` field (after `keepalive_disabled_accounts`).
11. `engine.py`: add `self._started_at` init, set it at both success points in `_start_locked()`.
12. `engine.py`: add `is_healthy()`, `uptime_seconds`, `recycle()` to `MuseEngine`.
13. `app.py`: add `_browser_health_check_sync()` (after `_warmup_browser_sync`).
14. `app.py`: wire `_browser_health_check_sync` into `_keepalive_loop()`'s `while True:` body.
15. Add `tests/test_browser_health.py` covering the engine-level and app-level cases listed under Phase 2.
16. Run `pytest tests/test_browser_health.py tests/test_keepalive_scheduling.py tests/test_concurrency.py tests/test_lock_order.py -q` — all green.
17. Manual test: kill the real Chromium process, confirm recycle on next tick, confirm in-flight deferral (Phase 2 Test Procedure).
18. Document `MUSE2API_CHAT_QUEUE_MAX` and `MUSE2API_BROWSER_MAX_UPTIME_HOURS` in `README.md` and `.env.example`, mirroring the existing `MUSE2API_IMAGE_QUEUE_MAX` entries.
19. (Optional/stretch, only after 1-18 are done and green) `cdp.py`: delete `pump()` (default) or lock it (alternative) per Phase 3.
20. Run the full test suite (`pytest -q`) once more to confirm no regressions anywhere.

## Acceptance Criteria

1. `/v1/chat/completions` and `/v1/responses` return HTTP 429 with the OpenAI-shaped error body once `CFG.chat_queue_max` concurrent requests are already admitted, for both streaming and non-streaming requests.
2. The admission counter (`app._CHAT_ACTIVE`) returns to exactly the pre-request value after every completed, errored, or client-disconnected chat/responses request — verified by the new tests and by not leaking across the existing `test_responses_api.py`/`test_stream_backpressure.py` tests.
3. `MUSE2API_CHAT_QUEUE_MAX` is configurable via env var, follows the existing `MUSE2API_*_QUEUE_MAX` pattern, and defaults to `16`.
4. `ACCOUNT_POOL`-before-`GEN_LOCK` lock ordering is unchanged; `tests/test_lock_order.py` passes unmodified.
5. A Chromium process that exits or stops answering CDP pings is detected and recycled within one keepalive tick, without any code change to `tests/test_keepalive_scheduling.py`'s existing behavior.
6. A recycle attempt that would race an in-flight generation is deferred (not forced), proven by `test_health_check_defers_when_gen_lock_held` and the Phase 2 manual test.
7. `MUSE2API_BROWSER_MAX_UPTIME_HOURS` defaults to `0` (disabled) — no existing deployment's browser lifecycle changes unless explicitly opted in.
8. All existing tests listed in [Blast Radius](#blast-radius) pass unmodified; all new tests listed in Phase 1 and Phase 2 pass.
9. `cdp.py`'s `pump()`/`send()` race is resolved (deleted or locked) if Phase 3 is attempted; otherwise explicitly left for a future pass with no behavior change.
10. Items 4 and 5 remain untouched in this pass, with their deferral rationale recorded in this plan (done above).

## Verification Evidence

Required evidence before this plan can be marked VERIFIED (not just CODE DONE):

- **Automated**: full output of `pytest -q` (or at minimum the targeted subsets listed in Implementation Checklist steps 8 and 16) showing all pass, pasted or summarized in the EXECUTE-phase closeout.
- **New-behavior proof**: the new test files (`tests/test_chat_admission.py`, `tests/test_browser_health.py`) pass, demonstrating the admission cap and the health/recycle logic actually work, not just that nothing else broke.
- **Manual evidence for Phase 1**: a captured 429 response body from the manual Test Procedure (curl output or server log line).
- **Manual evidence for Phase 2**: a server log line showing `[Browser health] Recycling Chromium: ...` after a forced Chromium kill, plus confirmation that an in-flight generation survived a simulated recycle-deferral.
- **No regression evidence**: explicit confirmation that `tests/test_admission.py`, `tests/test_lock_order.py`, `tests/test_stream_backpressure.py`, `tests/test_concurrency.py`, `tests/test_keepalive_scheduling.py`, `tests/test_responses_api.py` all still pass without modification (their content should be byte-identical before/after this plan's execution — if any of them needed an edit, that is a signal this plan's design assumptions were wrong and must be revisited before claiming done).
- This repo has no `process/context/tests/all-tests.md` router; the test commands above (`pytest -q`, `pytest <files> -q`) are read directly from `pytest.ini` (`testpaths = tests`, `asyncio_mode = auto`) and `requirements-dev.txt` (pins `pytest-asyncio>=0.24,<1`).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| A bug in `finally: _release_chat()` placement leaks the counter, eventually wedging all chat/responses behind a false 429 | New tests explicitly cover success, error, and disconnect paths for both endpoints; counter is a simple int with a `max(0, ...)` floor so even an extra erroneous release can't go negative and mask a real leak as "fine" |
| `is_healthy()`'s unguarded read races a concurrent legitimate `_start_locked()` and produces a false "unhealthy" verdict | Documented as tolerable: the non-blocking `GEN_LOCK` acquire that follows will fail while a real start (which always holds `GEN_LOCK`) is in progress, so the health check safely defers rather than acting on a stale read |
| Recycling the browser while some other, non-generation code path still references a stale `engine.browser`/`engine.page` handle | Only possible call sites for mutating `engine.proc`/`engine.browser` either hold `GEN_LOCK` already (confirmed by reading all 9 call sites) or immediately `os._exit(0)` afterward; the health check's own `GEN_LOCK`-gated call is therefore safe by the same invariant the rest of the codebase already relies on |
| New env vars silently do nothing if not documented | Checklist step 18 requires README/`.env.example` updates before calling this plan done |
| Phase 3's dead-code deletion turns out to have an undiscovered caller | Already verified zero call sites repo-wide at plan time via `grep -rn "\.pump(" .`; execute-agent should re-run this grep immediately before deleting, as a final confirmation |

## Resume and Execution Handoff

If this plan is picked up cold (new session, compacted context, or a different agent):

1. **Read this file in full first** — it is the single source of truth; no companion phase files exist (this is a standard complex plan, not a phase program).
2. **Re-verify line numbers** in [Touchpoints](#touchpoints) against the current state of `app.py`, `engine.py`, `config.py`, `cdp.py` before editing anything — this plan was written against the repo state as of 08-11-26 and line numbers may have drifted.
3. **Order of work**: Phase 1 and Phase 2 are independent and can be done in either order; Phase 3 should only be attempted after both are code-complete and tested (it's optional and lowest priority). Do not attempt items 4 or 5 under this plan — they are explicitly deferred.
4. **Key invariant to never break**: `ACCOUNT_POOL` is always acquired before `GEN_LOCK`, everywhere. Neither phase in this plan changes that ordering; if a future edit seems to require changing it, stop and re-plan rather than deviating silently.
5. **Key invariant for Phase 2**: `engine.recycle()` (and therefore `engine.stop()`) must never be called without the caller already holding `GEN_LOCK` via a non-blocking acquire that it personally performed and will personally release. Do not call `engine.recycle()` from any new code path without this guard.
6. **Test commands to run before declaring done**: see [Verification Evidence](#verification-evidence) above; there is no CI config discovered in this repo pass beyond `pytest.ini`/`requirements-dev.txt`, so local `pytest -q` is the authoritative gate.
7. **No existing plan files conflict with this one** — `process/general-plans/active/` was empty at plan creation time (verified via `ls`).
8. **This plan file itself is the execute-anchor** — not a legacy `PLAN.md`/`phase-*.md` shape; it is a single direct `*_PLAN_*.md` artifact.

## Cursor + RIPER-5 Guidance

- **Cursor Plan mode**: import the [Implementation Checklist](#implementation-checklist) directly; work through items 1-20 in order; stop after item 9 (Phase 1 manual test) and after item 17 (Phase 2 manual test) to confirm before continuing.
- **RIPER-5 mode**: this plan was produced in PLAN mode. Review carefully, then say **"ENTER EXECUTE MODE"** to begin implementation exactly as specified above. No further RESEARCH or INNOVATE work is expected to be needed — all touchpoints, line numbers, and test strategies were verified by reading the live source files at plan time.
- If scope changes mid-execution (e.g., a touchpoint's surrounding code has materially changed since 08-11-26, not just shifted line numbers), pause and update this plan file's relevant section before continuing, rather than silently deviating.
