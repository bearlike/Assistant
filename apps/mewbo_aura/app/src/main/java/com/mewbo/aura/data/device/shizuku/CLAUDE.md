> ↑ [data/device/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../../CLAUDE.md)

# Screen control at shell UID — data/device/shizuku/

Scope: the Shizuku-backed half of device control. `ShizukuDeviceControl` (the app-side binding),
`DeviceUserService` (the shell-UID implementation), and the pure logic each is built from:
`ElementPruner`, `DisplayGeometry`, `SettlePolicy`, `UiSnapshot`, `ScreenCapture`.

## Two processes, and only one of them is the app

`DeviceUserService` does **not run in the app's process**. Shizuku starts it as a separate
`app_process` at uid 2000 and hands the app a binder. Nothing in it may touch app state, Hilt, or
any singleton — only what arrives over the binder. `ShizukuDeviceControl` is the app-side half and
the only thing that binds.

**The binding is held, not per-action.** That is the whole latency argument: a spawn per tap would
pay process startup every time. Measured on the dev container, the marginal cost of an
`app_process` spawn is **~16 ms** — two orders of magnitude below the ~1s figure the design was
originally argued from, so the *reason* to hold the binding is that `newProcess` is deprecated and a
held channel is the right shape, not that a spawn is ruinous. **The real cost centre is the element
read at ~2.0s**, which is ~124x a tap. Optimise there or nowhere.

## Index addressing is a correctness mechanism, not an ergonomic one

The model names an element index; the client resolves it to a centre point. There is deliberately
no coordinate argument anywhere in the tool surface. This removes by construction the failure where
coordinates computed against a downscaled screenshot are applied to full-resolution device space:
every tap lands proportionally wrong, hitting a real control rather than erroring, so nothing
reports a problem.

Two consequences:

- **Geometry never crosses the wire.** Four coordinates per element would be paid for on every
  observation and read by nobody. Pinned by a test, because it is the kind of field a future edit
  adds back "for completeness".
- **A stale index must be a structured error, never a silent mis-tap.** The list the model holds may
  be several turns old. `DeviceActionHandler.resolve` re-reads at action time and refuses with the
  actual element count, which is a state the model can recover from.

## An injected tap hits the TOPMOST window — including one of ours

`input tap` is delivered to whatever window is on top at those coordinates, so any overlay this app
draws intercepts the taps it is trying to inject. The device-control Stop pill sits bottom-centre
and would have ended the grant it was acting under. The cure is temporal — `ScreenCaptureVeil` wraps
`tap`/`swipe`/`type` (`type` included: it taps to focus the field first), and a window that is not
on screen cannot be hit. See [`ui/control/CLAUDE.md`](../../../ui/control/CLAUDE.md) for the window
side, and [`data/device/CLAUDE.md`](../CLAUDE.md) § "The capture veil" for the seam.

Two related window facts, both of which fail with NO error, if you ever add another overlay:
`TYPE_APPLICATION_OVERLAY` above **0.8** obscuring alpha silently drops every touch to the app
underneath (Android 12 untrusted-window rule), and a view added straight to the `WindowManager` does
not inherit the manifest's hardware acceleration — AGSL has no software path, so a shader draws
nothing without `FLAG_HARDWARE_ACCELERATED`.

## `show_touches` cannot verify an injected tap — do not reach for it

Injected events enter the pipeline at `InputDispatcher`, **downstream of
`PointerChoreographer`**, which is the only stage that draws touch spots. So the setting renders
nothing for our taps while still rendering the human's — a signal that is silent when correct and
misleading when wrong. Verification is by EFFECT (did the screen change), which is what
`SettlePolicy` is for.

## The settle is bounded, and the bound is not the interval

Three identical reads, 0.5s apart, 6.0s cap. **Never an unbounded idle wait** — `uiautomator2`
disables framework idle-waiting by default because a device that never idles (an animation, a
video, an ad) hangs the caller, and inside a 30s dispatch budget that is a timeout generator.

The real worst case is the cap **plus one read**, since the deadline is checked before starting a
poll rather than mid-read. With a ~2.0s read that is ~7s, not ~1s. Still inside the budget, still
incapable of running away.

## `uiautomator dump` ships, against the original plan, on a measurement

It was rejected as "an `app_process` launch — same cost class as the rejected route". Measured, the
spawn is ~16 ms and the other ~2003 ms is the accessibility-tree read itself, which any method pays.
Reading the tree in-process saves the 16 ms, not the 2 seconds.

What it does avoid is the file round-trip: the dump goes to stdout and is parsed in memory, so there
is no `/sdcard` write and no temp file. `UiSnapshot` isolates it behind one function precisely so a
held `UiAutomation` connection can replace it later without touching the pruning or the wire format.

## The foreground app is a FIELD on the observation, never a tool

`package` + `activity` are stamped at the HEAD of the elements result, from `mCurrentFocus`. Every
device task opens by asking what app is on screen, and with no answer the model ran
`dumpsys window | grep mCurrentFocus` by hand. A tool schema for that is re-sent at full price on
every model call; a package repeated on every node is per-node cost for a per-screen truth. The
second command costs **~0.01s against a ~2.0s element read** (measured) — re-measure before adding a
third. The focused window beats a popularity contest over the nodes' own `package`, which a
full-screen system overlay wins. Unparseable, or a window naming no app (`StatusBar`), omits both
keys rather than guessing.

## A failed capture must name its cause — `exists()` is not the check

`screencap` refusing a window still CREATES the output file, empty, so an existence check reports
success for the one case it exists to catch; the decode then returns null and a capture that answers
`""` hands the agent blindness. Test `length() == 0`, and keep `screencap`'s own combined output —
it is the only place the real cause is ever stated (`FB is protected: PERMISSION_DENIED`). The
reason is model-facing prose carrying a cause AND a recovery, because the alternative is a caller
inventing an explanation it was never given.

**Reproducing it takes a purpose-built app, and the privilege tier decides what you learn.** No stock
app on the AOSP image raises a secure window — the lock-screen and credential-confirm screens all
capture fine. A throwaway `FLAG_SECURE` overlay APK does, and measured A/B/A at **shell UID** it
gives `exit=1`, `size=0` and `W SurfaceFlinger: FB is protected: PERMISSION_DENIED`, for every form
(to a file, to stdout, raw). That is the tier that SHIPS: on a non-rooted device Shizuku runs at
shell. **The container's own service is not that tier** — `shizuku_server` runs as ROOT here, which
also silently bypasses a mode-400 trap, so a failure staged by permissions does not fire and the
capture succeeds with nothing reporting a problem. Whether root bypasses the secure-layer refusal
too is UNKNOWN (`CAPTURE_SECURE_LAYERS` is `signature|privileged`) and no user is on that tier. To
exercise the failure path THROUGH the container's root service, stage a root-proof failure instead:
make the capture path a directory.

## Everything worth testing here is device-independent, deliberately

The pruning rule, the `wm size` parser, the settle bound and the advertise gate are all pure logic
with JVM tests. Keeping the privilege-dependent surface thin is what makes that possible — and it
matters because the container is `privileged: true` and therefore **not a valid witness** for
anything gated on real shell-UID enforcement. Use it for the parser (geometry is set explicitly:
1440x3120, dpi 560) and for smoke tests; the physical device is the gate.

## The status is PUSHED, and a one-shot read is the bug it fixes

The Shizuku binder arrives **asynchronously**, after app start. A status read
once at screen-composition time therefore reports `NotRunning` for a service
that is running perfectly well, and nothing ever corrects it — the user is told
to start a service they already started. `addBinderReceivedListenerSticky` is
the cure and the sticky variant is load-bearing: it replays a binder that
arrived BEFORE the listener was added, so there is no race with app start.
`addBinderDeadListener` drops the bound handles with the service, since calling
through a dead binder throws.

Settings additionally re-reads on RESUME, not on composition: starting Shizuku
and granting a permission both happen in another app, so the moment that matters
is the user coming back.

## `wm size`: parse `Override` before `Physical`

An override is exactly the case that silently corrupts the coordinate space — the panel is 1440
wide, the window manager addresses 1080, and every tap lands proportionally wrong. Unparseable
output yields `null`, never a guess: a guessed geometry would be used for index→centre resolution.
