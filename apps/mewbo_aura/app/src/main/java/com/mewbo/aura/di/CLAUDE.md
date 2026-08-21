> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura DI — di/

Scope: `di/` — the Hilt `SingletonComponent` modules. `DataModule`, `DeviceModule`, `HapticsModule`,
`NotifyModule` ship in every variant; the fake-voice and mock-backend bindings live in variant-scoped
`src/debug` / `src/release` mirrors.

## The seam law — a cross-layer dependency is a narrow interface DOWN + a binding HERE

Every place a higher layer needs a lower one to call UP (a repo notifying the notification service, a
device handler launching an activity) is a narrow `fun interface` declared in the CONSUMER's own lower
package, with the implementation `@Binds`/`@Provides`-bound here. **There is never an upward import**;
the interface plus this module are the only seam.

- `DeviceToolDispatch` ← `DeviceToolExecutor` (`DeviceModule.bindDeviceToolDispatch`) is the ONE place
  the executor is wired to anything. `RunRepository.live()` attaches it, and **nothing else may inject
  it**.
- `RunNotifications` ← `RunNotificationLauncher` (`NotifyModule`) — same shape, for the FGS start.
- `DeviceToolGate` / `DevicePermissionChecker` / `DeviceClock` / `AppForegroundChecker` /
  `DeviceToolResultReporter` are `@Provides` lambdas so `data/device/` stays plain-JVM unit-testable.
  **Do NOT inject `SettingsStore`/`Context`/`AuraApi` into that package directly.**

## DataModule — one network stack, URL editable at runtime

- Retrofit is built ONCE against a placeholder base URL; **`BaseUrlInterceptor` rewrites every
  request's scheme/host/port** to the current `SettingsStore.baseUrl`, which is what makes the server
  user-editable with no Retrofit/OkHttp rebuild plumbing. `SessionStreamClient` builds its own URL, so
  the rewrite is a harmless no-op for SSE.
- `AuthInterceptor` sends `X-API-Key`, `X-Mewbo-Surface: android`, and `X-Mewbo-Capabilities` — a
  comma-joined list with `apps` always present and `stlite` only while `streamlitWidgetsEnabled` is on.
  **Advertise and render are keyed on the same flag**, so the app never advertises what it will not
  service. Flags are read via blocking `first()`: DataStore caches in memory, and this runs off-main on
  OkHttp's dispatcher.
- `provideJson`: `ignoreUnknownKeys = true` AND **`explicitNulls = false`**. The second is load-bearing
  for the DTO round-trip contract — a test asserting a null field is DROPPED from an encoded body must
  match this `Json` exactly.
- `provideEventSourceFactory` = `okHttpClient.newBuilder()` with `readTimeout(0)`/`callTimeout(0)`,
  since SSE is long-lived on 15s server heartbeats. **`newBuilder()` COPIES the interceptor chain**,
  which is why the debug mock interceptor transparently covers SSE too.
- `debugOnlyInterceptors: Set<Interceptor>` is a multibinding — empty in release, the mock interceptor
  in debug — added LAST, so a short-circuiting debug interceptor sees the exact final request shape the
  real network would.
- `@ApplicationScope` = `SupervisorJob + Dispatchers.IO`, app-process-lifetime. It backs
  `RunRepository`'s per-session `shareIn` multicast and `DeviceToolExecutor`'s dispatch job — both must
  outlive any single collector, or a `stop()`/nav event tears down servicing for a still-live run.
  `SupervisorJob` so one session's stream failure cannot cancel a sibling's.

## UpdateModule — the ONE client that must NOT be derived from the shared one

`okHttpClient.newBuilder()` is the house idiom for a second HTTP stack (`SpeechModule`,
`provideEventSourceFactory`) precisely because it COPIES the interceptor chain. **That is what makes
it wrong here, and it is the kind of wrong nobody notices in review.** The chain carries
`BaseUrlInterceptor`, which rewrites every request's host to the user's own Mewbo server — so a
release request would never reach the forge at all — and `AuthInterceptor`, which attaches the
user's Mewbo API key, handing it to a public forge on every update check. Broken and a credential
leak, in one line. `provideUpdateHttpClient` therefore builds a BARE `OkHttpClient` from scratch,
with `callTimeout(0)` because a call here is a hundred-megabyte APK. Enterprise TLS needs nothing:
the deployment CA is a Network Security Config trust anchor, which is application-wide.

- **`provideUpdateRetrofit`'s base URL is the one REAL base URL in this app** — nothing rewrites it,
  because the release source is a BUILD fact (`BuildConfig.UPDATE_API_ROOT`, per distribution
  flavor) and a shipped APK must not be re-pointable at another forge.
- `UpdateChannel` is injected as a VALUE rather than letting the repository read `BuildConfig`
  itself; that is what keeps `AppUpdateRepository` plain-JVM testable against a fake forge.
- `PackageFacts` ← `AndroidPackageFacts` and `PlatformInstaller` ← `ApkInstaller` are the usual
  narrow-interface-DOWN + binding-HERE seams, for the usual reason: the rules worth testing
  (version arithmetic, asset choice, the three verification checks) must not need a `Context`.
- Mechanics, the endpoint decision, and the signature-chain trap:
  [`data/update/`](../data/update/CLAUDE.md).

## DeviceModule / HapticsModule / NotifyModule

- `DeviceModule` provides the handler LIST out of `DeviceToolExecutor`'s constructor, so its unit tests
  need no `Context`-backed handlers. `AppForegroundChecker` ORs process importance with
  `AssistOverlayPresence.visible` → `canStartActivityNow`.
- `HapticsModule` is the ONE `Vibrator` injection point, wrapped in `runCatching { … }.getOrNull` so
  a device with no vibrator degrades to a no-op rather than crashing. The API branch itself lives in
  `data/device/VibratorResolver` — `VibratorManager` is API 31, and at minSdk 30 there is none, so
  the resolver falls back to the deprecated `VIBRATOR_SERVICE` lookup. It sits in `data/device/`
  rather than here because `WakeAlarmReceiver` needs the same branch and may not import `di/`
  (dependencies flow down); duplicating it is what let the two call sites diverge before.
- `NotifyModule` binds `RunNotifications` ← `RunNotificationLauncher`.

## `DeviceControlGate` — a capability seam, not a permission seam

`DeviceModule` binds it to `ShizukuDeviceControl.readStatus().isReady`, alongside `DevicePermissionChecker`
and `DeviceToolGate` and for the same reason: it keeps `DeviceToolCatalog` plain-JVM testable with no
Shizuku binder in the test. It is a THIRD axis rather than another permission — the Shizuku service
dies on reboot, so this one flips without the user touching the app.
[`data/device/shizuku/`](../data/device/shizuku/CLAUDE.md) owns the mechanics.

**`AuthInterceptor` asks the CATALOG whether to advertise `device_control`; it must never re-derive
that from the toggles.** The capability activates a playbook skill server-side, while the tools ride
the `/query` BODY — two carriers, so two predicates meant one could be true without the other. With
the toggles on and Shizuku down, the model received the observe→act playbook and no `device_ui` to
call, and only discovered it after activating the skill and running two tool searches.
`DeviceToolCatalog.advertisesDeviceControl()` is derived FROM the list that goes on the wire, which
makes the divergence impossible rather than merely fixed.

**Residual, and structural:** the header rides EVERY request (it is an interceptor, so also
`POST /api/sessions`, `/message` and SSE) while `device_tools` ride only `/query`. Capabilities are
also sticky server-side, `device_tools` are not. So a session created while Shizuku was up and
steered after a reboot still carries the capability against a last-persisted context with no device
tools. Closing that needs a server-side rule — a skill declaring the tool ids it requires — not more
client care.
