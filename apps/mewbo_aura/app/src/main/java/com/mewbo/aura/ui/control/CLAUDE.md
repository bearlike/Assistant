> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# The device-control overlay — ui/control/

Scope: `ui/control/` — the window that tells the user an agent is driving their phone.
`DeviceControlOverlay` (the window controller, a `@Singleton`), `DeviceControlNarration` (the pure
event→lines fold), `VeilFade` (the pure hide/restore choreography), `DeviceControlOverlayScreen`
(`DeviceControlAura` + `DeviceControlStopPill`).

**This package renders; it decides nothing about control.** The grant lives in
[`data/device/DeviceControlSession`](../../data/device/CLAUDE.md); this subscribes to
`active` and is raised and lowered by it alone — never by the call sites that take and release a
grant, because **a grant can end with nobody calling `stop()`** (the Shizuku binder dies with its
host process and the grant demotes itself). Only a collector sees that. A window claiming the phone
can be driven, outliving the channel behind it, is the toggle-that-lies failure the grant exists to
remove.

## The GRANT is the lifetime — not a run, not a screen, not the app being open

Worth stating flatly because it is the property most likely to be re-broken by something that looks
unrelated. The one raise/lower seam is `DeviceControlOverlay`'s `init` collector over
`grant.active`, and there is nothing else: no `Activity`, no `RunRepository` terminal, no
foreground check anywhere on that path. So the glow is up over the launcher, over somebody else's
app, and with Aura swiped away — which is the whole point, because a shell-UID takeover is
otherwise indistinguishable from the phone doing nothing.

`follow(sessionId)` is a NARRATION address, not a lifetime. `RunNotificationLauncher` calls it at
run start on EVERY run and it raises no window; the bubbles are the only thing a session id can
change. Reading it as "the overlay follows a run" and gating the windows on one is the specific
mistake this note exists to stop.

**What actually keeps this from working is `SYSTEM_ALERT_WINDOW`, and nothing in the app asks for
it.** It is a SPECIAL permission — the manifest declaration grants nothing, and the user has to
turn on "Display over other apps" in system Settings. `Settings.canDrawOverlays` is the only gate
in `raise()`, and the app has no `ACTION_MANAGE_OVERLAY_PERMISSION` entry point anywhere, so on a
fresh install `raise()` returns early and the glow never appears — in Aura, in another app, or
anywhere else. The surface degrades exactly as designed (device control is untouched) and that is
the trap: nothing fails, nothing logs, the feature is simply invisible. Whoever adds the request
should follow `POST_NOTIFICATIONS`' shape — asked at first relevance from a screen, the OS grant as
the sole gate, never a pre-consent dialog of our own.

One consequence of the gate being re-read only on a raise: granting the permission part-way
through a grant does not retroactively raise the window. That is honest rather than ideal; the next
grant picks it up.

### The GLOW spans the grant; the BUBBLES stand down in the app

Different questions, and only the first is about the grant. In the app the transcript is already
saying what the agent is doing — in full, with history — so a bubble stack repeating the last line
over the top of it is noise. Outside the app there is nothing else at all, which is the case the
narration exists for. So `DeviceControlAura` takes `narrating` separately from `visible`, and only
the bubble stack reads it.

**The predicate is the EXISTING `AppForegroundChecker`, reused verbatim — do not mint a second
one.** It is process importance OR `AssistOverlayPresence`, and both of those surfaces render the
transcript (`MainActivity`'s chat, and the assist overlay's own `ChatTranscript`), so the one
predicate already answers the question this surface has: not "is the app running" but "would a
bubble repeat something the user can already read".

Two things about it that are load-bearing and non-obvious:

- **Our own overlay window does not make the app read as foreground.** A visible
  `TYPE_APPLICATION_OVERLAY` ranks the process at `IMPORTANCE_VISIBLE` (200) and the device-control
  FGS hold at `IMPORTANCE_FOREGROUND_SERVICE` (125); the gate wants at least `IMPORTANCE_FOREGROUND`
  (100), so neither one trips it. Had either done so the bubbles would never appear at all — a
  self-cancelling feature with nothing reporting it.
- **It is POLLED on the expiry tick that already exists, not observed.** The app carries no
  process-lifecycle observer and `di/DeviceModule` deliberately declined the dependency that would
  provide one, so a poll is what is available. Reusing the 500ms narration tick costs no second
  loop, and half a second of lag on a stack whose lines live four seconds is imperceptible. The
  binder read only happens while an agent is actively driving the phone.

**On a TELEVISION the predicate answers the wrong question, and the shape says so.** The reasoning
above rests entirely on "the transcript is already saying this" — true of a phone held at reading
distance, false of small text on a panel across a room that the agent is driving at the same time.
`DeviceShape.narratesOverOwnApp` is OR-ed into the gate, so that shape narrates everywhere and the
handheld behaviour is untouched. It is derived into a separate `narrating` flow rather than folded
into `outsideApp`, so that field keeps meaning exactly what its name says.

Accepted gap: "in the app" is coarser than "looking at THIS session". A user in Settings or another
chat while an agent drives the phone sees the glow but no narration. Widening it means teaching this
surface which session is on screen, which is a route-observation dependency the overlay does not
otherwise need — take the noise reduction, and revisit only with a real complaint.

## Two windows, and it is not a style choice

There is exactly one supported shape for "pass touches through everywhere except one control": a
window receives every touch inside its own bounds, and an event no view consumes is **not** forwarded
to the window behind it. So:

| Window | Flags | Why |
|---|---|---|
| glow + narration | `FLAG_NOT_TOUCHABLE`, full-screen, `LAYOUT_IN_SCREEN｜NO_LIMITS`, cutout mode `ALWAYS` | can never take a touch; reaches past the system bars because the glow's peak is anchored at the true bottom edge, and past the CUTOUT because no-limits does not cover that (below) |
| Stop pill | `FLAG_NOT_TOUCH_MODAL`, `WRAP_CONTENT`, bottom-centre | its bounds ARE the touchable region; deliberately NOT no-limits, so the window manager keeps it clear of the gesture bar |

Added in that order, so the pill stays reachable while the glow is at its brightest.

## Six platform facts, each of which fails SILENTLY

- **`alpha` must stay ≤ `MAX_OBSCURING_ALPHA` (0.8).** Android 12 blocks touch pass-through beneath a
  window the system does not trust, and `TYPE_APPLICATION_OVERLAY` is explicitly untrusted; a
  `FLAG_NOT_TOUCHABLE` window is exempt only under the system maximum. **At the default 1.0 this
  window drops EVERY touch to the app underneath — the user's and the agent's injected taps alike —
  with nothing reporting a problem.** The cost is a glow at 80% opacity; do not "fix" that with
  `alphaScale`, which would retune a measured value against a guess.
- **`FLAG_HARDWARE_ACCELERATED` must be set explicitly.** A view added straight to the
  `WindowManager` does not inherit the manifest's acceleration, and AGSL (`RuntimeShader`) has no
  software path — without it `AuroraEdgeGlow` draws nothing at all.
- **`FLAG_SECURE` is not how this stays out of a screenshot, and reaching for it is destructive.**
  Measured at shell UID: one secure window makes the ENTIRE capture fail (`exit=1`, zero-byte file,
  `SurfaceFlinger: FB is protected: PERMISSION_DENIED`) because SurfaceFlinger refuses the whole
  framebuffer to a caller without `CAPTURE_SECURE_LAYERS`. It cannot hide one layer; it blinds us.
- **This window can never tint the navigation bar, and `FLAG_LAYOUT_NO_LIMITS` is what makes that
  look wrong.** The flag governs EXTENT, not z-order: the decoration window reaches geometrically
  into the navigation-bar region and still composites underneath it. AOSP's
  `WindowManagerPolicy.getWindowLayerFromTypeLw` puts `TYPE_APPLICATION_OVERLAY` at layer **11** and
  `TYPE_NAVIGATION_BAR` at **24** ("shows atop most things"). Independently sufficient on its own:
  `DisplayPolicy`'s nav-bar-appearance candidate admits an app window or `TYPE_VOICE_INTERACTION`
  and nothing else, so this window type is categorically excluded from the decision. Under GESTURE
  navigation the bar is forced transparent (`NAV_BAR_FORCE_TRANSPARENT`, a device config resource
  with no app lever) and the glow should read through it; under 3-button navigation, or beneath an
  app targeting < SDK 35 that still sets an opaque `navigationBarColor`, the strip stays solid and
  **there is no supported fix.** The only app-reachable type above the bar is
  `TYPE_ACCESSIBILITY_OVERLAY`, which needs an AccessibilityService — not a trade this surface makes
  for a cosmetic seam. *(Layer table and policy read from AOSP source; the on-device consequence is
  reasoned, not measured.)*
- **`FLAG_LAYOUT_NO_LIMITS` does not cover the DISPLAY CUTOUT either, and that clipped the top edge
  for a release.** They are separate attributes and are routinely read as one. `WindowLayout`
  intersects the PARENT frame of a window that is fullscreen-and-not-attached with the display's
  cutout-safe rect for every cutout mode except `ALWAYS`; the no-limits branch runs *after* it and
  resets only the DISPLAY frame, so the window is still measured against the clipped parent. The
  glow therefore began where the status-bar strip began — a hard line, not a falloff, which is what
  distinguishes this from an intensity problem. **`layoutInDisplayCutoutMode` has to be set
  explicitly**: the platform's edge-to-edge enforcement (which reinterprets every other mode as
  `ALWAYS`) is applied in `PhoneWindow.generateLayout`, i.e. only for an Activity's or Dialog's
  decor, so a window added straight to the `WindowManager` gets the default however recent the
  `targetSdk`. `ALWAYS`, not `SHORT_EDGES` — short-edges relaxes only the two short sides, so a
  long-edge cutout still clips a surface whose whole subject is an unbroken border. *(AOSP
  `WindowLayout.computeFrames` / `ViewRootImpl.adjustLayoutInDisplayCutoutMode` read directly; the
  on-device result is reasoned, not measured.)*
- **A foreground app can hide this window outright, and we get no signal.**
  `Window.setHideOverlayWindows(true)` is live, undeprecated API that suppresses every
  `TYPE_APPLICATION_OVERLAY` window in the system. So an app the agent navigates into can make the
  announcement disappear while the grant is still held — the toggle-that-lies failure, caused from
  outside. Nothing here can detect or prevent it; the mitigation that exists is the ongoing
  notification, which is a different window type and survives. *(Reasoned from the API contract, not
  measured.)*

## The Stop pill goes up only where it can be PRESSED

Both windows carry `FLAG_NOT_FOCUSABLE`, and that flag is load-bearing: it is what lets the agent's
own injected input reach the app underneath instead of being swallowed by our announcement. **A
window with that flag receives no key events at all.** A finger does not need any; a D-pad has
nothing else.

So on a television the Stop pill was a control drawn above everything and pressable by nobody — worse
than no control, because it is the one thing on screen claiming the grant can be ended there.
Reported from a physical panel as "there is no way for me to actually press the stop button", with
the user force-stopping the app instead. `DeviceShape.overlayCanHostControls` decides whether the
pill window goes up at all.

**Making the window focusable is NOT the cure, and this is recorded so it is not tried:**

- A focusable overlay takes key input from the app below — which is exactly where the agent's
  injected `key` presses land. It would break device control on the one shape it was meant to fix.
- The veil hides the windows for `tap`/`swipe`/`type` but deliberately not for `key`, so an injected
  key would hit the pill. Extending the veil to `key` is possible and pays the fade on every
  keystroke of every session.
- Most decisively: **it makes a leaked grant strictly worse.** Today a wedged overlay still lets the
  user navigate away and force-stop the app; a focusable one would swallow every D-pad press, and the
  user might not reach the launcher at all. Escalating the failure mode of a currently-open bug is
  the wrong trade.

**`lower()` therefore names its windows by ROLE, not by position.** With no pill the list holds one
window, and under `first()`/`last()` the decoration would answer to both — torn out on the PILL's
short timer, losing the ease-off entirely. That is the abrupt on->off luminance change the long exit
exists to prevent, on the one shape watched from across a dark room.

**The consequence is that the television has no stop of its own in this package**, and it must get
one where the D-pad already reaches: in the app, available whenever a grant is HELD rather than only
while a run is live. `ChatViewModel.stop()` already releases the grant synchronously and first, but
the control that calls it is gated on the run — which is exactly the leaked-overlay case.

## The veil — this class IS `ScreenCaptureVeil`

Suppression is **temporal**: the windows come down for the duration and go back however the caller
ends. The seam is declared DOWN in `data/device/` and implemented here, bound in
[`di/DeviceModule`](../../di/CLAUDE.md) — the same shape as `RunNotifications`, because `data/` may
never import a Compose surface. **One binding only**; two `@Provides` for one type is a Hilt
duplicate-binding failure, so the "am I showing" decision lives here where the window state is.

It wraps **two different problems**, and the second is not obvious:

1. the capture, so the model does not read our own chrome as the user's screen;
2. `tap`/`swipe`/`type`, because an injected touch goes to the **topmost window** at those
   coordinates — a tap aimed near the bottom centre would press our Stop pill and end the grant it
   is acting under. `type` is in that set because it taps to focus the field first.

It must cost nothing when nothing is drawn: the fast path returns before touching a window or a
dispatcher. Restore is `finally` + `NonCancellable` — a cancelled capture must never strand the
windows invisible, which would leave an agent driving the phone with nothing saying so. The hide is
inside the `try` for the same reason: a cancellation landing mid-ramp must still restore, because a
half-faded window left behind is the same lie as a hidden one.

### The veil FADES, and `VeilFade` is why the fade cannot bleed into the capture

The order is the correctness, so it is data rather than an inlined sequence: `VeilFade.hide()` ramps
the windows to fully transparent, THEN hides them, THEN holds `WINDOW_SETTLE_MS` — and only that
last step is what the capture or the injected touch runs under. A capture therefore cannot
photograph a half-faded glow; that is a sequence, not a race against the shutter, and a half-faded
one would be worse than a lit one because it reads as a rendering fault rather than as a deliberate
surface. `show()` mirrors it: visible again while still transparent, then ramp to exactly rest.
Being data, all of it is asserted on a plain JVM (`VeilFadeTest`) with no Android and no Compose on
the classpath, and the durations are constructor arguments so a test scripts a four-frame fade
instead of sleeping through a real one.

Three things about the mechanism that are not interchangeable with the obvious alternatives:

- **It ramps the WINDOW's alpha (`updateViewLayout`), not a Compose animation.** Alpha is a
  compositor property, so it fades with no app redraw and keeps working while a shell-UID capture
  has the render thread busy — and it fades both windows through one mechanism, where a Compose fade
  would need the shader and the Stop pill animated separately.
- **A window at alpha 0 is invisible to the eye and STILL IN THE INPUT DISPATCHER'S LIST.** The
  visibility hide is what removes it, and therefore what stops the Stop pill eating the tap
  `device_action` is about to inject. Dropping it as "redundant with alpha 0" looks like a
  simplification and silently re-opens the tap-presses-Stop failure.
- **A step never writes an absolute alpha — it scales each window's OWN resting alpha**, read off
  the params it was added with (`VeiledWindow`). The decoration window rests AT
  `MAX_OBSCURING_ALPHA` and the pill at full; a restore writing `1f` to both would put the
  decoration window over the Android 12 obscuring ceiling, at which point it swallows every touch on
  the screen with nothing reporting a problem. Scaling makes that unrepresentable, and the test
  asserts the envelope never exceeds `1f` for exactly this reason.

**The fade is short on purpose (`FADE_MS` = 96ms, six frames at 60Hz).** It runs on every
`tap`/`swipe`/`type`, not only on a screenshot, so its duration is paid on every action the agent
injects — a leisurely fade would make the glow visibly pulse throughout a session, a worse artefact
than the snap it replaces. 96 is the house's quick flat fade (`AuraMotion.reducedBlockFadeMs`, 100)
snapped to whole frames; it is NOT read from `AuraMotion`, because this is window timing beside
`WINDOW_SETTLE_MS` and a pure test of `VeilFade` must not class-load a Compose object. No
reduced-motion branch: a fade is opacity rather than travel, so reduced motion keeps it (DESIGN.md),
and at 96ms there is nothing left to flatten.

**`WINDOW_SETTLE_MS` is still the one unproven number**, and it is held AFTER the fade so
lengthening the fade can never eat into it. Hiding a view is not synchronous to the compositor and
the platform exposes no "this window has left the screen" signal. If the glow appears in a returned
screenshot, that is why.

## The narration fold is pure, and `nowMs` is an argument

`DeviceControlNarration` reads no clock and touches no I/O, so ordering, replacement and expiry are
all exercised on a plain JVM with fixed times instead of sleeps. Three rules worth keeping:

- **A streaming line refreshes IN PLACE, keyed on agent id.** Moving it to the end would re-order the
  stack under the reader every few hundred milliseconds; a sub-agent gets its own line rather than
  interleaving into the root agent's sentence.
- **`fold` returns THIS INSTANCE (identity, not an equal copy) for an event it does not draw**, so a
  `MutableStateFlow.update` over a busy stream emits nothing and the overlay does not recompose on
  traffic it ignores. `expire` does the same when nothing expired.
- **The tail is kept, not the head.** While a reply streams, a head-truncated line stops changing the
  moment the text passes the cap — the one surface whose whole job is to show that something is
  happening would freeze.

`label()` is a SECOND place `device_*` semantics are spelled out (the first is
`DeviceToolCatalog`). Every unknown arm falls back to the tool id, so a new action renders as a
generic phrase rather than vanishing — but **a new action added to the catalog wants a line here
too.**

## Everything degrades silently — this surface may never block a grant

`SYSTEM_ALERT_WINDOW` is a special permission, not a runtime one. `canDrawOverlays` is re-read on
every raise (the user can grant it at any time, and an app that asked once would stay silent for the
rest of its install), `addView` is in `runCatching` (the permission can be revoked between the check
and the call), and the Stop tap's `startService` is too. No grant, no window, device control
unchanged.

**Stop routes through `RunNotificationService.stopIntent`** — the same intent the notification's own
Stop fires. One way to end a grant, deliberately: a second release path here would be a second
opinion about how long an agent may drive the phone.

## Arrival and departure are ONE envelope — and the Stop pill is the one exception

`showing` is the whole choreography. The glow, the narration stack and the pill all read that one
flow, so nothing on this surface can arrive or leave on a schedule of its own; both directions ramp
LINEARLY over `AuraMotion.deviceControlEaseMs` (2.4s), and only then do the windows come down —
removing a lit surface outright is the abrupt on→off luminance change the photosensitivity law
forbids.

**Three mechanical points, each of which a plausible-looking alternative gets wrong:**

- **The envelope is a `graphicsLayer` alpha read in the LAYER phase, not a Compose state read in
  composition.** `AuroraEdgeGlow`'s per-frame knobs are all `State<Float>` for this reason; driving
  its `alphaScale` from an animation instead would recompose the shader host every frame for the
  whole 2.4 seconds.
- **The glow is never handed `Hidden` on the way out.** It stays `Listening` for the entire exit and
  the surface envelope does all the fading. Handing it `Hidden` as well runs the composable's own
  dismiss ramp underneath this one, and two ramps multiplied are a curve whose fastest segment is at
  the END — the exact shape the photosensitivity rule forbids. (`AuroraEdgeGlow(dismissFadeMs = …)`
  is therefore unused HERE; it is still the right knob for the assist overlay and for chat, which
  fade by state rather than by envelope. Mechanics: [`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md).)
- **The `entered` latch is still needed and is not the envelope.** `animateFloatAsState` initialises
  AT its target, so a glow composed already-`Listening` has its INTERNAL envelope at full on frame
  one. The surface envelope would hide that anyway — an `Animatable(0f)` genuinely starts at 0 —
  but the latch keeps the two rising together instead of one sitting at rest under the other.

### Why a 2.4s exit does not read as "it did not stop"

The short 180ms exit this replaced was argued for on exactly that ground: a grant ending is
deliberate and user-initiated, so a glow still fading seconds after Stop says the Stop did not work.
That is true **of the affordance**, and the affordance is the pill — the one thing here asserting the
phone can still be taken over. So the pill is the exception: it arrives on the shared window and
leaves on `DEVICE_CONTROL_PILL_DISMISS_MS` (the old quick value), and its WINDOW is removed at the
end of that. What eases off afterwards is a decaying glow with nothing left on it to press.

Removing the window is the load-bearing half, not the fade. A window faded to nothing is still in
the input dispatcher's list (the same fact the veil documents), and this one sits bottom-centre over
the app the user is already reaching past — leaving it up for the decoration's benefit would keep
eating taps, and a pressed off-switch still on screen invites a second press.

`lower()` therefore tears down in two stages, and **the waits are DERIVED**: the pill at its own
dismiss plus a frame, the decoration at `DEVICE_CONTROL_EXIT_MS` — `maxOf` of the two exits — plus
that same frame. A teardown restated as a second literal is how a window gets ripped out from under
an animation the next time either number is retuned, and the symptom is the abrupt cut the fade
exists to remove.

**The veil's `FADE_MS` is a DIFFERENT mechanism and must not follow this one.** It runs on every
injected tap, swipe and keystroke; at 2.4s the glow would visibly pulse for the whole session. Its
96ms is deliberate — see "The veil" above.

`AuraTheme(reducedMotion = …)` is threaded explicitly. It is a defaulted parameter, so a bare
`AuraTheme { }` compiles and silently drops the in-app toggle for this whole surface — the trap that
bit the assist overlay for a release cycle.

## What a test can see here — and the line it stops at

`DeviceControlNarrationTest` and `VeilFadeTest` cover the two pure halves, the fold and the
choreography, on a plain JVM with no Android on the classpath. **Neither can see a window, and that
is exactly how this surface shipped completely invisible with every gate green.** `raise()`'s early
return on `canDrawOverlays` decides whether a window EXISTS; no test of a fold can observe an
absence of one.

`DeviceControlOverlayTest` is the one Robolectric suite in this module and it exists for that gap
alone. It stands a real `DeviceControlOverlay` over a real `WindowManager`, driven by a real
`DeviceControlSession` behind fake Shizuku seams — so the transitions below are the production ones,
not a fake overlay being told what to think. Five claims are now pinned:

- **No `SYSTEM_ALERT_WINDOW`, no window — and the grant is still held.** The pair is the assertion:
  the surface degrades to nothing AND never blocks the thing it announces.
- **With the permission, both windows go up** — the decoration and the Stop pill.
- **Releasing the grant takes them down.**
- **A grant ending with NOBODY calling `stop()` takes them down too.** The status leaves `Ready`, the
  session demotes `HELD → LOST` off its own collector, and the windows follow. This is the
  binder-death path the `init` collector exists for; an implementation that lowered from `stop()`
  alone passes every other test here.
- **`hiddenDuring` puts the windows back when the block THROWS** — the `FLAG_SECURE` capture, i.e.
  the `finally` + `NonCancellable` the veil's KDoc calls load-bearing.

**It proves a window was ADDED, never that a pixel was drawn.** Nothing renders under Robolectric:
the AGSL shader never draws, and a glow that composed to a fully transparent surface would pass all
five. Still unwitnessed by anything — colour, placement, the two windows' flags, the
`MAX_OBSCURING_ALPHA` ceiling that keeps touches passing through, whether the Stop pill is reachable,
and every item under "Not verified" below.

Two mechanical facts a new test here will otherwise re-derive the hard way:

- **`ShadowChoreographer` is NOT paused by default**, and this surface drives an unbounded frame loop
  through `AuroraEdgeGlow`. A bare `ShadowLooper.idle()` therefore drains a queue that refills itself
  and never returns — a HANG, not a failure, which reads as a slow suite rather than as a bug.
  `setPaused(true)` + `setFrameDelay` in `@Before` is what turns every wait into a bounded number of
  frames; time is then virtual, so nothing sleeps.
- **Assert VISIBILITY, never window alpha.** `view.visibility` is written straight onto the view, but
  the alpha ramp goes through `updateViewLayout` inside a `runCatching` — so an alpha assertion would
  pass just as happily against a build where the ramp never landed at all.

`KeystoreCipher`'s constructor opens the `AndroidKeyStore` JCA provider, which Robolectric does not
ship, so a `SettingsStore` needed by a Robolectric test takes a mocked cipher. The store itself is
real; the path under test never reaches the cipher.

## Not verified

Nothing here has run on physical hardware. Beyond the settle delay: insets in a
`FLAG_LAYOUT_NO_LIMITS` window are unproven (if `navigationBars` reports 0 there, the bubble stack
sits lower than intended), and the Stop tap assumes the foreground service is already running — a
held grant implies it, and if it is not the tap does nothing rather than crashing. The cutout mode
does not widen that gap: it moves only the sides the cutout constrained — the top in portrait — and
BOTH inset consumers read `navigationBars` at the bottom, where the window already reached the true
edge. The Stop pill is in the other window entirely and its params are untouched.

The 2.4s envelope adds three, all reasoned rather than measured:

- **That 2.4s reads as an ease rather than as "it did not stop".** The pill leaving promptly is the
  argument, and only a device says whether it is enough. If it is not, the pill is already the knob:
  shorten `DEVICE_CONTROL_PILL_DISMISS_MS`, not the decoration's window.
- **That a group-opacity fade over the dithered near-black ramp does not band.** The envelope is a
  `graphicsLayer` alpha, so the composited result is re-quantized to 8 bits on the way out for the
  duration of the fade, over precisely the ramp `ui/aurora/CLAUDE.md` Rule 3 says has only ~50–84
  distinct codes to begin with. `CompositingStrategy.ModulateAlpha` is the knob if it does band —
  it folds the alpha into each draw instead of compositing a layer — at the cost of the bubbles no
  longer occluding the glow mid-fade.
- **That the teardown's 2.4s hold is harmless when a grant ends and immediately restarts.**
  `grant.active.collect` is sequential, so a `raise()` cannot begin until `lower()` returns — the
  surface fades fully out and back in over ~5s while control was continuous. It self-corrects and
  was already the shape at 180ms; only the duration is new. Under reduced motion the same hold
  applies while the envelope has already flattened to 100ms, so the (invisible, already-removed)
  windows simply wait longer than they need to.

The veil adds three more, all reasoned rather than measured:

- **That a view set `INVISIBLE` leaves input dispatch** (the window's `viewVisibility` reaching WMS
  is what should do it) — the fade never relies on it, since the visibility hide is kept precisely
  because alpha is known NOT to, but a measurement would settle it.
- **That a six-step `updateViewLayout` ramp reads as a fade rather than as banding**, and that a
  relayout per step is cheap enough at that rate. The step count is the knob if it is not; the
  fade's total duration should not grow, for the per-tap reason above.
- **That 96ms + the settle is an acceptable per-action tax.** An agent doing twenty taps now pays
  roughly three extra seconds across a session. Measure a real device-control run before trading
  the fade away for it — the snap it replaces is on screen for the whole grant, not just once.
