> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md) · children: [shizuku](shizuku/CLAUDE.md)

# Aura Device Tools — data/device/

Scope: `data/device/` — the `device_*` tool catalog, handlers, executor, dispatch, ledger, and the
platform readers behind them. This package PROVIDES the executor/catalog; the seam that advertises AND
answers them is `RunRepository.live()` ([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)) — never a ViewModel.

Handlers: `time`, `battery`, `setAlarm`, `setTimer`, `wake`, `readLatestSms`, `sendSms`,
`getNextAlarm`, `dismissAlarm`, plus the three screen-control tools in
[`shizuku/`](shizuku/CLAUDE.md) (`device_ui`, `device_action`, `device_shell`).

## `DeviceShape` — the two product shapes, named once

`DeviceShape.kt` is where a phone/tablet and a television stop being "the same UI with a boolean" and
become two closed variants, `Handheld`/`Television`, resolved via `DeviceShape.of(televisionChecker)`
— a thin resolver over the existing `TelevisionChecker.isTelevision()` predicate (this same package),
never a second platform read. Declared HERE beside `TelevisionChecker` for the identical reason: `data/` may not import `ui/`.
`ui/common/LocalDeviceShape` publishes the same instance app-wide for Compose, and
`di/DeviceModule.provideDeviceShape` injects the same answer to readers outside a composition.
`voice/SpeechController` now reads it (`narratesTextTurns`); `data/settings/` still does not, for
the reason the reverted member below records.

Each difference between the two shapes is a MEMBER — `opensKeyboardOnFocus`,
`hasOverlayPermissionScreen`, `narratesTextTurns`, `narratesOverOwnApp`, `controlAuraRiseFraction`
and `overlayCanHostControls` — never a boolean asked independently at every reader; adding a third
shape later is a compile error at every arm that has not answered the new member, which is the point.
The one legitimate `when` on a `DeviceShape` picks between two whole component trees
(`ui/navigation/AuraNavHost`'s shell pick); everywhere else asks a member.

- **`hasOverlayPermissionScreen = false` on `Television` is measured Fire OS behaviour, not a
  precaution** — the system screen that grants "Display over other apps"
  (`ACTION_MANAGE_OVERLAY_PERMISSION`) is unreachable there, so the row wired to it is a button that
  silently does nothing and the device-control overlay is permanently and silently inert. The
  Shizuku-backed fallback that provisions the app-op instead ([`shizuku/`](shizuku/CLAUDE.md)'s
  `ShizukuOverlayGrant`) exists because of this member, though it is deliberately NOT gated on
  `DeviceShape` itself — the same ungrantable-screen problem can occur on a kiosk build or a stripped
  AOSP handheld, so the fallback checks reachability directly rather than trusting the device shape as
  a proxy for it.
- **A candidate third member, `speaksRepliesByDefault`, was tried and deliberately reverted — do not
  re-add it without re-reading `data/settings/SettingsStore.kt`'s own KDoc on `speakResponses`
  first.** The read-aloud default was already `true` on every shape, so a device-conditional default
  would have changed nothing on television while silently switching read-aloud OFF for every
  handheld that had never touched the setting. The actual television-silence defect was which
  SYNTHESIZER the selection resolved to (`voice/CLAUDE.md`'s on-device-availability fallback), not
  this flag — a `DeviceShape` member is the wrong tool for a bug that isn't about which shape the
  device is.

### The four members added for the television, and the one question each answers

Each came from a defect on a physical panel, not from symmetry:

- **`narratesTextTurns`** — a television has no voice entry point at all (the assistant role is
  unreachable there), so under the handheld rule that gates read-aloud on `InputModality.Voice`,
  every reply on that shape was silent while the user's read-aloud switch read as ON. The modality
  gate is asking the wrong question there; the switch is the one that should decide. **This is NOT
  the reverted `speaksRepliesByDefault`** — that one proposed changing a DEFAULT that was already
  `true` everywhere. This changes which turns are eligible at all.
- **`narratesOverOwnApp`** — the bubbles stand down in-app because the transcript is already saying
  it. That holds for a phone at reading distance and not for small text on a panel across a room
  that the agent is driving.
- **`controlAuraRiseFraction`** — the control overlay's border profile runs its side rails the full
  height of the surface by construction, which reads as a frame on a tall handheld and as a wash
  over most of a short, wide 16:9 panel.
- **`overlayCanHostControls`** — the overlay's windows carry `FLAG_NOT_FOCUSABLE` so the agent's
  injected input reaches the app underneath, and such a window receives NO key events. A finger does
  not need them; a D-pad has nothing else, so the Stop pill was unpressable by construction on that
  shape. Its KDoc records why making the window focusable is the wrong cure — it would swallow the
  agent's own injected keys, and make a leaked grant unrecoverable rather than merely ugly.

**`DeviceShape` is injectable** (`di/DeviceModule.provideDeviceShape`, `@Singleton`) for readers that
are not under `MainActivity`'s composition and therefore cannot read `LocalDeviceShape` — the
device-control overlay is a raw `WindowManager` view and `ChatViewModel` is not a composable at all.
Reading the local from either would silently return the `Handheld` default with nothing reporting it.
**`MainActivity`'s debug `deviceShape` intent override does NOT reach that binding** and cannot: it is
scoped to one Activity's intent while the binding is resolved per process. So the override still
re-shapes the Compose tree for layout and focus work, and anything reading the injected shape follows
the real platform feature only — which means none of the four members above can be exercised on
redroid at all, on either tier.

## THREE gates now, not two — the third is a CAPABILITY, not a permission

Screen control is gated on the Shizuku service being live (`DeviceControlGate`), which is not an
Android runtime permission and cannot be modelled as one: it changes **without the user touching the
app**, because the service does not survive a reboot on a non-rooted device. So
`filterAvailable(..., deviceControlReady=)` is a separate axis from `requiredPermission`, and
`DeviceControlStatus` is a four-way diagnostic rather than a boolean — *not installed* / *not
running* / *permission denied* / *ready* each need a different action from the user, and a boolean
would render as an unexplained disabled switch.

**The three control tools default to OFF**, unlike the other nine, via
`DeviceToolToggles.DEFAULT_DISABLED_TOOL_IDS` which `SettingsStore` falls back to when the key is
absent. The setter reads the same default, so the FIRST toggle persists the whole effective set —
otherwise enabling one control tool silently enables the other two.

## The grant — `DeviceControlSession` owns "may an agent drive this phone"

`device_control_start` / `device_control_stop` are the lifecycle, and the fact they move lives in ONE
atomic class. It used to be four booleans derived independently at four moments (the FGS hold, the
tool list, the capability header, the toggles), so nothing owned it and nothing could say WHY it was
false — a boolean has nowhere to put a reason.

- **`DeviceControlGrant` is a sealed union, never a bare success flag.** `Granted` / `AlreadyActive`
  / three `Refused` arms, each carrying a message written for the person holding the phone. `Refused`
  is a closed SUB-union so a future in-app `declined_by_user` is one more `data object` and a compile
  error at every consumer, with no reshaping.
- **`Granted` means the binder is live, not merely permitted.** The status says Shizuku WOULD allow a
  bind; `start()` performs one. Reporting success on the status alone defers the failure to the first
  `device_ui` call, which is where it used to be discovered.
- **The two gate layers ask the SAME object two different questions.** `canTakeControl()` (advertise —
  reached through the existing `DeviceControlGate` binding) and `isActive()` (answer, in
  `executeOutcome`). One owner, so they cannot be derived independently the way the capability and
  the tool list once were.
- **The advertise layer is deliberately NOT gated on the grant, and that is a server constraint, not
  a preference.** `context.device_tools` binds `ClientDeclaredTool`s once per run, at run start
  (`backend.py` `_derive_tool_grants`; `ToolUseLoop` builds `_session_tools` in `__init__`). A grant
  taken mid-run therefore cannot add a tool to the run that took it — gating advertisement on it
  would mean `device_ui` is never bound in the run where `device_control_start` was called, which is
  a feature that can never execute. The ungranted refusal (`device_control_not_started`) is
  recoverable BY THE MODEL, unlike `tool_disabled`, and names the recovery.
- **The lifecycle pair is gated on the screen-control opt-in and nothing else** — not on Shizuku
  (naming the cause of a refusal is what it is for) and not on the grant (which it exists to create
  and release). It carries no Settings switch: one that disabled the gate while leaving the tools it
  guards enabled reads backwards. `DeviceToolTogglesTest` asserts that exemption rather than
  assuming it, so a THIRD un-toggleable tool cannot arrive silently.
- **The grant is released by the HOLD's idle bound in `notify/`, and `RunRepository` must not
  release it.** (This doc previously said the release point was the hold's epoch-end — that was
  wrong: an epoch ends roughly every 15s by design and releases nothing.) Two other seams have been
  tried and both were wrong for recorded reasons. A terminal FRAME is unreliable —
  `SessionStreamClient` may DROP `stream_end` under buffer pressure while still ending the loop.
  Upstream COMPLETION reads like "the run is over" but fires on every `WhileSubscribed` stop and on
  each of the hold's transport rebuilds, so a grant released there is revoked every few seconds by
  the very hold that protects it. The hold's own idle watch is the only unit that means "the agent
  has gone quiet", and it belongs to one owner: two independent opinions about how long an agent may
  drive the phone is the same disease this epic started with. Mechanics of the watch itself —
  the 15-minute bound, the two measured leaks it closed, and the structural work still open — live in
  [`notify/CLAUDE.md`](../../notify/CLAUDE.md) § "The device-control HOLD".
- **The arming predicate is evaluated at run START, so it must stay wider than "control is possible
  right now".** A grant can be taken part-way through a run — the model calls start, is refused, the
  user fixes Shizuku, the model retries — and by then the only chance to arm the hold has passed
  (Android forbids starting a foreground service from the background, which is where a run that
  drives the phone ends up). Whatever `deviceControlInPlay` reads must therefore cover every run in
  which a grant could LATER come to exist, or that grant has no owner to release it.
- **A binder death INVALIDATES a held grant — the intent and the substrate are separate facts and
  everything public reads both.** The Shizuku user service is `daemon(false)`, so it dies with its
  client process; measured, a server restart took the capability away mid-session and the only
  symptom was a tool count quietly changing. `_held` is the intent; `isActive()`/`active` are
  `_held && status.isReady`. A returning binder does NOT resurrect a dead grant — the agent that
  held it is gone and the user watched the notification disappear.
- **The answer-layer refusal is discriminated, and reuses `Refused`.** `NotStarted` (the model fixes
  it, in the same run) versus the substrate arms (the user fixes it). Collapsing them would report
  "call `device_control_start`" for a state in which start refuses identically — a loop neither
  party can break. `NotStarted` is the one arm `start()` never returns.
- `DeviceControlSession.active` is a `Flow` because the visible half of a grant must REACT to it —
  including to an invalidation nobody called `stop()` for. Two independent subscribers: the FGS hold
  and its persistent notification ([`notify/`](../../notify/CLAUDE.md)), and the on-screen glow
  ([`ui/control/`](../../ui/control/CLAUDE.md)). The grant publishes; both subscribe. Dependency
  still flows down. Nothing here reasons about the Shizuku server's uid, which is by turns shell or
  root depending on how it was started.

## The capture veil — `ScreenCaptureVeil`, and it is not only about screenshots

Declared here beside `AppForegroundChecker` and implemented up in `ui/control` (declared-DOWN, the
`RunNotifications` shape) because `data/` may never import a Compose surface. It hides whatever the
app draws over other apps for the duration of a block.

**`FLAG_SECURE` cannot do this job.** Measured at shell UID, one secure window makes the ENTIRE
`screencap` fail rather than hiding one layer — SurfaceFlinger refuses the whole framebuffer to a
caller without `CAPTURE_SECURE_LAYERS`. So suppression is temporal.

Two call sites, for two different reasons:

- **`DeviceUiHandler`, the capture only** — never the element read. `uiautomator` walks the
  accessibility tree, which the overlay is absent from anyway, so veiling there would flicker the
  window for no gain.
- **`DeviceActionHandler`, on `tap`/`swipe`/`type`** — because an injected touch is delivered to the
  **topmost window** at those coordinates, and the grant's own Stop pill is a window near the bottom
  centre. Without this, a tap aimed at a control underneath it presses Stop and ends the grant it is
  acting under. `type` belongs in that set because it taps to focus the field first; `key`, `launch`
  and `wait` never go through the screen and pay nothing.

The binding must stay free when nothing is drawn — a capture is already the expensive observation.

## Two-layer gate, one set of ids

Every tool is gated at BOTH layers so a stale server cannot slip one through:

- **Advertise:** `DeviceToolCatalog.availableTools` (`suspend`) intersects the `DeviceToolGate`
  disabled-id set AND runtime-permission availability, so a disabled or ungranted tool is never in
  `context.device_tools`.
- **Answer:** `DeviceToolExecutor.executeOutcome` refuses a disabled tool with a `tool_disabled` error,
  checked BEFORE handler lookup so it never reads as `unknown_tool`. The grant check sits AFTER the
  disabled one, and the order is the message: a tool the user switched off is not fixed by starting a
  grant, so telling the model to start one sends the user round a loop that cannot terminate.

`disabledDeviceToolIds` is stored at tool-id level even though the Settings UI shows
`DeviceToolToggles.GROUPS` clusters — the gate is exact. An empty set means every tool is enabled.

## Plain-JVM testability is the reason for every injected collaborator

`DevicePermissionChecker`, `DeviceClock`, `DeviceToolGate`, `AppForegroundChecker`,
`DeviceToolResultReporter`, `SmsInboxReader`, `NextAlarmReader` are narrow `fun interface`s bound in
[`di/DeviceModule`](../../di/CLAUDE.md). **Never inject `SettingsStore`/`Context`/`AuraApi` into this
package directly** — that is what keeps `DeviceToolCatalog`/`DeviceToolExecutor` unit-testable without
Robolectric. `DeviceToolExecutor` is bound to `DeviceToolDispatch` at exactly ONE place; nothing else
may inject the executor.

## The truncation contract — `DeviceReadPage`, and it belongs to every device READ

**A device read must distinguish "that is everything" from "that is the first N", and no reader may
invent its own way of saying so.** This is a contract, not an implementation detail, which is why
it lives in `DeviceReadPage`/`DeviceReadWindow` rather than inside the one handler that needed it
first.

Two measurements, from real sessions, and the second is why the rule is absolute:

1. A five-message global window answered a question about a 52-message conversation with
   marketing, a debt collector, a scam text and a receipt. Schema-valid JSON, right fields, wrong
   contents, nothing marking it incomplete. **The tool did not fail; the answer built on it did.**
2. Worse — the contacts that actually mattered were in a table with **no tool at all**. So a
   *perfect* SMS reader would still have reported "nobody has been in touch". Completeness is
   never something the harness can infer from a successful read.

A tool that errors is diagnosable; a tool that silently narrows is not.

**The envelope, fixed for every reader:** `{<items>, returned, offset, has_more}`, plus optional
`total`. `<items>` is named per table (`messages`, `calls`, `contacts`); the three fields around it
never change. A call-log reader spelling it `more_available` re-creates the per-capability drift
this cluster came from.

- **`has_more` comes from a one-row LOOK-AHEAD** — the reader is asked for `count + 1`
  (`DeviceReadPage.fetchLimit`) and the extra row is trimmed. Chosen over a mandatory `total`
  because a total costs a second count query over the whole table on every call, and a reader that
  cannot count cheaply must still be able to report truncation honestly. The trim happens BEFORE
  the render lambda runs, so a look-ahead row cannot leak into the response even by mistake.
- **`total` is optional and omitted when absent, never sent as `0`** — the model has no way to
  tell a real zero from "not measured". A reader whose table makes an exact count cheap reports it
  under that name rather than inventing one.
- **`DeviceReadWindow` holds the bounds that are ADVERTISED and ENFORCED in one object**, so
  `schemaProperties()` (what the catalog publishes) and `pageFrom()` (what the handler clamps to)
  read the same two fields. A schema promising `maximum: 50` over a handler clamping to 5 is a
  silent narrowing — the model asks for what it was told it could have and quietly receives less.
  Declare a window; never hand-write `count`/`offset` into a tool's schema.
- **`pageFrom` is TOTAL.** Missing, negative, oversized or non-integer args land on a usable page.
  A read that refuses on a fat-fingered offset teaches the model to stop paging, which costs more
  than the bad argument did.
- **`contact_name` is ONE primitive shared with the call log, not a per-table lookup.** Not built
  yet; when it lands it belongs beside `address` as a nullable field resolved through a single
  injected resolver, consumed identically by both readers. Two independent implementations of "who
  is this number" is the same drift again. The seam is documented on `SmsMessageRow`.
- **Not yet in the contract: a time window (`since`/`until`).** The call log will want one. It
  belongs on `DeviceReadWindow` when it arrives, not bolted onto one reader.

## The SMS read specifically

`count` is a PAGE SIZE (`DeviceReadWindow.SMS` — default 10, max 50), `offset` pages backwards,
`sender_filter` narrows to one conversation. Each row is `{address, direction, body, timestamp}`.
SMS passes no `total`: an exact one costs a second count over the whole mailbox.

- **Narrowing and paging happen in the READER, in one query, and that ordering is the bug.** The
  original shape pulled a fixed newest-first pool of 200 and filtered in Kotlin afterwards, so a
  conversation older than the pool was unreachable at any offset — filtering a truncated window can
  only ever return a subset of that window. `offset` is meaningful only against the same set the
  filter selects.
- **Read `Telephony.Sms.CONTENT_URI`, never `Inbox.CONTENT_URI`.** Verified on device: one
  12-message thread returns 12 rows through the former and 9 through the latter, with nothing in
  the response marking the 3 sent replies as missing. A thread read through the inbox URI silently
  drops the user's own half of it. `TYPE IN (inbox, sent)` still excludes drafts/outbox/failed — a
  draft is not something either party said.
- **`address` + `direction`, never `from`.** `from` is a lie on an outbound row (it is the
  recipient), and it is the kind of lie that reads as fact when quoted back as prose. Verified on
  device: `type` 1 = inbox, 2 = sent.
- **The `sender_filter` reaches SQL, so its `%`/`_` are escaped** (`LIKE ? ESCAPE '\'`, confirmed
  accepted by TelephonyProvider). Args are model output; an unescaped wildcard silently widens a
  one-conversation read back into a mailbox-wide one — the exact failure this tool closes.
- **The description is the model's only guide, so the paging contract lives IN it** — that `count`
  is a page size and not a per-conversation total, and that `has_more: true` means "you have not
  seen everything". The old description said "the most recent message(s)" and left the scope
  unstated; that ambiguity was half the defect, not a cosmetic issue, and it is test-pinned.

**Known gap, deliberately not built here:** there is no way to LIST conversations, so
`sender_filter` still assumes the address is known — a filter matching nothing is indistinguishable
from a person who never texted. A contacts tool is the real cure and carries its own
account-selection and idempotency design; it does not belong bolted onto a read.

## Laws

- **A subscriber is NOT an executor.** Dispatch lives and dies with `live()`'s upstream, not as a
  second subscriber; correctness comes from backlog-replay + `DeviceToolCallLedger.recordIfNew`, never
  from a terminal frame that may be dropped ([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)).
- **The activity-launch gate is not a process-importance test.** `canStartActivityNow` ORs process
  importance with `AssistOverlayPresence.visible`, because a showing `VoiceInteractionSession` pins the
  process at `IMPORTANCE_FOREGROUND_SERVICE` (125), never `IMPORTANCE_FOREGROUND` (100), while the
  launch itself is genuinely permitted. **A BAL-blocked start is a SILENT no-op** — `START_ABORTED` is
  mapped to `START_SUCCESS` before the caller sees it — so a stale-`true` flag reports
  `handed_to_clock_app: true` for an alarm the user never got. Every ambiguous path errs toward
  `false`. Full chain: [`voice/CLAUDE.md`](../../voice/CLAUDE.md) § "Device tools from the overlay".
- **Args are model output — a black box.** Handler arg parsing must be TOTAL and degrade honestly.
- **`DeviceToolResultReporter` never retries** — the wire forbids retrying a result POST
  (200/403/404/409/network are all terminal). It closes both `Response<ResponseBody>` body variants;
  the body is raw rather than a converted DTO, so no JSON conversion is attempted against a
  legitimately-empty 200 body.
- **`SetAlarmHandler`/`SetTimerHandler` results carry no confirming "note" field — the handoff IS the
  success signal.** A note claims certainty the handler does not have, and competes with the
  promoted-tool card the reducer builds from the success. `device_set_alarm`/`device_set_timer`/
  `device_send_sms` are in `PromotedTools` ([`data/model/CLAUDE.md`](../model/CLAUDE.md)), so their
  result renders as an action card ([`toolcards`](../../ui/chat/toolcards/CLAUDE.md)) with no editorial
  prose in the payload.
