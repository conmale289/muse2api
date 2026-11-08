# muse2api — Enhancement Audit

**Date:** 2026-11-06
**Scope:** image/video generation quality, reliability & security, performance & concurrency, API compatibility, admin UI / extension / docs, tests & CI
**Type:** Research only. No code was modified.
**Baseline:** working tree on `main`, post-English-conversion (uncommitted).

---

## 1. Executive summary

The service is functionally solid — the hard parts (CDP automation, attachment verification, refusal
detection, multi-account leasing, reference-media safety) are already well built and well tested.
The remaining risk is concentrated in **four structural areas**:

1. **Two locks are acquired in opposite orders.** `GEN_LOCK` → `ACCOUNT_POOL` in generation,
   `ACCOUNT_POOL` → `GEN_LOCK` in chat. Combined with `max_concurrency=2` and a per-account lease,
   this creates a reachable 60–600 s stall, not just a theoretical inversion.
2. **A disconnecting streaming client can permanently consume a browser slot.** `safe_chat_stream`
   does an unbounded `q.put()` while holding `GEN_LOCK`. Two such events wedge the whole service.
3. **`store._read` converts a corrupt JSON file into an empty one**, and the next write persists the
   empty state. That is silent destruction of `accounts.json` (all cookies) or `tasks.json`.
4. **Admission is asymmetric.** Images are capped at 8 queued tasks; video has *no* cap and spawns an
   unbounded thread per request, while `VideoRequest.timeout` has no upper bound.

On the product side, the two highest-value quality fixes are small and cheap:
**stop forcing "remove all text/watermark/logo" on every reference-image request** (it actively
breaks legitimate text-rendering prompts), and **complete the aspect-ratio map for OpenAI's
`1024x1792` / `1792x1024`**. Both are one-function changes.

| Severity | Count | Meaning |
|---|---|---|
| Critical | 2 | Silent data loss or a service-wide stall |
| High | 10 | Wrong under normal load: hangs, credential exposure, unbounded admission, broken client contract |
| Medium | 22 | Correctness/quality defects with a workaround, or hardening gaps |
| Low | 15 | Polish, docs, low-impact compatibility |

49 findings in total; §3 lists every one, §10 groups them into three implementation batches.

---

## 2. Method

Evidence was read directly from the working tree (`app.py`, `engine.py`, `store.py`, `cdp.py`,
`config.py`, `proxy.py`, `admin.html`, `extension/`, `tests/`, `Dockerfile`, `docker-compose.yml`).
Every finding below cites a file and line range. Severity reflects *impact × reachability in the
current default configuration* (`MUSE2API_CONCURRENCY` unset → 2, single Chromium instance, API key
shared with all downstream clients).

Effort scale: **S** < 1 h · **M** ≈ half day · **L** 1–3 days · **XL** > 3 days.

---

## 3. Priority matrix

**All findings have been implemented — see §12 for the record.** Q8 was rejected after
checking `tests/test_reference_media.py`. The rows below are kept as the audit found them.

| ID | Finding | Severity | Effort | Area |
|---|---|---|---|---|
| R3 | `store._read` silently resets corrupt JSON → data loss | Critical | S | Reliability |
| R2 | Unbounded `q.put` in `safe_chat_stream` pins `GEN_LOCK` forever | Critical | S | Reliability |
| R1 | `GEN_LOCK` ↔ `ACCOUNT_POOL` lock-order inversion | High | M | Reliability |
| R4 | GitHub token written into `.git/config` | High | S | Security |
| R5 | `/admin/repo/push` + `/admin/upgrade` gated only by inference API key | High | M | Security |
| R6 | Unbounded video admission (no queue cap, thread per task) | High | M | Reliability |
| P1 | Chat/Responses pick an account outside `ACCOUNT_POOL` | High | S | Performance |
| Q1 | Unconditional "remove all text/watermark/logo" prompt | High | S | Quality |
| Q2 | Aspect-ratio map misses OpenAI `1024x1792` / `1792x1024` | High | S | Quality |
| A1 | `/v1/videos` diverges from the OpenAI Sora shape + no content endpoint | High | M | API |
| T1 | `pytest` cannot collect; no pinned test deps | High | S | Tests |
| T2 | No CI at all | High | M | Tests |
| R7 | Unvalidated `VideoRequest.timeout` / `duration` / `size` | Medium | S | Reliability |
| R8 | Non-constant-time key compare, no rate limiting | Medium | M | Security |
| R9 | `.env` written with default (0644) permissions | Medium | S | Security |
| R10 | Chromium start race outside `_start_lock` | Medium | S | Reliability |
| R11 | `CFG.cors_origins` parsed then ignored | Medium | S | Security |
| P2 | Full `accounts.json` rewrite on every account pick | Medium | S | Performance |
| P3 | Full `tasks.json` rewrite + unbounded task history | Medium | M | Performance |
| P4 | `max_concurrency=2` vs one Chromium/CDP connection | Medium | L | Performance |
| Q3 | `n` accepted but ignored (always 1 image) | Medium | M | Quality |
| Q4 | `size` echoed back even when not honored | Medium | S | Quality |
| Q5 | Chat `image_url` collapsed to `[图片]`, never forwarded | Medium | M | Quality |
| Q6 | Video `duration` asserted in prompt with no upstream control | Medium | S | Quality |
| A2 | `GET /v1/models/{model}` missing | Medium | S | API |
| A3 | `usage` always zero | Medium | S | API |
| A4 | Idempotency only on image tasks | Medium | M | API |
| A5 | Responses API silently drops `tools`, `temperature`, etc. | Medium | M | API |
| U1 | English default applied client-side → Chinese flash on first paint | Medium | M | UI |
| U2 | i18n is a Chinese-keyed regex dictionary → new strings leak Chinese | Medium | M | UI |
| U3 | Extension requests `http://*/*` + `https://*/*` | Medium | S | Extension |
| T3 | Browser tests ignore `MUSE2API_CHROMIUM` | Medium | S | Tests |
| T4 | `test_admin_layout.py` 320 px assertion fails on `HEAD` | Medium | S | Tests |
| T5 | No tests for lock ordering, corruption, video cap, idempotency | Medium | M | Tests |
| R12 | `/admin/cookie-helper`, `/admin/extension` unauthenticated | Low | S | Security |
| R13 | Container runs as root, no healthcheck | Low | S | Ops |
| P5 | Fixed sleeps dominate chat latency | Low | M | Performance |
| P6 | Keepalive rewrites `accounts.json` once per account | Low | S | Performance |
| P7 | No media/task retention policy | Low | M | Performance |
| Q7 | Repeated "严禁使用历史图片" dilutes the creative instruction | Low | S | Quality |
| Q8 | `_REFERENCE_TOKEN` decides wording via a fragile regex | Low | S | Quality |
| A6 | Alias map lacks newest model names | Low | S | API |
| A7 | `/v1/models` has no capability metadata | Low | S | API |
| A8 | No `Idempotency-Key` echo header | Low | S | API |
| U4 | Extension stores API key in `chrome.storage.local` | Low | S | Extension |
| U5 | Version drift: 1.6.6 / 1.6.9 / 1.6.1 | Low | S | Docs |
| U6 | Admin tabs have no deep-link / no `PRG` | Low | M | UI |
| U7 | README doesn't state English is the new default | Low | S | Docs |
| T6 | Tests are standalone scripts with no shared harness | Low | M | Tests |

---

## 4. Reliability & security

### R3 — `store._read` silently converts corrupt JSON into empty state (Critical, S)

**Evidence:** `store.py:79-87`, consumed at `store.py:107-109`.

```python
def _read(path: str, default):
    ...
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default      # accounts.json -> []
```

`_write` is atomic (`tmp` + `os.replace`), so torn writes are unlikely — but any other corruption
(disk full during `os.replace`, manual edit, truncated restore from backup, concurrent `cp`) turns
"file unreadable" into "file empty". The *next* `add_account` / `mark` / `update_task` writes `[]`
over the real file. **All cookies for all accounts are gone, with no log line.**

**Impact:** total account-pool loss, unrecoverable without re-importing every Muse session.
**Proposed change:** distinguish "missing" from "unparseable". On parse failure, move the file aside
(`accounts.json.corrupt-<ts>`) and raise/log loudly instead of returning the default. Refuse to
serve the account pool until an operator confirms.
**Verification:** new test writes a truncated `accounts.json`, asserts (a) a `.corrupt-*` backup
exists, (b) a subsequent `update_task` does not write an empty accounts file, (c) an error is logged.

---

### R2 — Unbounded `q.put` in `safe_chat_stream` can pin `GEN_LOCK` forever (Critical, S)

**Evidence:** `app.py:375` (`q = queue.Queue(maxsize=100)`), `app.py:426-429`, `app.py:452-455`.

```python
with GEN_LOCK:
    ...
    for chunk in engine.chat_stream(...):
        q.put(("data", chunk))      # no timeout, no stop_event check before blocking
```

The consumer stops draining when the downstream client disconnects; `stop_event` is set in the
generator's `finally` (`app.py:469`), but the worker is already blocked inside `q.put` **while
holding `GEN_LOCK`**. It never observes `stop_event`. With `max_concurrency=2`, two disconnects
during a long answer permanently consume the entire browser queue.

**Impact:** total service stall requiring a restart; trivially reachable by any client that
disconnects mid-stream (browser tab close, agent timeout, reverse-proxy idle timeout).
**Proposed change:** `q.put(..., timeout=1.0)` in a loop that checks `stop_event`, and on
`queue.Full` abort the worker. Alternatively set `stop_event` from the consumer's `finally` *and*
use `q.put_nowait` with an explicit drop-and-abort policy.
**Verification:** a unit test with a fake `chat_stream` yielding 1000 chunks and a consumer that
breaks after 10; assert `GEN_LOCK` is released within a bounded time and the worker exits.

---

### R1 — `GEN_LOCK` ↔ `ACCOUNT_POOL` lock-order inversion (High, M)

**Evidence:**
- Generation: `app.py:1101` acquires `GEN_LOCK`, then `app.py:1133` calls `ACCOUNT_POOL.acquire(...)`.
- Chat: `app.py:406` acquires the account lease, then `app.py:417` enters `with GEN_LOCK`.
- Admin maintenance: `app.py:2015`, `2083`, `2109` use `with GEN_LOCK, _account_operation(aid)`
  (GEN_LOCK first, then `ACCOUNT_POOL.cond`).

Concrete deadlock with the default `limit=2` and 2 live accounts:
two generation threads each take a `GEN_LOCK` slot and then wait for an account, while two chat
threads each hold an account lease and wait for a `GEN_LOCK` slot. Neither side can progress.
`ACCOUNT_POOL.acquire` raises only after its `timeout` — which for generation is the *task* timeout
(`max(1, timeout)`, up to 600 s). So the observable symptom is a multi-minute hang, not an error.

Secondary effect even without a full deadlock: a thread holding a scarce `GEN_LOCK` slot while
waiting for an account is pure head-of-line blocking — the browser sits idle.

**Impact:** hangs under normal multi-client load; the most likely "why did it stop responding?" bug.
**Proposed change:** enforce one order everywhere. Recommended: **account lease first, browser lock
second** (matching chat), since the account is the scarcer of the two and the browser is a single
shared resource. That means moving `ACCOUNT_POOL.acquire` out of `_run_generation_locked` and above
`GEN_LOCK.acquire` in `_run_generation`, then converting the admin paths to
`with _account_operation(aid): with GEN_LOCK:`. Document the invariant in a comment next to both
locks.
**Verification:** add a stress test that starts N generation + N chat calls against a fake engine and
asserts total wall time stays proportional to `N/limit` with no call exceeding a bound. A simpler
guard: assert lock ordering with an instrumented `ConcurrencyLock` that records the thread's
currently-held lock and fails on inversion.

---

### R4 — GitHub token persisted into `.git/config` (High, S)

**Evidence:** `app.py:2570-2581`.

```python
remote_url = f"https://x-access-token:{token}@github.com/.../muse2api.git" if token else ...
_git(["remote", "set-url", "origin", remote_url])
```

The token is written in plaintext to `.git/config`, is visible to any process that can read the
repo, survives in `.git/config` after the operation, and leaks into any error message that echoes
the remote URL. `.git/config` is not in `TRACKED_REPO_PATHS`, so it isn't pushed — but it is
persisted on disk indefinitely.

**Proposed change:** keep `origin` token-free and inject credentials per-command via
`-c credential.helper=` + `GIT_ASKPASS`, or use an `http.extraHeader` passed on the command line
only. At minimum, strip the token from the remote after the push in a `finally`.
**Verification:** run a push against a local bare repo; assert `.git/config` contains no token, and
that a failed push does not leave a token behind.

---

### R5 — `/admin/repo/push` and `/admin/upgrade` are gated by the inference API key (High, M)

**Evidence:** `app.py:2786-2799` (`/admin/upgrade`), `app.py:2803-2836` (`/admin/repo/push`),
both `_=Depends(auth)`. `auth` (`app.py:320-329`) checks the same `CFG.api_key` that every
downstream client (OpenCode, Cline, Open WebUI…) is given.

So any client that can call `/v1/chat/completions` can also:
- commit and force-push this node's source to the upstream repo (`git push origin HEAD:main`),
- overwrite local core files from GitHub and `os._exit(0)` the service.

The docstring says "maintainer-only", but there is no separate maintainer credential.

**Proposed change:** split the credential. Introduce `MUSE2API_ADMIN_KEY` (or a separate
`/admin/*` bearer) and require it for mutating admin routes; keep the inference key scoped to
`/v1/*`. If a single key must remain, add an explicit `MUSE2API_MAINTAINER=1` opt-in that is
*off* by default and gates just `/admin/repo/push`, `/admin/upgrade`, `/admin/apikey/rotate`.
**Verification:** with `MUSE2API_ADMIN_KEY` set, assert the inference key gets 403 on
`/admin/repo/push` and 200 on `/v1/models`.

---

### R6 — Unbounded video admission (High, M)

**Evidence:** `app.py:1284-1287` caps images at 8 queued tasks. `app.py:1461-1498` has no cap at
all: it creates a task and `threading.Thread(...).start()` immediately, per request.

Each accepted video task holds a lease candidate for up to `MUSE2API_VIDEO_TIMEOUT` (600 s by
default). N unbounded requests → N threads, N task records, and a queue that can only drain at the
rate of one browser.

**Proposed change:** mirror the image guard with a `video` cap (a dedicated
`MUSE2API_VIDEO_QUEUE_MAX`, default e.g. 4) returning `429`, and reuse a single admission helper
for both kinds so the two paths can't drift again.
**Verification:** submit `cap+1` videos with a stubbed `_run_generation`; assert the last returns
429 and no extra thread is created.

---

### R7 — Unvalidated `VideoRequest.timeout` / `duration` / `size` (Medium, S)

**Evidence:** `app.py:490-505`. `timeout: int | None = None` — no `Field(ge=…, le=…)`, unlike
`ImageRequest.timeout` (`app.py:481`, `Field(default=None, ge=1, le=600)`).
`duration` flows straight into the prompt string (`app.py:945`), and `size` into
`f"尺寸/比例：{r.size}"` (`app.py:737`).

`timeout=10**9` gives a worker that holds a `GEN_LOCK` slot essentially forever; `duration=-3`
renders `时长严格为 -3 秒` into the Muse prompt.

**Proposed change:** `timeout: int | None = Field(default=None, ge=10, le=3600)`,
`duration: int | None = Field(default=None, ge=1, le=60)`, and normalize `size` against an
allowlist (or clamp to `^\d{2,5}x\d{2,5}$`).
**Verification:** `422` for `timeout=0`, `timeout=999999`, `duration=-1`; `200` for the valid range.

---

### R8 — Non-constant-time key compare, no rate limiting (Medium, M)

**Evidence:** `app.py:327` — `token != CFG.api_key`. No attempt counter, no backoff, no 429 on
repeated 401s anywhere in `app.py`.

Timing attack on a 48-bit-entropy random key is not realistic, but the *absence of any brute-force
throttle* is. The service is meant to be exposed through nginx; nginx's `limit_req` is the natural
mitigation but isn't in `deploy/nginx.example.conf`.

**Proposed change:** `secrets.compare_digest(token, CFG.api_key)` (S, do it regardless), plus
either an in-process sliding-window failure counter per IP, or a documented `limit_req_zone` block
in `deploy/nginx.example.conf` (S, no code).
**Verification:** unit test asserts a 401 path uses `compare_digest` (monkeypatch) and that the
nginx example contains `limit_req`.

---

### R9 — `.env` written with default permissions (Medium, S)

**Evidence:** `app.py:2167-2183`. `tmp = path + ".tmp"` then `open(tmp, "w")` — mode comes from the
umask (typically `0644`). The file holds `MUSE2API_KEY` and `GITHUB_TOKEN`, and `os.replace`
preserves the temp file's mode.

**Proposed change:** `os.chmod(tmp, 0o600)` before `os.replace`, and `os.chmod(path, 0o600)` on
startup if it already exists.
**Verification:** after `/admin/apikey/rotate`, assert `stat.S_IMODE(...) == 0o600`.

---

### R10 — Chromium start race outside `_start_lock` (Medium, S)

**Evidence:** `engine.py:349-352` holds `_start_lock` only for the fast-path check, then releases it
before probing the CDP endpoint (`369-378`), unlinking singleton locks (`380-386`), and spawning
Chromium with a 90-iteration poll (`392-400`).

With `GEN_LOCK` limit 2, two threads can both pass the check and both `Popen` a browser against the
same `--user-data-dir` and `--remote-debugging-port`. Result: the loser's process conflicts on the
port and the singleton lock is deleted from under the winner.

**Proposed change:** hold `_start_lock` for the whole method (re-entrancy isn't needed), or use a
`_start_done` `threading.Event` with a double-checked pattern.
**Verification:** a test spawning 8 threads calling `engine.start()` against a stub binary asserts
exactly one `Popen` call.

---

### R11 — `CFG.cors_origins` parsed then ignored (Medium, S)

**Evidence:** `app.py:77` builds `_origins` from `CFG.cors_origins`, then `app.py:81` uses
`allow_origins=["*"]`. `_origins` is dead code. `config.py:102-106` still documents the setting as
meaningful.

**Impact:** two problems. (a) An operator who sets `MUSE2API_CORS_ORIGINS` to lock the service down
gets no effect and no warning — a silent security expectation mismatch. (b) `["*"]` is the right
default for a Bearer-authenticated API, so the *behaviour* is defensible; the *configuration* is a
trap.

**Proposed change:** either honour the setting (`allow_origins=_origins or ["*"]`) or delete the
setting and the comment. Honouring it is better; keep `*` as the default so current behaviour is
unchanged.
**Verification:** `MUSE2API_CORS_ORIGINS=https://example.com` → preflight from another origin is
rejected; unset → still `*`.

---

### R12 — Unauthenticated admin helpers (Low, S)

**Evidence:** `app.py:2190` (`/admin/cookie-helper`), `app.py:2202` (`/admin/extension`) have
no `Depends(auth)`.

Both serve static, already-public content (a stdlib cookie script, and a zip of the extension
folder that is also in the README release). Low impact, but it means an unauthenticated caller can
trigger a zip build per request — a cheap CPU amplification.

**Proposed change:** add `Depends(auth)`, or cache the zip bytes once at startup.
**Verification:** `curl` without a key returns 401.

---

### R13 — Container runs as root, no healthcheck (Low, S)

**Evidence:** `Dockerfile:1-45` (no `USER`), `chromium ... --no-sandbox` (`engine.py:358`),
`docker-compose.yml` has no `healthcheck` despite `/healthz` existing (`app.py:1209`).

Running Chromium as root forces `--no-sandbox`, which is the single largest container-escape
surface in the stack. A non-root `USER` lets you drop `--no-sandbox`.

**Proposed change:** create a `muse2api` user, `chown` `/app/data` and the profile root, add
`USER muse2api`, and make `--no-sandbox` conditional on `os.geteuid() == 0`. Add a compose
`healthcheck` hitting `/healthz`.
**Verification:** build, run, `docker exec id -u` → non-zero; `/healthz` reachable; an image
generation still succeeds.

---

## 5. Performance & concurrency

### P1 — Chat and Responses pick an account outside `ACCOUNT_POOL` (High, S)

**Evidence:** `app.py:1597` (chat) and `app.py:1759` (Responses) both call
`store.pick_account(...)` directly, then hand `acc_id` to `safe_chat_stream`, which *then* calls
`ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)` (`app.py:406`).

`store.pick_account` has no knowledge of `ACCOUNT_POOL.busy_accounts`. So two concurrent chat
requests can both be told "use account A"; the second then blocks inside `acquire` for up to the
full chat timeout while account B sits idle. Under a burst of agent requests this looks exactly
like "the service is slow", and it inflates `use_count`/`last_used` for an account that may never
have served the request.

**Proposed change:** let `ACCOUNT_POOL.acquire()` do the selection — call it with
`preferred_id=getattr(engine, "current_acc_id", None)` and no `account_id`, then pass the returned
`id` downstream. That also removes a redundant `accounts.json` write (see P2).
**Verification:** two concurrent chat requests against 2 stubbed accounts must bind to *different*
accounts and complete in ~1× the single-request time.

---

### P2 — `accounts.json` fully rewritten on every account pick (Medium, S)

**Evidence:** `store.py:190-196` and `store.py:204-206` call
`_write(self.cfg.accounts_file, self.accounts)` inside `pick_account`, which is called on every
generation *and* every chat. The file contains every account's full cookie jar.

Cost per pick: serialize all accounts + cookies, write a temp file, `os.replace`. With a 20-account
pool that's a meaningful fraction of the per-request overhead, and it rewrites secrets on every
request.

**Proposed change:** make `last_used`/`use_count` in-memory only, flushed by the existing 30-minute
keepalive cycle (or a 60 s debounced writer). `checked_at`/`note`/`ok` still write immediately —
those are correctness-bearing.
**Verification:** instrument `_write`; a 100-request run should produce ≤ a handful of
`accounts.json` writes instead of ~100.

---

### P3 — `tasks.json` rewrites and unbounded history (Medium, M)

**Evidence:** `store.py:302-310` — `update_task` re-serializes the **entire** `self.tasks` dict on
every progress update. Progress is milestone-gated (`engine.py:1226-1231`) so it's ~5 writes per
generation, which bounds the damage. But `self.tasks` is **never pruned**: no TTL, no cap, only
manual `/admin/tasks/clear` (`app.py:2256-2257`).

Two consequences as history grows:
- every `update_task` is O(total tasks) in serialization time;
- `_queue_image`'s idempotency lookup scans all tasks on every submit
  (`app.py:1273-1283`, `list(store.tasks.values())`), and so does the video account-preference
  scan (`app.py:1125-1135`).

**Proposed change:** (a) add a retention policy — keep last N (e.g. 200) completed/failed tasks,
enforced on write or on a timer; (b) maintain an in-memory `idempotency_key → task_id` index instead
of scanning.
**Verification:** seed 10 000 synthetic tasks, assert `create_image_task` latency is independent of
`len(store.tasks)` and that `tasks.json` stays bounded after 1 000 cycles.

---

### P4 — `max_concurrency=2` against a single Chromium/CDP connection (Medium, L)

**Evidence:** `config.py:110` defaults `MUSE2API_CONCURRENCY` to 2; `app.py:184` builds
`GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)`. But `engine` is a single `MuseEngine`
(`app.py:88`) sharing one Chromium process (`engine.py:349-400`), one CDP websocket
(`cdp.py:19-23`), and a *thread-local* page (`engine.py:314-327`).

So `limit=2` does not give two browsers — it gives two threads interleaving commands on one
browser. `CDP.send` serializes on `self._lock` (`cdp.py:26`), so commands don't corrupt the socket,
but two threads can drive two pages whose Muse state (attachments, agent counters, download dirs)
is only isolated by convention.

**Impact:** the lock's stated purpose ("multi-account concurrency scheduling") is not delivered;
raising the knob increases interference without increasing throughput.

**Options:**
1. *Cheap and honest:* default `max_concurrency=1` and rename the setting to reflect that it
   serializes the browser; document that throughput comes from shorter tasks, not parallelism.
2. *Real parallelism:* run one `MuseEngine` + Chromium per slot (distinct `profile_dir` and
   `cdp_port`), and lease an engine from a pool alongside the account. This is the L/XL option but
   it's what the current abstraction promises.

> **Resolution:** option 1 was implemented — `MUSE2API_CONCURRENCY` now defaults to **1**, and
> `.env.example` documents the setting as serialised-by-default. Option 2 remains a separate
> project and is tracked in §10 as deferred work.

**Recommendation:** do (1) now as a truth-in-config fix, and treat (2) as a separate phase if
multi-account throughput becomes a requirement.
**Verification:** throughput benchmark at 1/2/4 with (1) should show ~flat or worse beyond 1.

---

### P5 — Fixed sleeps dominate chat latency (Low, M)

**Evidence:** `engine.py:886` and `engine.py:1036` (`time.sleep(0.1)` per interaction), `engine.py:1024-1060` (`_send` with
several `sleep(0.1)`), `engine.py:1147` (`time.sleep(0.6)` in the poll loop),
`engine.py:1148` (`_scroll_bottom()` every poll iteration).

Per-request overhead is roughly `0.1×k + 0.6×polls`, and `_scroll_bottom` does a full JS round-trip
each poll even when nothing changed. For chat this is a fixed tax on every turn.

**Proposed change:** (a) replace the fixed poll cadence with an adaptive one (100 ms → 600 ms ramp);
(b) only call `_scroll_bottom()` when the attachment count or agent count changed.
**Verification:** measure time-to-first-token and time-to-first-attachment before/after on the same
account, N=10.

---

### P6 — Keepalive rewrites `accounts.json` per account (Low, S)

**Evidence:** `store.py:209-236` (`touch_keepalive` writes on every call), called once per account
from the keepalive sweep.

**Proposed change:** batch the sweep into one write (collect updates, then a single `_write`).
**Verification:** 20-account sweep produces 1 write.

---

### P7 — No media or task retention (Low, M)

**Evidence:** `data/media` only shrinks via manual `/admin/media/delete`. `data/tasks.json` never
shrinks. `_image_response` reads the media file to produce `b64_json`
(`app.py:1230-1234`) — files must therefore stay, but nothing bounds their count.

**Proposed change:** `MUSE2API_MEDIA_RETENTION_DAYS` (default off) with a startup + daily sweep;
skip files referenced by a task that is still `queued`/`processing`.
**Verification:** seed old files, run the sweep, assert age-based deletion and that in-flight task
files survive.

---

## 6. Image / video generation quality

### Q1 — Unconditional "remove all text/watermark/logo" (High, S)

**Evidence:** `app.py:739-740`.

```python
if has_ref and "image" in kinds:
    parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记"
                 "（clean image without any watermark, text, or logo），输出绝对纯净无字画面")
```

This fires on **every** request that carries a reference image, regardless of what the user asked
for. A prompt like "put the title 'SUMMER SALE' on this poster" or "translate the text in this
screenshot" gets an instruction that directly contradicts it — and the model has to choose.

**Impact:** the single most likely cause of "it removed my text" complaints. Cheap to fix, high
visible value.
**Proposed change:** gate it. Apply the clean-picture clause only when the user prompt does **not**
ask for text (no quoted strings, no `文字|文本|标题|字幕|排版|text|title|caption|subtitle|lettering|
typography|logo` match) *and* `r.extra` doesn't request it. Better: make it a per-request flag
(`preserve_text: bool = False`) exposed on `ImageRequest`, defaulting to the current behaviour only
for the plain "no text requested" case.
**Verification:** a `build_image_prompt` unit test matrix — (a) reference + "add the title 'X'" must
not contain `去除`/`without any watermark`; (b) reference + plain scene description must contain it.

---

### Q2 — Aspect-ratio map misses OpenAI's `1024x1792` / `1792x1024` (High, S)

**Evidence:** `app.py:721-737`. Recognised sizes are `720x1280`, `1080x1920` (portrait),
`1280x720`, `1920x1080` (landscape), `1024x1024` (square). Anything else falls through to
`f"尺寸/比例：{r.size}"` (line 737) — a bare restatement with no explicit orientation language.

`1024x1792` (portrait) and `1792x1024` (landscape) are the `dall-e-3` / `gpt-image-1` sizes most
OpenAI-compatible clients send. Today they land on the weakest branch, so a portrait request can
come back landscape.

**Proposed change:** replace the substring match with a parsed classifier — parse `HxW`, compute
the ratio, classify into portrait/square/landscape with a tolerance, and keep the named-ratio
branches as an override. That fixes `1024x1792`, `1792x1024`, `2048x2048`, `1536x1024`, etc. in one
change and removes the drift risk of a hardcoded list.
**Verification:** table-driven test over ~15 sizes/ratios asserting the chosen clause; specifically
`1024x1792` → portrait and `1792x1024` → landscape.

---

### Q3 — `n` accepted but ignored (Medium, M)

**Evidence:** `ImageRequest.n` at `app.py:475`; `_image_response` (`app.py:1227-1235`) always emits
exactly one item.

OpenAI clients that pass `n=4` get one image and no error. Silent partial compliance is worse than
a 400 because callers can't detect it.

**Options:** (a) honestly reject `n>1` with a 400 explaining the limitation (S); (b) implement it by
running `n` sequential generations and returning `n` items (M, multiplies quota cost — should be
opt-in). **Recommend (a)** now, (b) behind an explicit flag if requested.
**Verification:** `n=2` → 400 with a clear message; `n=1` unchanged.

---

### Q4 — `size` is echoed back even when not honored (Medium, S)

**Evidence:** `app.py:1229` — `"size": req.size or "auto"`.

The response claims the requested size regardless of what Muse actually produced. The engine does
capture real dimensions on the attachment (`engine.py:1853` and `engine.py:1876`:
`"w": att.get("w"), "h": att.get("h")`), but `_image_response` doesn't use them.

**Proposed change:** surface the actual `w`/`h` when available (`size: "WxH"`), falling back to the
requested value. This makes client-side assertions meaningful.
**Verification:** a stubbed result with `w=1024,h=1792` for a `1024x1024` request returns
`size: "1024x1792"`.

---

### Q5 — Chat `image_url` collapsed to `[图片]` (Medium, M)

**Evidence:** `app.py:760-761` in `_content_text`.

```python
elif t == "image_url":
    parts.append("[图片]")
```

Any vision-capable client that sends an image gets a literal placeholder — the image bytes are never
attached. Since `_attach_images` already exists for generation (`engine.py:1711`), the capability is
present but not wired to chat.

**Proposed change:** collect `image_url` parts during `build_chat_prompt`, run them through
`_prepare_reference_images`, and pass them to `engine.chat_stream` as attachments. Until that lands,
return an explicit `400` for image parts rather than silently degrading — silent degradation is what
generates "the model hallucinated my screenshot" reports.
**Verification:** a chat request with one `data:` image reaches `_attach_images` (mock) with one
entry; a request without images is unchanged.

---

### Q6 — Video `duration` asserted in the prompt with no upstream control (Medium, S)

**Evidence:** `app.py:945` (`dur = r.duration or 6`) and `app.py:974-986`
(`时长严格为 {dur} 秒`).

Muse exposes no duration parameter. Pinning "strictly N seconds" into the prompt makes the model
*claim* compliance while the rendered clip length is whatever upstream chose. The result reads as a
bug.

**Proposed change:** keep the duration as a soft preference (`时长约 N 秒` / "roughly N seconds"),
and report the measured duration back. `engine.py:126-200` already has an EBML/Matroska duration
parser (`_mp4_duration`) — use it to populate `duration` in the task result.
**Verification:** a completed video task reports a measured duration; the prompt no longer contains
`严格`.

---

### Q7 — Prompt over-constraining (Low, S)

**Evidence:** the phrase `严禁使用历史图片或任何其他图像` / `严禁参考任何历史图片或上下文` appears
in nearly every branch (`app.py:715, 950-966, 974-986`).

It exists for a real reason (Muse pulls from the user's album), but repeating it 1–3× per prompt
plus a long parenthetical dilutes the actual creative instruction and eats context budget.

**Proposed change:** state the constraint once, at the end, in a short canonical sentence; keep the
branch-specific wording focused on composition.
**Verification:** A/B a handful of prompts for attachment-selection accuracy (the existing
`test_media_selection.py` harness can be reused) and confirm no regression.

> **Correction (measured during implementation):** the "repeating it 1–3×" premise above is
> wrong. Across 11 branch combinations each prompt contained **at most one** history
> constraint. The real defect was five *different* wordings embedded mid-sentence. Fixed by
> replacing them with one constant appended last — see §12. The original A/B verification was
> not run; `tests/test_media_selection.py` still passes unchanged, which is the regression
> check that exists.

---

### Q8 — `_REFERENCE_TOKEN` decides wording via a fragile regex (Low, S)

**Evidence:** `app.py:715` and `app.py:955` gate the "legend" wording on
`_REFERENCE_TOKEN.search(r.prompt)`.

A user prompt that happens to contain the token triggers the multi-reference legend branch even for
a single reference, producing different phrasing for semantically identical requests.

**Proposed change:** decide on `len(refs) > 1` alone (which the branch already requires) and drop
the regex from the condition.
**Verification:** a `build_image_prompt` test with 1 ref and a token-containing prompt is identical
to the no-token variant.

---

## 7. API compatibility & features

### A1 — `/v1/videos` diverges from the OpenAI Sora shape (High, M)

**Evidence:** `app.py:1461-1498` (create), `app.py:1501-1525` (get). Routes present:
`POST /v1/videos`, `POST /v1/videos/generations`, `GET /v1/videos/{id}`,
`GET /v1/videos/generations/{id}`.

Gaps versus the OpenAI video API:
- no `GET /v1/videos/{id}/content` (this is what the official SDK's `videos.content(id)` calls —
  the SDK cannot download the result);
- no `DELETE /v1/videos/{id}`;
- `object` is `"video.task"`, not `"video"`;
- missing `model`, `seconds`, `size`, `completed_at`, `expires_at`, `error` on the completed object.

**Impact:** any client using the OpenAI SDK for video fails at the download step, even though the
file is produced and served at `/v1/media/...`.
**Proposed change:** add the content endpoint as a thin redirect/serve over `out["url"]`, add
`DELETE`, and emit the full object shape. Keep the existing custom fields for backward compat.
**Verification:** `curl -OJ $BASE/v1/videos/$ID/content` returns the mp4 with the right
`Content-Type`; a small script using `openai` SDK `videos.create` → poll → `videos.content`
succeeds.

---

### A2 — `GET /v1/models/{model}` missing (Medium, S)

**Evidence:** only `/v1/models` exists (`app.py:1221-1223`). Many OpenAI-compatible clients validate
a configured model with `GET /v1/models/{id}` before use.

**Proposed change:** add the single-model route, resolving aliases and 404-ing unknown ids.
**Verification:** `GET /v1/models/dall-e-3` → 200 with `id: "muse-image"`; unknown → 404.

---

### A3 — `usage` always zero (Medium, S)

**Evidence:** `app.py:1776-1778` — `"usage": {"input_tokens": 0, "output_tokens": 0,
"total_tokens": 0}`. Chat presumably similar.

Clients that budget or bill on `usage` see zero-cost turns, and any dashboard built on it is wrong.

**Proposed change:** either estimate (`len(prompt)//4`, `len(output)//4` — clearly labelled as an
estimate) or omit `usage` entirely. An omitted field is more honest than a false zero, but some SDKs
require it; estimate with a note in the README.
**Verification:** a non-empty completion returns non-zero `total_tokens` consistent with the
estimate.

---

### A4 — Idempotency only on image tasks (Medium, M)

**Evidence:** `app.py:1265-1285` implements `Idempotency-Key` for images only. Video/chat ignore the
header entirely.

An agent that retries a timed-out video POST gets a second render — expensive in quota.

**Proposed change:** lift the idempotency block out of `_queue_image` into a shared helper keyed by
`(key_hash, kind)` and apply it to `/v1/videos` too. Add the same short-circuit to chat only if the
client sends the header (cheap, but chat is not safely replayable if streamed — document that).
**Verification:** two identical video POSTs with the same key return the same `task_id` and create
one task.

---

### A5 — Responses API silently drops unsupported fields (Medium, M)

**Evidence:** `app.py:1746-1829` (handler) and `app.py:536-545` (model). `ResponsesRequest`
declares `instructions`, `max_output_tokens`, `tools` and `store`, but the handler only reads
`input`, `model`, `stream` and `timeout`. The other four are accepted and silently ignored; fields
not declared at all (`temperature`, `tool_choice`, `previous_response_id`, `reasoning`) are dropped
by pydantic's default `extra='ignore'`. Nothing in the response tells the caller.

**Proposed change:** map `instructions` onto the system turn (it already has a system slot in
`build_chat_prompt`), echo `max_output_tokens`/`temperature` back as accepted-but-unsupported in a
response `metadata` note, and return a documented `400` for `tools` when `CFG.tool_protocol` is off —
currently it silently answers without tools, which surprises agent frameworks.
**Verification:** a request with `instructions` shows it in the flattened prompt; a request with
`tools` and `tool_protocol=0` returns a documented error rather than a plain answer.

---

### A6 — Alias map lacks newest model names (Low, S)

**Evidence:** `app.py:287-311`. Missing: `gpt-5.1`, `claude-sonnet-4-5`, `claude-opus-4-1`,
`gemini-3-pro`, `gpt-image-1.5`, `o3-pro`.

Impact is low because `resolve_model` passes unknown names through unchanged (`app.py:314-316`) and
`/v1/chat/completions` ignores `model` for routing anyway — but `/v1/images/generations` uses
`resolve_model` to decide *kind*, so a new image model name would be misrouted.

**Proposed change:** prefix-based classification in addition to the alias table —
`gpt-image*`/`dall-e*`/`flux*`/`midjourney*` → image, `sora*`/`veo*`/`kling*`/`runway*` → video,
everything else → chat. That is forward-compatible and removes the maintenance treadmill.
**Verification:** `resolve_model("gpt-image-2")` → `muse-image`; `resolve_model("sora-3")` →
`muse-video`.

---

### A7 — `/v1/models` has no capability metadata (Low, S)

**Evidence:** `app.py:272-279`. Entries carry `id`/`object`/`owned_by`/`description` only.

**Proposed change:** add `context_window`, `supports_vision`, `supports_streaming`, and
`modalities` per model. Costs nothing and lets clients auto-configure.
**Verification:** schema test over `/v1/models`.

---

### A8 — No `Idempotency-Key` echo header (Low, S)

**Evidence:** `app.py:1283-1290`. The 202 response body carries the id but the header is not echoed,
so clients can't correlate a retry with the original.

**Proposed change:** return `Idempotency-Key` and `Idempotent-Replayed: true|false` headers.
**Verification:** header present on first and replayed responses; `Replayed` differs.

---

## 8. Admin UI, extension & documentation

### U1 — English default is applied client-side, so the first paint is Chinese (Medium, M)

**Evidence:** `admin.html:2` declares `<html lang="en">` and `admin.html:798-801` defaults
`LANG` to `'en'` — but the static HTML body is still Chinese source text
(`admin.html:262-738`), translated at runtime by `translateTree` (`admin.html:1372-1420`).

Consequences: a flash of Chinese on every load; a Chinese page entirely if JS fails or is blocked
(and the page is unusable without JS anyway, so this is cosmetic rather than functional); search
engines and screen readers see the Chinese source.

**Proposed change:** either (a) pre-render the English into the HTML and treat Chinese as the
translation, or (b) add a tiny inline bootstrap that hides `<body>` until `translateTree` has run
once (`visibility:hidden` → `visible`), which removes the flash for ~15 lines of work. (b) is the
cheap 90% fix.
**Verification:** throttle the CPU 6× and load the page; no Chinese text is visible in the first
frame.

---

### U2 — i18n is a Chinese-keyed regex dictionary (Medium, M)

**Evidence:** `admin.html:806-1245` (`I18N_EN` whole-sentence map) and
`admin.html:1246-1312` (`I18N_EN_PATTERNS`, ~60 regexes for strings with variables). Any string the
backend produces that doesn't match a pattern stays Chinese in English mode.

Concrete example: `admin.html:2303-2315` builds the update-status panel by interpolating Chinese
labels into HTML (`当前版本核心特性`, `官方仓库最近提交动态`, `暂无提交记录`,
`检测到新版本更新！`, `当前已是官方仓库最新版本`, `待鉴权`). All six do have English entries today
(`admin.html:1146-1150` plus one regex in `I18N_EN_PATTERNS`), and they are translated at runtime by
`translateTree`'s MutationObserver (`admin.html:1406-1420`). So the mechanism works — but it works by
*exact* string match, including punctuation. `当前版本核心特性` is keyed as a regex with a
full-width colon and a captured version number; if that interpolation changes shape, the string
silently reverts to Chinese with no error. There is no test asserting coverage, so drift is
invisible until a user reports it.

**Proposed change:** move user-visible strings to keys on both sides — have the backend return
`{"code": "quota_expired", "params": {...}}` and let the frontend own both languages. That is the
only version that stops drifting. Interim: add the coverage test below.
**Verification:** a test that scans `admin.html` and `app.py` for CJK string literals not present in
`I18N_EN` or matched by `I18N_EN_PATTERNS`, and fails. This is a mechanical gate that prevents
future drift.

---

### U3 — Extension requests host permissions for every site (Medium, S)

**Evidence:** `extension/manifest.json:10-16` —
`"http://*/*"`, `"https://*/*"` in `host_permissions`, alongside `cookies`, `storage`,
`activeTab`.

The broad host access exists because users self-host on arbitrary URLs, and the popup POSTs to
whatever URL is typed. But it means the extension can read and modify traffic to *every* site the
user visits — a large trust ask for a cookie-import helper, and a Chrome Web Store review problem.

**Proposed change:** move the generic hosts to `optional_host_permissions` and request the specific
origin at runtime via `chrome.permissions.request({origins: [serverOrigin]})` when the user saves
the service URL. `*://muse.ai/*` stays required (needed for `cookies.getAll`).
**Verification:** fresh install shows no "read and change all your data on all websites" warning;
import succeeds after granting the one origin.

---

### U4 — Extension stores the API key unencrypted (Low, S)

**Evidence:** `extension/popup.js` persists config under `muse2api_ext_cfg` in
`chrome.storage.local`.

Standard for extensions, but worth documenting: the key grants full admin access (see R5), so an
extension compromise is a full service compromise.

**Proposed change:** document it in `extension/README.md`; combine with R5 (separate admin key) so
the stored key can't push code.
**Verification:** docs mention the risk.

---

### U5 — Version drift (Low, S)

**Evidence:** `app.py:69` — `FastAPI(title="muse2api", version="1.6.6")`; `version.json` — `1.6.9`;
README release note — `1.6.1`.

**Proposed change:** read the version from `version.json` at import (`_read_local_version()`
already exists at `app.py:2540-2549`) so there is one source of truth.
**Verification:** `/openapi.json` `info.version` equals `version.json`'s `version`.

---

### U6 — Admin tabs have no deep link (Low, M)

**Evidence:** `admin.html` switches panels via JS with no URL fragment/history entry.

**Proposed change:** `location.hash` per tab, restored on load. Makes support ("send me the
Accounts tab") and browser back/forward work.
**Verification:** reload preserves the tab; back/forward navigates tabs.

---

### U7 — README doesn't state the new English default (Low, S)

**Evidence:** `README.md` is now English (as decided), `README.zh-CN.md` preserves the original, and
`admin.html` defaults to English — but the README never says so, and there is no note that
`extension/INSTALL.txt` replaced `安装说明.txt`.

**Proposed change:** short "Language" section listing the English default, the zh/en toggle path
(`localStorage.muse2api_lang`), and the preserved Chinese docs.
**Verification:** docs review.

---

## 9. Tests & CI

### T1 — `pytest` cannot collect; test deps aren't declared (High, S)

**Evidence:** running `python3 -m pytest --collect-only -q` fails at plugin load:

```
ImportError: cannot import name 'FixtureDef' from 'pytest'
  at pytest_asyncio/plugin.py:42
```

`requirements.txt` lists only runtime deps — no `pytest`, `pytest-asyncio`, `httpx`, or Playwright
pins. There is no `pyproject.toml`, `pytest.ini`, or `setup.cfg` to constrain versions.

**Impact:** there is no discoverable, reproducible way to run the suite. Today it only works as
`python3 tests/test_x.py` standalone scripts, which is what the English conversion relied on.
**Proposed change:** add `requirements-dev.txt` (`pytest`, `pytest-asyncio`, `httpx`) with bounded
ranges, a `pyproject.toml` `[tool.pytest.ini_options]` (`asyncio_mode = "auto"`,
`testpaths = ["tests"]`), and make `pytest` the documented entry point.
**Verification:** `pip install -r requirements-dev.txt && pytest -q` collects and passes on a clean
venv.

---

### T2 — No CI (High, M)

**Evidence:** no `.github/workflows/` directory exists.

**Impact:** nothing prevents the English conversion (or any future change) from regressing; the
`git diff --check`, `compileall`, and `node --check` gates that were run manually are exactly what CI
should own.

**Proposed change:** one workflow, three jobs:
1. `lint` — `python -m compileall -q .`, `node --check admin.html`'s script (extract or lint
   `extension/popup.js`), `git diff --check`.
2. `unit` — `pytest -q tests/ -k "not browser"`.
3. `browser` — a separate, `continue-on-error` job that installs Chromium and runs the three
   browser tests, gated behind a label or a nightly schedule.

**Verification:** a deliberately broken commit fails `lint`; the browser job is green on `main`.

---

### T3 — Browser tests ignore `MUSE2API_CHROMIUM` (Medium, S)

**Evidence:**
- `tests/test_media_selection.py:17` does it correctly
  (`os.environ.get('MUSE2API_CHROMIUM') or shutil.which(...)`).
- `tests/test_reference_upload_browser.py:42` —
  `subprocess.Popen([shutil.which("chromium") or shutil.which("google-chrome"), ...])`.
- `tests/test_admin_layout.py:38` — same pattern.

On macOS with Chrome at `/Applications/Google Chrome.app/...` and no `chromium` on `PATH`, both
resolve to `None` and `Popen(None)` raises `TypeError`. The repo's own `config._detect_chromium()`
(`config.py:40-70`) already solves this — the tests just don't use it.

**Proposed change:** a shared `tests/_browser.py` helper that calls
`config._detect_chromium()` (with the `MUSE2API_CHROMIUM` override), and `raise SystemExit` with a
clear message when nothing is found.
**Verification:** on a machine with only Google Chrome installed, all three browser tests run.

---

### T4 — `test_admin_layout.py` 320 px assertion fails on `HEAD` (Medium, S)

**Evidence:** `tests/test_admin_layout.py` asserts no horizontal overflow at a 320 px viewport; this
fails against the *original* `admin.html` from `HEAD`, not just the English conversion.

**Impact:** the test can't be used as a gate, and it currently masks real regressions.
**Proposed change:** fix the layout (min-width on the account table + `overflow-x: auto` wrapper),
or downgrade to a 360 px assertion plus a documented known-issue. Prefer fixing: the accounts table
with 8 columns is the culprit.
**Verification:** test passes at 320 px on a clean checkout.

---

### T5 — Coverage gaps in exactly the risky areas (Medium, M)

**Evidence:** `tests/` has strong coverage of media selection, reference-media safety, proxy
routing, quota binding, and account sessions. Untested:
- lock ordering between `GEN_LOCK` and `ACCOUNT_POOL` (R1);
- `store._read` corruption behaviour (R3);
- video admission capping (R6);
- `safe_chat_stream` abort-on-disconnect (R2);
- idempotency replay for images (A4) and video (A4);
- `/v1/videos` object shape and content endpoint (A1);
- prompt assembly: text-request preservation (Q1) and the size classifier (Q2).

**Proposed change:** add one focused test per finding above as the corresponding fix lands. The
prompt-assembly tests (`build_image_prompt`, `build_video_prompt`) are pure functions and cost
almost nothing.
**Verification:** `pytest -q` covers each fix; coverage on `app.py` prompt + admission paths rises.

---

### T6 — No shared test harness (Low, M)

**Evidence:** each test bootstraps its own `SimpleNamespace` config, temp dirs, and (for browser
tests) a Chromium subprocess. `tests/test_media_selection.py:17-19` and
`tests/test_admin_layout.py:36-40` duplicate the same setup with different quality.

**Proposed change:** `tests/conftest.py` with fixtures: `cfg` (temp dirs), `store`, `engine_stub`,
`browser` (skips when unavailable). Converges with T3.
**Verification:** two existing tests rewritten against the fixtures with no behavioural change.

---

## 10. Recommended sequencing

**Batch 1 — "stop the bleeding" (≈1 day, mostly S)**
`R3` corrupt-JSON guard · `R2` bounded `q.put` · `R4` token out of `.git/config` ·
`R7` video field validation · `R9` `.env` 0600 · `R10` start lock · `R11` honour CORS setting
→ plus `T1` dev requirements and the first `pytest` run.

**Batch 2 — "concurrency correctness" (≈2–3 days)**
`R1` unify lock order · `R5` separate admin key · `R6` video admission cap · `P1` pool-owned account
selection · `P2` stop writing accounts per pick · `P3` task retention + idempotency index
→ plus `T3`/`T4` browser-test portability, and `T2` CI with the unit job only.

**Batch 3 — "quality and compatibility" (≈3–4 days)**
`Q1` text-preservation gate · `Q2` parsed size classifier · `Q3` explicit `n` behaviour ·
`Q4` real size echo · `Q6` measured duration · `A1` video content endpoint + object shape ·
`A2`/`A3`/`A4` · `U3` optional host permissions → then `T5` tests alongside each.

**Deferred / needs a decision**
`P4` option 2 (one engine per slot) · `Q5` chat vision attachments ·
`U2` key-based i18n migration · `P7` retention policy · `A5` Responses field mapping.

---

## 11. Open questions

1. **Admin key split (R5)** — is a second `MUSE2API_ADMIN_KEY` acceptable, or must a single key stay
   for simplicity? This determines whether `R5` is S or M.
2. **`max_concurrency` (P4)** — *answered:* default 1 (serialised). If true multi-browser
   throughput is ever wanted, option 2 (one engine per slot) is the XL project that would deliver
   it.
3. **`n>1` (Q3)** — reject, or implement at N× quota cost?
4. **Chat vision (Q5)** — is forwarding images to Muse a desired feature, or should the API return
   an explicit 400 and stay text-only?
5. **Retention (P7)** — is unbounded `data/media` growth acceptable for single-user deployments?
6. **Version source (U5)** — `version.json` as the single source of truth, or keep the FastAPI
   literal in sync manually?

---

*Report generated from a read-only pass over the working tree at `main`. No source files were
modified by the audit itself.*

---

## 12. Implementation status

Every finding is implemented and verified except **Q8**, which was rejected after
checking the existing tests (recorded in the "Rejected / revised" table below).

### Batch 1 — stop the bleeding

| ID | Change | Files |
|---|---|---|
| R3 | Corrupt/unusable JSON is quarantined to `<file>.corrupt-<ts>`, logged at ERROR and reported via `/admin/status` → `corrupt_files`. Fails closed with `StoreCorruptError` when the file cannot be moved aside. Wrong-shape JSON (e.g. a list where a dict is expected) is now caught at load instead of crashing on the first `.values()`. | `store.py`, `app.py` |
| R2 | `safe_chat_stream` uses a bounded `_emit()` that aborts on `stop_event`, plus `_emit_done()` which waits up to 5 s for room and then guarantees the terminal sentinel. | `app.py` |
| R4 | Git auth moved to `GIT_CONFIG_*` environment variables; `origin` is always stored token-free and a tokenised remote from an older install is scrubbed on the next run. | `app.py` |
| R7 | `VideoRequest.duration` 1–60, `timeout` 10–3600, `size`/`aspect_ratio`/`resolution` ≤ 32 chars. `ImageRequest.size`/`aspect_ratio` capped too. | `app.py` |
| R9 | `.env` created 0600 via `os.open(..., 0o600)` + explicit `chmod` before `os.replace`. | `app.py` |
| R10 | `_start_lock` covers the whole startup; body extracted to `_start_locked()`. | `engine.py` |
| R11 | `MUSE2API_CORS_ORIGINS` is honoured; empty still means "any origin". Parsing extracted to `_allowed_origins()`. | `app.py` |

### Batch 2 — concurrency correctness

| ID | Change | Files |
|---|---|---|
| R1 | Lock order is now **ACCOUNT_POOL → GEN_LOCK everywhere**. `_run_generation` no longer takes the browser slot before waiting for an account; the slot is acquired inside each attempt. Admin maintenance and browser warmup were flipped to match. | `app.py` |
| R5 | `MUSE2API_MAINTAINER_KEY` added. `/admin/repo/push` requires it and is **403 when unset** (it is not UI-driven). `/admin/update/upgrade` prefers it and falls back to the API key with a startup warning. Constant-time comparison via `secrets.compare_digest`. | `app.py`, `config.py`, `.env.example` |
| R6 | Shared `_admit_task()` caps images (`MUSE2API_IMAGE_QUEUE_MAX`, default 8) and video (`MUSE2API_VIDEO_QUEUE_MAX`, default 4) under one `TASK_ADMISSION_LOCK`. | `app.py`, `config.py` |
| P1 | Chat and Responses no longer call `store.pick_account`. `safe_chat_stream` leases through `ACCOUNT_POOL` and reports the chosen id back via `state`. | `app.py` |
| P2 | `last_used`/`use_count` are updated in memory and flushed at most once a minute (and on shutdown). | `store.py`, `app.py` |
| P3 | `MUSE2API_TASK_RETENTION` (default 200) prunes the oldest finished tasks; queued/processing tasks are never pruned. Idempotency lookup uses a key→task index instead of scanning. | `store.py`, `config.py` |
| P4 | `MUSE2API_CONCURRENCY` defaults to **1** (serialised), which is what "one Chromium process, one CDP connection" actually delivers. `config.py` and `.env.example` say so explicitly; `tests/test_config_defaults.py` pins the default, the override, the clamp and the template match. Option 2 (one engine per slot) stays deferred. | `config.py`, `.env.example`, `tests/` |
| T1 | `requirements-dev.txt`, `pytest.ini`, `tests/conftest.py` with an explicit `collect_ignore` for script-style tests. | new |
| T2 | `.github/workflows/ci.yml`: lint (compileall + node checks), unit (pytest), browser (non-blocking, real Chrome). | new |
| T3 | `tests/_browser.py` uses `config._detect_chromium()`; the three browser scripts no longer pass `None` to `Popen`. | `tests/` |
| T4 | 320 px overflow fixed (the keep-alive badge could not wrap). The full layout test now passes at every width in both languages. | `admin.html` |

### Batch 3 — quality, compatibility, polish

| ID | Change | Files |
|---|---|---|
| A1 | Video object uses `object: "video"` and `queued/in_progress/completed/failed`, reports `seconds`/`size`/`completed_at`, and adds `GET /v1/videos/{id}/content` and `DELETE /v1/videos/{id}`. | `app.py` |
| A2 | `GET /v1/models/{model}` with alias resolution. | `app.py` |
| A3 | `usage` is estimated (~4 chars/token) instead of a hard zero, on chat, streaming chat and Responses. | `app.py` |
| A4 | Video honours `Idempotency-Key`, namespaced as `video:<key>` so it cannot collide with image keys. | `app.py` |
| A5 | `/v1/responses` gains the same opt-in tool protocol as chat: prompt injection, `function_call` output items, and `function_call_arguments` stream events. With the protocol off, `tools` is ignored exactly as chat does. | `app.py` |
| A6 | Prefix classification (`gpt-image*`, `sora*`, `veo*`, …) after the exact-alias table. | `app.py` |
| A7 | `/v1/models` entries carry `context_window`, `supports_streaming`, `supports_vision`, `modalities`. | `app.py` |
| A8 | `Idempotency-Key` and `Idempotent-Replayed` echoed on image and video creates. | `app.py` |
| Q1 | The "remove all text/watermark/logo" clause is skipped when the prompt asks for text (`_requests_text()`); pure cleanup requests keep it. | `app.py` |
| Q2 | Aspect ratio/size is parsed and mapped to the nearest canonical ratio, covering `1024x1792`, `1792x1024`, `1536x1024`, `2048x2048`. Video orientation uses the same classifier; `auto`/`default`/`any` mean unconstrained. | `app.py` |
| Q3 | `n>1` returns 400 with an explanation instead of being silently ignored (all image endpoints). | `app.py` |
| Q4 | `size` reports the dimensions Muse actually produced when captured. | `app.py` |
| Q5 | Chat and Responses return 400 for image content instead of flattening it to `[图片]`. | `app.py` |
| Q6 | Video duration wording softened to `时长约 N 秒`, and the produced clip is measured with `_mp4_duration` and reported. | `app.py` |
| Q7 | Every prompt now carries exactly **one** canonical "use only this request's attachments" sentence, appended as the **last** instruction. The five ad-hoc wordings were removed from the branch texts. Measured first: the audit's premise ("repeated 1–3× per prompt") was wrong — each prompt already contained at most one mention — but the phrasing varied per branch and sat mid-sentence, so the recommendation still applied. | `app.py` |
| R8 | Constant-time key comparison (done in Batch 2) plus a `limit_req_zone` block in the nginx example. `tests/test_deployment_artifacts.py` asserts the zone is declared **and** applied, and that clients get 429 rather than nginx's default 503. | `deploy/nginx.example.conf`, `tests/` |
| R12 | Extension zip is built once per change instead of per request (endpoint stays unauthenticated because the admin page links to it directly). | `app.py` |
| R13 | Container runs as an unprivileged user (`UID`/`GID` build args, default 1000) and has a `HEALTHCHECK`. **Verified by building and running it**, not just by parsing the YAML — see "Container verification" below. Existing installs must `sudo chown -R 1000:1000 data` once; the migration is documented in the README and an unwritable data dir now produces an actionable startup error instead of a bare traceback. `tests/test_deployment_artifacts.py` asserts the non-root `USER`, the created group/user, the UID/GID build args, the healthcheck target and the compose healthcheck. | `Dockerfile`, `docker-compose.yml`, `README.md`, `app.py`, `tests/` |
| U1 | The page is hidden until the English pass runs, with a 2 s fallback so a script error cannot leave it blank. | `admin.html` |
| U2 | New i18n gate: every `data-i18n` has an English block, no orphans, no Chinese inside English blocks, no duplicate keys, all patterns compile. It immediately found and removed a duplicate key. | `tests/test_admin_i18n.py` |
| U3 | Only `muse.ai` is requested at install time; the service origin is requested at runtime via `chrome.permissions`. | `extension/` |
| U4 | Documented where the extension stores the API key and why the maintainer key matters. | `extension/README.md` |
| U5 | FastAPI version is read from `version.json`; the OpenAPI version can no longer drift. | `app.py` |
| U6 | Tabs are deep-linkable via `location.hash` with back/forward support. | `admin.html` |
| U7 | README gained a Language section (English default, zh-CN preserved). | `README.md` |
| P5 | Attachment polling is adaptive (0.2 s while changing, backing off to 0.6 s). `_scroll_bottom()` still runs every tick because Muse lazily renders attachments into view. | `engine.py` |
| P6 | `Store.batch_writes()` collapses a keepalive sweep into one `accounts.json` flush; imports and deletes force an immediate write. | `store.py`, `app.py` |
| P7 | `MUSE2API_MEDIA_RETENTION_DAYS` (default 0 = off) with a startup and daily sweep that never deletes files referenced by a running task. | `app.py`, `config.py` |

### Rejected / revised after checking the code

| ID | Outcome |
|---|---|
| **Q8** | **Rejected.** The marker regex is deliberate: `tests/test_reference_media.py:99` asserts that a plain two-image request must *not* include the attachment legend. Making it unconditional broke that test, so the conditional was restored and the test kept as the specification. |
| **A5** | **Revised.** `instructions` was already mapped by `_responses_messages`; only the `tools` path was missing. |
| **R12** | **Revised.** The two helper endpoints stay unauthenticated because the admin page links to them with a plain `<a href>` that cannot carry a Bearer header. The actual issue (rebuilding the zip per request) is fixed by caching. |
| **Q7** | **Premise corrected, recommendation implemented.** Measurement across 11 branch combinations showed **at most one** history constraint per prompt, not the 1–3 the audit claimed, so "repetition" was not a real defect. What *was* real: five different wordings (`严禁使用历史图片`, `严禁参考任何历史图片`, `请勿查找历史相册`, `严禁使用历史素材`, `严禁使用历史图片或任何其他图像`) embedded mid-sentence. These were replaced by one constant (`_NO_HISTORY_MARKER`) appended last, which is both more consistent and mechanically testable. |

### New tests

`test_store_corruption.py`, `test_stream_backpressure.py`, `test_prompt_quality.py`,
`test_request_validation.py`, `test_start_lock.py`, `test_lock_order.py`,
`test_account_selection.py`, `test_admin_auth.py`, `test_admission.py`,
`test_store_retention.py`, `test_video_api.py`, `test_models_and_usage.py`,
`test_responses_api.py`, `test_admin_i18n.py`, `test_hardening_batch3.py`,
`test_config_defaults.py`, `test_deployment_artifacts.py`.
Q7 added four cases to `test_prompt_quality.py`: one canonical occurrence per prompt
across 12 branch combinations, no legacy phrasing, the rule is the trailing instruction,
and the wording differs for with-refs vs no-refs.

### Container verification (R13)

Run against a real Docker engine (Docker Desktop 29.8.0, linux/arm64), not a YAML parse:

```
docker build -t muse2api:audit-test .                      exit=0
docker run -d --name muse2api-audit -p 18699:18610 \
           -v "$DATA:/app/data" muse2api:audit-test
health=healthy
uid: 1000  user: muse2api
healthz: {"status":"ok","time":1793982917}
models:  {"object":"list","data":[{"id":"muse-spa...
image config user: 1000:1000
write test on the bind mount: uid=1000, file owned by muse2api
```

Negative case — the read-only data dir an old root install would produce:

```
health=unhealthy
ERROR muse2api: Cannot create the data directories under /app/data
  ([Errno 30] Read-only file system: '/app/data/media'). If you upgraded from a
  container that ran as root, hand the data directory over once:
  `sudo chown -R 1000:1000 data` (or rebuild with MUSE2API_UID/MUSE2API_GID).
```

So the failure is loud (container unhealthy, explicit remedy) rather than a silent
"healthy but cannot import accounts". The one-time migration is in the README's Docker
section, and `docker-compose.yml` passes `MUSE2API_UID`/`MUSE2API_GID` through to the
build args for hosts that are not uid 1000.

### Verification run

- `pytest` in a venv built from `requirements-dev.txt`: **156 passed**.
- Every standalone script passes, including the three browser ones
  (`test_admin_layout`, `test_media_selection`, `test_reference_upload_browser`) and
  `test_async_images`, whose lock assertion was updated to the new contract.
- `python -m compileall`, `node --check extension/popup.js`,
  `node tests/test_admin_api_error_messages.mjs`, and `git diff --check`: clean.
- `test_admin_layout.py` reports `bodyWidth <= viewport` at 320/390/768/1280/1366/1440/1536/1920/2560 px in both languages.
