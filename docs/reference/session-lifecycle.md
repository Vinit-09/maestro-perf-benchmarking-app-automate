# Session Lifecycle — Appium vs Maestro, Android & iOS

A walk-through of what actually happens from the moment you press "go" to the moment the test session ends. Written for someone reasoning about benchmarks, not someone implementing automation. Uses the same hotel analogy throughout — pick it up once, follow it through.

---

## The hotel analogy in one paragraph

**BrowserStack is a hotel chain.** Each device in the cloud is a hotel room. A test session is a guest's stay in that room. You — the test author — are the guest with luggage (your app and your test scripts). When you want to run a test, you're effectively asking the front desk to: book a room, carry your luggage in, set up your testing gear inside, then carry out a checklist of activities, then check you out and clean the room for the next guest. Everything else in this document is just a more detailed look at that flow.

| Real thing | Hotel analogy |
|---|---|
| BrowserStack App Automate | The hotel chain |
| Device (e.g., iPhone 17, OnePlus 9R) | A specific hotel room |
| Build | One reservation that may book multiple rooms at once |
| Session | A guest's stay in one room |
| Parallel-slot ceiling on your plan | Maximum rooms your membership lets you book simultaneously |
| Queue | Waiting in the lobby for a room to free up |
| App (.ipa / .apk) | Your luggage |
| Test suite (Maestro flows / Appium scripts) | Your itinerary or to-do list for the stay |
| Region (ap-south, us-east, etc.) | Which city's hotel they put you in |

---

## Phase 1 — Reservation (build trigger)

**What it is, in plain words:** You tell BrowserStack "I want to run a test." You hand over your app, your test scripts, and say which device(s) you want.

**Hotel analogy:** You walk up to the reception desk and say, "I'd like to book one of your rooms tonight; here's my luggage and my schedule for the visit."

**What happens technically:** A POST request to BrowserStack's REST API with your app URL, test-suite URL, device list, project tags, and capabilities (settings).

**Time spent:** Milliseconds — it's just submitting the request.

**Where it shows up in metrics:** Not measured directly; it's the moment the clock starts.

---

## Phase 2 — Queue (waiting time)

**What it is, in plain words:** If all the rooms your plan allows are already in use, you wait until one frees up.

**Hotel analogy:** The hotel says, "Sorry, all the rooms your gold-card membership covers are occupied. Have a seat in the lobby; we'll call you when one's ready."

**Reasons you might wait:**
- All your parallel slots are in use (most common).
- The specific device you asked for is busy.
- Your app is still being processed by background services (signing, etc.).
- The region you'd land in has no free devices.

**Where it shows up in metrics:** This is the **`waiting_time`** P0 metric in the spec. In BigQuery, it appears as flags: `has_queued_device_tier`, `has_queued_async_signing`, `has_queued_soft_nta`, with corresponding `_time` fields giving the milliseconds spent waiting in each reason bucket.

---

## Phase 3 — Room assignment (device allocation)

**What it is, in plain words:** A free room is picked for you. Sometimes it's in your preferred city; if your preferred city is full, they put you in another city's hotel of the same brand.

**Hotel analogy:** "Room 314 in our Mumbai property is yours. Wait — Mumbai is full. We're moving you to Singapore." That cross-region thing is exactly what we observed in the 100-session benchmark — 28% of sessions got pushed out of `ap-south-1`.

**Where it shows up in metrics:** Not a separate timer in BQ, but the **device_region** field tells you which city's hotel you ended up in. The spec's `cross_region` flag is supposed to mark when this differs from your home region (in our run, BS reported `false` for everyone even though devices were scattered across continents — known data-quality gap).

---

## Phase 4 — Luggage retrieval (app download)

**What it is, in plain words:** Your app file (the `.ipa` or `.apk`) needs to actually arrive at the room. If BrowserStack's storage server already has it cached near the device, this is fast. If not, it gets pulled from the central blob store.

**Hotel analogy:** A bellhop fetches your luggage from the storage facility. If you stayed here recently and your suitcase is in the closet on this floor, takes seconds. If they have to send a courier across town to fetch it, takes minutes.

**Where it shows up in metrics:** **`app_download_time`** (P1 supporting metric in the spec). In our cloud iOS run, P50 was ~0.07 s (mostly cached), P90 was ~0.86 s, max was 90 s (very cold cache).

---

## Phase 5 — Unpacking the luggage (app install)

**What it is, in plain words:** The app file gets unpacked and installed onto the actual device.

**Hotel analogy:** The bellhop opens your suitcase and arranges your clothes in the closet, your toothbrush in the bathroom, etc.

**What can go wrong / make it slow:**
- The device's flash storage is slow.
- The OS does extra integrity checks on the app.
- App Verifier (on Android) or Play Protect dialogs may interrupt the flow.
- iOS does a "main app unarchive" step (visible as `main_app_unarchive_time` in BQ).

**Where it shows up in metrics:** **`app_install_time`** (P1). In our cloud iOS run, P50 = 1.6 s, P90 = 3.7 s.

---

## Phase 6 — Setting up your testing equipment (test suite download + install)

**What it is, in plain words:** Your *test scripts* are a separate package from the app. They also have to arrive and be unpacked.

**Hotel analogy:** A second bellhop brings your itinerary and the special equipment you need for your activities (your gym bag, your camera, your laptop). Sets it all up on the desk.

**Where it shows up in metrics:** **`test_download_time`** + **`test_install_time`** (both P1). Same caching pattern as the app — if you reused the same `bs://test-suite-url` across sessions, download is ~0 ms. Install on iPhone 17 was P50 2.4 s, P90 6.8 s.

---

## Phase 7 — Training the room staff (driver install)

**What it is, in plain words:** Real automation needs a "robot helper" running on the device that can actually tap, swipe, and read the screen on your behalf. This helper is its own little app, and it has to be installed first.

**Hotel analogy:** Before they can carry out your itinerary, the front desk has to train (or update) a robot butler in your room. The butler reads your itinerary, watches for instructions from you, and performs actions on the room's furniture, lights, etc.

**This is where Appium and Maestro really differ:**

- **Appium Android**: installs a helper called **UiAutomator2 server** (uses Android's built-in accessibility framework).
- **Appium iOS**: installs **WebDriverAgent** plus a small **XCUITest** test runner Apple provides.
- **Maestro Android**: installs **`maestro-driver-android.apk`** plus an instrumentation app of its own.
- **Maestro iOS**: installs an **XCTest UI runner** packaged with Maestro's driver code.

In all four cases, this is a one-time-per-session install of a "robot." If the device has stale leftovers from a previous tenant, the install is fast. Otherwise it's slow.

**Where it shows up in metrics:** For Maestro, it's specifically `install_maestro_ui_runner_app` inside the `data` JSON field. P50 was 2 s, P90 was 8.4 s, max was 77 s — that 38× variance is the worst-case "fresh device, full driver install" path.

---

## Phase 8 — Private phone line (tunnel setup, only if Local is enabled)

**What it is, in plain words:** If your app talks to a server only reachable from inside your office network (e.g., a staging server), BrowserStack runs a secure tunnel from the device back to your machine.

**Hotel analogy:** The hotel sets up a private phone line from your room to your home office, so you can call back to systems only your home network can reach.

**For our iOS cloud benchmark:** Local was off, so this phase was effectively a no-op (`tunnel_setup` ≈ 2 ms across all sessions).

---

## Phase 9 — "Staff is ready" (firecmd / start_sessions)

**What it is, in plain words:** Everything is set up. The room is yours, your luggage is unpacked, the robot butler is trained and waiting, and the front desk says, "Your stay officially begins now." This is the boundary between BrowserStack-side overhead and your actual test running.

**Hotel analogy:** The bell rings. Your stay clock officially starts.

**Where it shows up in metrics:** **`firecmd_time`** in the spec (P0 metric — the "start_time" the spec asks for). It's the *total* of phases 4–8: download + install of app + test suite + driver + tunnel setup. Everything BrowserStack does to get from "I got your reservation" to "ready to run your test." In our iOS cloud run, P50 was 12.4 s, P90 was 23 s.

---

## Phase 10 — Your stay (test execution)

**What it is, in plain words:** The actual testing happens. The robot butler follows your itinerary one item at a time. **This is where Appium and Maestro feel very different.**

### Appium: the chatty butler

You write your test in real code (Java, Python, JavaScript, etc.). Each line of your code that does something on the device sends one request, like a phone call to the room: "robot butler, tap this button"... wait for confirmation... "now type this text"... wait for confirmation... and so on, hundreds of times per test.

**Hotel analogy:** You're sitting in the lobby with a phone, and you call the robot butler in your room for *every single thing* you want done: "Open the curtains." "Turn on the TV." "Tune to channel 5." "Increase the volume." Each call has overhead — picking up the phone, hello, talk, hang up. But you have full control mid-call: if you don't like what the TV is showing, you can change your mind on the next call.

**Why it's slower per command but more flexible:** Every command crosses a network boundary (HTTP request from your code → BrowserStack's hub → the device's automation server). But you can branch logic, react to state, retry, etc. — all the things real programs do.

### Maestro: the checklist butler

You write your test as a YAML list of steps (no real code, no HTTP per step). The whole list gets handed to a runner *on the device*, and the runner walks through it.

**Hotel analogy:** You hand the robot butler a printed checklist:
1. Open curtains.
2. Turn on TV.
3. Tune to channel 5.
4. Volume +10.

The butler does items 1–4 in sequence without you on the phone. You're not even in the lobby — the work happens autonomously in the room. Faster per step (no phone tag) but you can't change your mind mid-checklist.

**Why it's faster per command but less flexible:** Commands are batched, executed locally on the device. No HTTP per step. But you can't react to runtime state from external code; conditionals have to be in the YAML itself.

### What this means for benchmarks

| Aspect | Appium | Maestro |
|---|---|---|
| Per-command overhead | Higher (HTTP round-trip per command) | Lower (in-process or short-link) |
| Granularity in BQ | **Very fine** — every command is a row in `app_automate_performance_data_partitioned.command_data`, with arrays of durations and statuses per command type | **Coarse** — Maestro doesn't write per-command rows to BQ; per-command timing only exists in the session's S3 log |
| Hooks (`@Before`/`@After`) | Yes — they're test-framework concept (JUnit/TestNG/Mocha), each hook is more WebDriver calls | **No** — Maestro doesn't have hooks; closest analog is `onFlowStart` / `onFlowComplete` config |
| Branching mid-run | Yes (real code) | Limited (YAML conditionals only) |
| Typical command count | 50–500 per test | 5–100 per flow |

**Where execution time shows up in metrics:** This is the **`execution_time`** / session duration, the spec's main P0 metric. For Maestro it's the BQ `duration` field (in seconds). For Appium it's `customer_session_duration` (in milliseconds — and *not* populated for Maestro, which is why we use `duration` instead).

---

## Phase 11 — Checkout (stop time)

**What it is, in plain words:** Your test is done. Time to clean up: video upload, log upload, app uninstall, screenshots packaged.

**Hotel analogy:** You leave the room. Housekeeping comes in: takes photos for inventory, moves out your luggage, tidies up. All of this takes time, and it's billed to your stay (or rather, it stretches the *build duration* even though you the guest are already gone).

**Where it shows up in metrics:** **`stop_time`** in the spec, **`product.performance.total_stop_time`** in BQ. **Important caveat:** for Maestro sessions specifically, this field is `NULL` in BQ as of this writing (a known data-quality gap I flagged in the report's recommendations).

---

## Phase 12 — Room reset (device recycle)

**What it is, in plain words:** BrowserStack restores the device to a clean state for the next user — wipes apps, resets settings, reboots if needed. Not visible to you as a metric; it just affects how soon the next session can start.

**Hotel analogy:** Housekeeping deep-cleans the room for the next guest after you've gone. The room can't be re-rented until that's done.

**Where it shows up in metrics:** Not a user-facing field. Internally affects "queue / waiting time" for the next person to land on this device.

---

## A typical Maestro iOS session, end to end (from our actual data)

Numbers below are from the 100-session iPhone 17 / iOS 26.x cloud benchmark (P50 / P90):

```
Phase                              P50         P90
─────────────────────────────────────────────────
1.  Reservation (trigger)          ~0          ~0
2.  Queue (waiting time)           0           ~varies (37/100 had some)
3.  Room assigned                   instant (bookkeeping only)
4.  App download                   0.07 s      0.86 s     (cache hit on most)
5.  App install                    1.60 s      3.79 s
6.  Test suite download            0 s         0.19 s     (cache hit)
6b. Test suite install             2.44 s      6.83 s
7.  Driver install                 2.00 s      8.43 s
8.  Tunnel setup                   0.002 s     0.005 s    (Local off)
9.  firecmd_time (sum of 4–8)     12.4 s      23.1 s
10. Test execution                720 s       810 s       ← user's flow
11. Stop time                     NULL        NULL        ← gap for Maestro
12. Device recycle                (background, no metric)
─────────────────────────────────────────────────
Sum (visible to spec)            ~733 s      ~833 s
```

**Reading this:** at P50, ~3% of session time was BrowserStack overhead, ~97% was the user's own flow execution. At P90, ~6% was BrowserStack-side. For very *short* tests (e.g., 60 s flows), BrowserStack overhead becomes a much bigger fraction of total — which is why long synthetic loops were chosen for benchmarking, to make BrowserStack-side numbers stable as a % of total.

---

## Same picture for Appium (the differences)

For Appium on either OS, the lifecycle has the same 12 phases — but two things differ:

1. **Phase 7 (driver install)** is `UiAutomator2` server (Android) or `WebDriverAgent` + `XCUITest` runner (iOS) instead of Maestro's driver. Different binaries, similar role.

2. **Phase 10 (test execution)** is fundamentally chattier. Each command is a separate HTTP exchange between your test process and the device's automation server, so:
   - Per-command timing is captured in BQ (`app_automate_performance_data_partitioned.command_data` has arrays like `{"POST:element_click": {"d": [540, 455, 477, ...]}}`).
   - You can SQL-aggregate "which command is slow" without parsing logs.
   - But each command pays HTTP overhead, so very short tests are dominated by command count, not actual UI work.

---

## Cheat sheet — Android vs iOS specifics by framework

|  | Android | iOS |
|---|---|---|
| **Appium driver** | UiAutomator2 server APK (uses Android's built-in accessibility framework) | WebDriverAgent + XCUITest test runner (Apple's official UI testing framework) |
| **Maestro driver** | maestro-driver-android.apk + instrumentation APK | XCTest UI runner with Maestro's driver code |
| **Command transport** | Both: HTTP (Appium) or in-flow batch (Maestro) | Same |
| **App format** | `.apk` | `.ipa` |
| **Special install flag** | `-t` for test-only APKs (sample apps usually need this) | normal install |
| **Common quirk** | Play Protect verifier may delay install if not disabled | App Verifier is mandatory; less variance but ~1 s of constant overhead |

---

## Why this lifecycle matters for benchmarking

Every spec metric has a clear home in this lifecycle:

| Spec metric (PDF) | Phase |
|---|---|
| `waiting_time` | 2 |
| `terminal/device readiness` | 3, 7 |
| `start_time` / `firecmd_time` | sum of 4–8 (= phase 9 boundary) |
| `execution_time` (session duration) | 10 |
| `app_download_time` (P1) | 4 |
| `app_install_time` (P1) | 5 |
| `test_suite_download_time` (P1) | 6 |
| `test_suite_install_time` (P1) | 6b |
| `stop_time` (P1) | 11 |

The reason the spec asks for these specific cuts is that each one is a *different team's responsibility* on the BrowserStack side:
- Phase 2 (queue) is the capacity / scheduling team.
- Phases 4–6 (downloads / installs) are the storage / device-fleet team.
- Phase 7 (driver install) is the framework-integration team.
- Phase 11 (stop) is the cleanup / recycling team.

Benchmark data lets each team see their slice and improve it.

---

## TL;DR

> **A session is a hotel stay.** The test framework (Appium or Maestro) is the kind of robot butler in your room. Appium is chatty (one phone call per command, full flexibility). Maestro is quiet (hand over a checklist, faster but less flexible). BrowserStack handles the rest — booking, luggage, room cleanup. Your benchmark tries to measure exactly how long each stage of "moving in" and "checking out" takes, separately from the actual test work, so you can tell whether slowness is on you (the guest) or the hotel (BrowserStack).
