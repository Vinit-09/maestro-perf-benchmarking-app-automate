# Cloud Maestro (BrowserStack App Automate v2) — Operating Gotchas

Field-collected list of platform behaviors that bite when running benchmark builds
against BrowserStack Maestro v2 (Android, Maestro 1.39.13). Add new entries as
they're discovered, with the build IDs that surfaced them.

---

## 1. `Instrumentation stalled` on cold `launchApp` — device-pool-specific, not BS-platform-wide

The `Instrumentation stalled` pattern is **specific to the OnePlus 12R-14.0 pool**.
The same script + flow + APK ran cleanly on Samsung Galaxy S25-15.0 (90-rep and
120-rep probes passed, plus a 150-rep run started cleanly) in the same time
window where OnePlus 12R-14.0 had a 100 % stall rate on 5 consecutive probes.

So this is a device-pool health issue (OnePlus 12R-14.0 in ap-south, 2026-05-11),
not a BS-Maestro-platform issue. Other device specifiers may not be affected.

**Do not confuse with #3 (the ~940-s wall-time cap).** Both end with
`session.status: failed` + empty stacktrace, but the fingerprints are
distinct:

| Field | Stall (#1) | Cap kill (#3) |
|---|---|---|
| session.status | `error` | `failed` |
| session.duration | 36–65 s | 940–975 s |
| Reps completed (in log) | 0 | dozens to ~125 |
| Maestro log last line | at or before `Launch app … RUNNING` | inside the loop, mid-iteration |

`OnePlus 12R-14.0` is not a single physical unit. BS load-balances across multiple
physical units in `ap-south`. Hostnames observed:

| Physical hostname | Observed outcomes |
|---|---|
| `5ac9eaa9` | Passed 100-rep smoke at 933 s |
| `5cbb0293` | **Mixed** — passed iterations for 108 reps (148-rep run) earlier; later errored at 36 s on launchApp (same day, ~3 h later) |
| `56c5aa31` | Errored at 55 s on launchApp |
| `b33588eb` | Errored at 42–65 s on launchApp — seen 4+ times |
| `d19684da` | Errored on launchApp |

**Initial hypothesis** (hostname-deterministic bad units) was wrong. Same
hostname can pass earlier and fail later. Stalls appear to be **time-correlated**
— both 2026-05-11 morning probes (90-rep retry on `5cbb0293`, 120-rep on
`56c5aa31`) errored within 30 s of each other, suggesting a transient BS
infra-load condition.

**Impact:** identical script + identical APK + identical device specifier give
different outcomes session-by-session. Observed ~25–47 % stall rate on
`OnePlus 12R-14.0` in a 100-session run on 2026-05-11. Probe-of-1 smoke runs are
unreliable — they may all error in a bad time window even though steady-state
pass rate is higher.

**Diagnosis fingerprint** for an `Instrumentation stalled` session:
- `status: error`
- `duration: 36–65 s`
- `metadata.stacktrace: "Instrumentation stalled"`
- `metadata.maestro_status: "UNKNOWN"`
- Maestro log stops at or before `Launch app "org.wikipedia.alpha" RUNNING`
- Device log: 0 bytes

**Mitigation:** no direct fix. Retry the failed session — BS may assign a
different unit and/or the transient condition may have cleared. There's no API
to blacklist hostnames or pin to a known-good unit. For probe-style runs
(N=1), expect multiple retries to be necessary in bad windows.

**Evidence builds:**
- `d1440dacc873396e92f5a86808229fbe6b696741` (100-session run, ~47 % stall rate)
- `f3c2c50ae53c8030fdb113cdd8b3878eb5517d46`, `9ebd74b85679e1d21b48a6710dec6e75d3ab245d`,
  `d744cf6cf3612e4aeb25252baa492fe3a7f7e091`, `25879773a971d2bc47eee0b2c37905610d6432cc`,
  `1d0aabce88f052ef6949dcb863ae91d34eee9054` (smoke stalls)

---

## 2. Running builds cannot be stopped via the public API

All documented stop variants return 422 or 404:

| Verb + path | Result |
|---|---|
| `DELETE /maestro/v2/builds/{id}` | 422 `BROWSERSTACK_BUILD_DELETION_UNSUCCESSFUL` |
| `POST /maestro/v2/builds/{id}/stop` | 404 HTML |
| `PUT /maestro/v2/builds/{id}/stop` | 404 HTML |
| `PATCH /maestro/v2/builds/{id}` `{"action":"stop"}` | 404 HTML |
| `PUT /maestro/v2/sessions/{id}` `{"status":"stopped"}` | 404 |

**Mitigation:** stop builds from the BS App Automate web dashboard — that does
work. Verified 2026-05-11 against build `1df08d8993d3c0587e4fba0ada8fd55ceb696de5`.

---

## 3. Cap on Maestro test execution time at `tc.duration` ≈ 900 s (independent of `idleTimeout`)

BS Maestro v2 kills a single Maestro test once Maestro test execution time
(`tc.duration`) reaches ~880–915 s. Corresponding `session.duration` at kill
is ~940–975 s (BS adds ~60 s of pre/post-test overhead). The cap is real,
applies across device pools (verified on both OnePlus 12R-14.0 and Samsung
Galaxy S25-15.0 on 2026-05-11), and **fires regardless of whether
`idleTimeout` is set in the build payload.**

**Key separation from the `idleTimeout` build-payload key:** the cap-kill
behavior is the same whether or not `idleTimeout: 900` is in the payload —
build `1687bb73f49b012d4df935bc0e6fbf65d83832e5` (idleTimeout=900) was killed
at tc.dur ~889 s; build `f3c2c50ae53c8030fdb113cdd8b3878eb5517d46`
(idleTimeout not set) was killed at tc.dur ~883 s. The `idleTimeout`
capability's documented purpose is "max time between commands" — we have not
tested flows with intentional idle gaps, so we cannot say what that key does
on Maestro v2. We can only say it does not lift the observed
execution-time cap.

`sessionTimeout: 1800` is silently dropped (not echoed in
`input_capabilities`) and does not lift the cap either.

**Observed boundary** (with measured `tc.duration` for passing runs;
`tc.duration` is 0 in BS metadata for killed sessions because the test
process is killed before terminal state is written — values shown for kills
are `session.duration − 60 s` typical overhead):

| Build | idleTimeout | Reps | tc.duration | session.duration | Outcome |
|---|---|---|---|---|---|
| `67ee914a878eb77ebdd018767d89c18e814f482f` | not set | 90  | 656 s | 704 s | ✅ passed |
| `d6b3adda8bdeee0e5538819e625792bfffbbef06` | not set | 100 | 738 s | 812 s | ✅ passed |
| `0c714895150f32bb9f49af54246887128c616d9d` | **900**  | 100 | 779 s | 845 s | ✅ passed |
| `99235cebf5384a0e6cb227c533e708ea60457ee9` | not set | 120 | **873 s** | **933 s** | ✅ passed (closest) |
| `f3c2c50ae53c8030fdb113cdd8b3878eb5517d46` | not set | 148 | ~883 s | 943 s | ❌ killed (108 / 148) |
| `81fd9404bbb956397d39e6f5b2277ff7135cd321` | not set | 150 | ~884 s | 944 s | ❌ killed (124 / 150) |
| `1687bb73f49b012d4df935bc0e6fbf65d83832e5` | **900**  | 132 | ~889 s | 949 s | ❌ killed (111 / 132) |
| `3d79ca031c8108615e6d1f43852eb6d052e4a545` | not set | 148 | ~914 s | 974 s | ❌ killed (125 / 148) |

So the cap sits at **`tc.duration` ≈ 900 s** (kill band 883–914 s), which is
**`session.duration` ≈ 940–975 s** with BS overhead. Plan for ≤ ~870 s of
expected `tc.duration` per Maestro test.

**Kill fingerprint** (any device, any device pool):
- `session.status: failed`, `session.duration: ~940–975 s`
- `tc.status: failed`, `tc.duration: 0`
- `metadata.completed_flows: 0`, `metadata.total_flows: 0`
- `metadata.maestro_status: "UNKNOWN"`, `metadata.stacktrace: ""`
- `session.error.message`: absent
- Maestro log stops mid-iteration; device log stops a few seconds later
- The actual reps completed are visible only via grep on the maestro log
  (`grep -c "Software company based in India.*COMPLETED"`)

**Mitigation:** keep a single Maestro test's expected test-time under ~870 s.
If the workload needs more, split into multiple `.yaml` files in the same
test_suite zip — BS runs them as separate tests, each with its own ~940-s
budget, all aggregated under one session.

**Mitigation:** keep a single Maestro test's expected `tc.duration` under
~870 s (≤ ~930 s session.duration). If the workload requires more, the only
confirmed path is to request a higher cap from BS support. Splitting the flow
into multiple `.yaml` files in one test_suite zip is plausible (BS would
likely apply the cap per test) but **not verified by our experiments**.

**What we have NOT tested — open questions:**

1. Whether `idleTimeout` does what its name suggests (max time between
   consecutive commands) on Maestro v2. All our flows had < 1 s between
   commands, so any idle-timer behavior would not have fired. A flow with an
   intentional 120-s+ idle paired with `idleTimeout: 60` is needed to test.
2. Whether the cap is the documented `idleTimeout` mechanism enforced as
   wall-time, a separate platform-level execution-time cap, or another
   mechanism. Our data shows behavior, not mechanism.
3. Whether splitting into multiple `.yaml` files inside the test_suite zip
   gives each its own ~900-s budget.
4. Whether the cap is identical on iOS Maestro v2.

**Confusing behavior when the cap is hit:**

- `session.status: failed`
- `session.duration: 940–975 s`
- `metadata.stacktrace: ""` (empty)
- `metadata.maestro_status: "UNKNOWN"`
- `metadata.completed_flows: 0 / 0` (test process killed before writing terminal state)
- `session.error.message`: absent
- Maestro log stops mid-iteration (e.g., right after `Input text BrowserStack RUNNING`) — **no error, no exception, no warning**
- Device log also stops within a few seconds of maestro log
- BS attributes no reason for the failure

**Sized to fit:** for the OnePlus 12R-14.0 + Wikipedia Alpha + selector-based
search loop, 100 reps is ~812 s — fits. 120+ reps cross the cap.

**Mitigation:** keep the longest single Maestro test under ~870 s of expected
wall-time. If the workload requires more, split into multiple `.yaml` files in
the same test_suite zip; BS treats each as a separate test, each with its own
900-s budget.

**Evidence builds:**
- `f3c2c50ae53c8030fdb113cdd8b3878eb5517d46` (148 reps killed at 943 s, 108 reps completed)
- `3d79ca031c8108615e6d1f43852eb6d052e4a545` (148 reps with `sessionTimeout: 1800` silently dropped — same 974 s kill, 125 reps completed)
- `1687bb73f49b012d4df935bc0e6fbf65d83832e5` (per repo comment, 132 reps killed at ~910 s, 111 reps completed)
- `d6b3adda8bdeee0e5538819e625792bfffbbef06` (100 reps passed at 812 s — under the cap)
- `0c714895150f32bb9f49af54246887128c616d9d` (100 reps passed at 845 s — under the cap)

---

## 4. Unknown build-payload keys are silently dropped

BS Maestro v2 only echoes back recognized keys in `input_capabilities`. Anything
else you pass is dropped without a warning, success indicator, or error.

**Verified silent drops:**
- `sessionTimeout: 1800` — not recognized; cap was still enforced at ~970 s
- (likely also: any other guessed name that isn't in BS Maestro v2 docs)

**Mitigation:** after triggering a build, fetch `GET /maestro/v2/builds/{id}` and
inspect `input_capabilities`. If your key isn't there, it wasn't honored.

---

## 5. `BS spawned N sessions` snapshot lies

The script's "BS spawned X sessions (requested Y)" line samples the build
response right after creation. BS attaches sessions lazily as devices free up,
so the first snapshot often shows 1–4 even when you requested 100.

**Don't trust this number as a final count.** Wait several polls and re-sum
across all devices.

---

## 6. Build status lags session status

When a build is stopped (manually or by timeout), individual sessions flip to
`error` immediately but the build-level `status` field continues to report
`running` for several minutes until the last in-flight session terminates.

**Implication for the script's poll loop:** don't break on session-level
completion. Wait for build `status != running` and `status != queued`.

---

## 7. `error` status conflates two failure modes

A session marked `error` can mean either:

| Origin | Fingerprint |
|---|---|
| Real launch failure (bad physical unit) | `duration: 42–65 s`, stacktrace `Instrumentation stalled` |
| Stopped/aborted by build cancellation | `duration: None` (never started) or duration cut short |

When triaging a finished build, bucket by duration before drawing conclusions
about pass rate.

---

## 8. Test suite must be re-uploaded when flow YAML changes

`_cloud_cache.env` stores the previously-uploaded `app_url` and `test_suite_url`.
The `-s` flag reuses these. If the flow YAML changed, `-s` will deploy the OLD
flow.

**Mitigation:** only pass `-s` when neither APK nor flow YAML has changed since
the last upload. Otherwise omit it and let the script re-upload.

---

## 9. `_cloud_cache.env` is shared between iOS and Android runners

`cloud_run_android.sh` and `cloud_run_ios.sh` both write to the same cache file
at `results/_cloud_cache.env`. Running iOS after Android (or vice versa) and
then using `-s` will silently deploy the wrong platform's APK + test suite.

**Mitigation:** never use `-s` across platform changes. Track the last platform
used yourself, or split the cache per script.

---

## 10. API path inconsistencies

| API | Host | Notes |
|---|---|---|
| Build / session metadata | `api-cloud.browserstack.com` | `/maestro/v2/builds/{id}` and `/maestro/v2/builds/{id}/sessions/{id}` work; bare `/maestro/v2/sessions/{id}` returns 404 |
| Test artifacts (logs, screenshots, video) | `api.browserstack.com` (no `-cloud`) | URL embedded in the session detail's `testcases[].data[].testcases[].maestro_log` / `device_log` / `commands_log` fields. Keyed by `tc_id`, not session id. |

**Mitigation:** always pull the per-session detail first to get the `tc_id` and
the canonical log URLs, then fetch artifacts from those exact URLs. Don't
hand-construct artifact URLs.

---

## 11. Maestro log endpoint flakes for some passed sessions

Right after a session passes, `/maestrologs` sometimes returns `404 Not Found`
HTML. Retrying a few minutes later usually works.

**Mitigation:** if you need the log, retry on backoff. Build the report from
session metadata first, fetch logs on demand.

---

## 12. Device log is empty for early-failed sessions

For sessions that die during the cold `launchApp` (the "bad unit" pattern), the
device log is 0 bytes — device-level logcat collection only starts after the
launchApp handshake completes. Use only the maestro log to diagnose these.

---

## 13. BS plan parallel cap shapes wall time

`GET /app-automate/plan.json` returns `parallel_sessions_max_allowed`. Our
account: **25**. For a 100-session request, only 25 run at a time, the rest
queue. Wall time for a 100-session benchmark is roughly:

  ceil(N_sessions / parallel_cap) × per_session_wall_time
  = 4 × ~14 min = ~56 min (best case, no unit-stall retries)

---

## 14. Operational note — `path` is reserved in zsh

Avoid using `path` as a loop variable in shell snippets — zsh treats `$path` as
an array view of `$PATH`, and assigning to it nukes your PATH for the rest of
the shell.

---

## 15. Poll-line `passed=N` noise

`cloud_run_android.sh`'s poll loop emits a status line every 30 s containing
`passed=N failed=N error=N`. A naive `grep "passed="` filter on the log floods
any monitor with one event per poll. Filter on terminal sentinels only:

    grep -E "build_id=|all builds terminal|^    passed=|Sessions written|ERROR:"

(Note the leading whitespace on `^    passed=` — that pattern matches only the
final summary line, not the per-poll status lines.)

---

## 16. Pre-launchApp watchdog kill at ~125 s — distinct from #1

A failure mode where BS Maestro v2 kills a single tc ~120–130 s after Maestro
has selected the device but before any flow command (including `launchApp`)
is issued. Visually similar to #1 (instrumentation stall), but the
fingerprints are different and the kill is later. Documented on
2026-05-18 against build `8ed0b0edc7e137a39670472e4855b521e04a6889` (100-session
multitest baseline on Samsung Galaxy S24-14.0, tag `multitest-100x-baseline`,
1/100 sessions affected).

**Diagnosis fingerprint:**

| Field | Stall (#1) | Pre-launchApp watchdog (#16) | Cap-kill (#3) |
|---|---|---|---|
| session.status | `error` | `error` | `failed` |
| session.duration | 36–65 s | **~320 s** (covers 2 tcs at ~125 s each + setup) | 940–975 s |
| Per-tc `duration` (metadata) | 0 | **0** | 0 (process killed before terminal) |
| Per-tc `stacktrace` (metadata) | `"Instrumentation stalled"` | **`""` (empty)** | `""` (empty) |
| Per-tc `metadata.status` | `UNKNOWN` | **`ERROR`** | `UNKNOWN` |
| Maestro log last line | at or before `Launch app … RUNNING` | **`Selected device <hostname> using port …`** (never reaches `Launch app`) | mid-iteration |
| Device log | 0 bytes | 0 bytes (logcat collection not yet active) | populated, stops a few seconds after maestro log |
| BS video `t=` window per tc | n/a | **~127 s per tc** (e.g., `t=6,133` and `t=148,273`) | full ~940 s |
| session.error.message | absent | **`"Could not start a session : Something went wrong during test execution. Please try to run the test again."`** (generic BS fallback) | absent |
| Reps completed | 0 | **0** | dozens to ~125 |

The key separator is **session.duration** and the maestro-log stop point.
A #1 stall dies during launchApp at <80 s with a populated stacktrace. A
#16 watchdog kill dies after device selection but before any flow command,
at ~125 s per tc, with an empty stacktrace. Both share `duration=0` (metadata)
and `completed_flows=0/0` — fingerprint shape alone is not enough; you need
the wall-time and the maestro-log content.

**Hypothesis:** BS enforces an internal ~120 s watchdog for "no Maestro flow
command issued since device selection." When the device-side environment
prevents Maestro from issuing the first command in that window (background
process spike, OS-level prompt, slow boot, transient APK install hiccup), the
watchdog fires and BS marks the tc errored. Both tcs in a `multitest`-style
session are affected because BS routes a session's tcs to the same physical
unit serially — if the unit isn't usable for tc-1, tc-2 hits the same fate.

**Pool-allocation observation:** the failing unit appears as a single
hostname in the build (e.g., `RZCX60PWTST` in the evidence build). A sample
of 8 passed sessions in the same build hit 8 distinct hostnames — BS does
spread sessions across many physical units. The pattern looks like a
one-off bad-unit allocation rather than a pool-wide health issue. With a
larger sample of failing builds we may see hostnames repeat or correlate to
specific time windows — track in future runs.

**Mitigation:** no direct fix. Retry the failed session (re-trigger a single-
device build, or re-run that session via the dashboard). The same
hostname may not be re-allocated.

**Reporting:** for benchmark runs, exclude #16-pattern sessions from
percentile aggregation the same way #1-pattern sessions are excluded — the
session has no successful per-rep timings to contribute. Surface the
incident count in the report's methodology / "Build health" section
alongside #1 incidents.

**Evidence builds:**
- `8ed0b0edc7e137a39670472e4855b521e04a6889` (session `2d629ae8da089233fac20b2a72a80d0012152a6a` on hostname `RZCX60PWTST`, 1 incident in 100 sessions, 2026-05-18)

---

## Adding new entries

Append in numbered order. Each entry should have:

1. **One-line summary** of the behavior.
2. **Diagnosis fingerprint** — exact field values or log signatures that let
   someone recognize the same issue.
3. **Mitigation** — the workaround, or "no fix" if there isn't one.
4. **Evidence build IDs** — the BS build(s) that surfaced it.
