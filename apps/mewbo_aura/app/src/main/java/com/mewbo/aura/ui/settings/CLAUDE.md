> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Settings Screen — ui/settings/

Scope: `ui/settings/` — `SettingsScreen`, `SettingsSection`/`SettingsRow`/`StatusBadgeText`,
`SettingsStatus`, `AssistantRoleReader`, `SettingsViewModel`/`SettingsUiState`, `ProjectPickerSheet`,
`PermissionRequest`. All persisted state is [`data/settings/SettingsStore`](../../data/settings/CLAUDE.md);
this is the UI over it.

## The two laws this screen exists to uphold

**Colour is never the only signal.** Every state readout is a glyph AND a word AND a tint, in that
order of importance — `StatusTone` owns all three as members, so a new state cannot ship as a tint
alone. Colour alone is invisible to a colour-blind reader and can be flattened outright by a high
contrast mode. `StatusTone.Value` is the one glyph-less tone and deliberately so: it reports a value
the user chose (a model name, a count of enabled tools), not a state the system decides, so it must
not read as a status claim at all. `SettingsStatusTest` pins both halves.

**A status indicator that guesses is worse than none.** A wrong "granted" sends the user hunting for
a bug in Mewbo instead of a grant in Android, which is strictly worse than saying nothing. Anything
not reliably readable renders `StatusTone.Unknown`, and one unknown row drags its section's
collapsed summary to unknown too — a green header over an unknown row is the same wrong claim one
level up. The summary counts what IS granted and never asserts the remainder.

## Which states are knowable, and which are not

This table is the expensive part of this screen. Do not re-derive it; do not widen a claim without
re-measuring on a device.

| State | Read | Verdict |
|---|---|---|
| `POST_NOTIFICATIONS` | `NotificationManagerCompat.areNotificationsEnabled()` via `NotificationPermissionReader` | **Exact — and deliberately NOT a `DevicePermissionChecker` read, on any API level.** `POST_NOTIFICATIONS` is only a runtime permission from API 33; below that `checkSelfPermission` reports GRANTED unconditionally, which reads "enabled" for a user who switched the app's notifications off in system Settings. `areNotificationsEnabled()` reads the real per-app toggle everywhere, and on 33+ the platform keeps that toggle and the runtime grant in sync, so it stays correct above the version split too. Below 33 there is no dialog to request — the row's tap falls straight through to the app's own settings page instead of firing a no-op permission request. |
| `READ_SMS` + `SEND_SMS` | two `checkSelfPermission` reads, AND-ed | **Exact.** "Granted" means BOTH; one of two granted still reads Not granted, which is right — the tools need both. |
| Shizuku authorization | `Shizuku.checkSelfPermission()` → `DeviceControlStatus`, four-way | **Exact, and it is not an Android permission.** It asks the Shizuku SERVER; `pm grant` reads back as granted while the server still refuses (app-root CLAUDE.md). Only the server's answer is the gate. |
| `SYSTEM_ALERT_WINDOW` | `Settings.canDrawOverlays` via `OverlayPermissionReader` | **Exact — and deliberately NOT a `DevicePermissionChecker` read.** It is a SPECIAL, app-op-backed permission: the manifest declaration is not the grant, and `checkSelfPermission` does not consult the app-op that actually gates the window. Routing it through the runtime-permission seam would report granted for a window the window manager refuses. |
| Default assistant | `RoleManager.isRoleHeld(ROLE_ASSISTANT)` | **Exact on the devices measured** — verified in BOTH directions on the redroid container (role absent → "Not set", role held → "Active"). Falls to Unknown when there is no `RoleManager` or `isRoleAvailable` is false. |
| Whether stored credentials reach a server | one live `GET /api/models` probe | **Only after a probe.** Nothing anywhere persists a validated flag, so a fresh screen genuinely does not know and says "Not checked". |
| Whether the system will let you change the assistant from our row | — | **Not knowable.** The row states the role and the tap opens the system picker; it never claims the picker will succeed. |
| `REQUEST_INSTALL_PACKAGES` | `PackageManager.canRequestPackageInstalls()` | **Exact — and deliberately NOT a `DevicePermissionChecker` read**, for the identical reason as `SYSTEM_ALERT_WINDOW`: it is a SPECIAL, app-op-backed permission, so the manifest declaration is not the grant and `checkSelfPermission` does not consult the op that actually gates the install. There is no dialog and therefore no result callback, so the resume re-read is the only thing that can notice a grant. |
| Whether a newer release exists | one live release-list request per explicit check | **Only after a check, and "failed" is its own answer.** Nothing persists a last-known result, so a fresh screen says "Not checked". An unreachable forge renders `Check failed`, NEVER `Up to date` — and a newer release publishing no file for this device renders `No build for this device`, which is neither. Rationale + the measured public-mirror case: [`data/update/`](../../data/update/CLAUDE.md). |
| Whether the system exposes a screen for "Install unknown apps" | whether the intent RESOLVES, at the moment of the tap | **Answered at runtime, deliberately not as a `DeviceShape` member.** A member would have to state a value for `Television`, and nobody has run this on a Fire TV or Android TV — an unmeasured member is a guess wearing a type. `ApkInstaller.openInstallPermissionScreen()` returns `false` when nothing resolves and the row says so, the same posture `PermissionRequest.openOverlaySettings` takes. |
| Whether a denied permission will prompt again | `shouldShowRequestPermissionRationale` + a remembered "asked" flag | **Partial, and deliberately not surfaced.** `PermissionRequest` uses it to route the tap; no row renders a claim about it. |

**Presence is not validity.** A saved URL and key say only that someone typed something once. The
server may have moved and the key may have been rotated, and the row still renders as configured —
which is why `SettingsViewModel.checkStoredConnection()` probes on screen entry. Cost is `O(1)`: one
request per entry behind `ConnectionProbe`'s 5s timeout, skipped when no URL is stored, and it never
persists anything, so a probe against stale credentials cannot overwrite them. "Save anyway" drops
the status back to unchecked — saving past a failure proves nothing about the server.

### Two device traps that produce a FALSE NEGATIVE on the assistant role

Both cost real time and neither reflects a real device.

- **`adb install -r -d` silently clears the assistant role holder.** A reinstall-then-check reads
  "Not set" for a role that was held before the install, so verifying this row immediately after a
  deploy measures the installer, not the reader.
- **The redroid container revokes the role again within a minute or two.** Aura does not qualify as
  an assistant there, most likely because AOSP ships no `SpeechRecognizer` (app-root CLAUDE.md,
  device matrix). Run `cmd role set-bypassing-role-qualification true` before
  `cmd role add-role-holder --user 0 android.app.role.ASSISTANT com.mewbo.aura`, or the holder
  evaporates between the grant and the screenshot.

## Structure — eight collapsible sections, all closed on open

`SettingsSection` is a hairline-BORDERED card over `surfaceCanvas`, never a filled one: the design
language ranks by type, gutters and hairlines rather than by nested fills (DESIGN.md §1.1), and a
filled surface would fight the switches and status glyphs inside it for contrast.

**Connection and identity** (Lock) · **Defaults** (Star) · **Voice & Motion** (Mic) · **System
permissions** (Security) · **Device tools** (Tune) · **Widgets** (AddCircle) · **About app** (Info) ·
**Debug** (Build, debug-only). Every heading is an a11y heading with a `stateDescription` of Expanded/Collapsed;
`SectionExpansion` holds which are open and carries its own `Saver`, so a rotation does not slam
every card shut under the user.

- **The split of permissions from tools is not cosmetic.** Android grants the first set and Mewbo
  can only ask; the user owns the second set outright. They fail differently, so they are answered
  differently — reading as one list is what made "Screen control: Ready" look like a switch rather
  than a grant.
- **A collapsed section still reports its state** (`summary`), so folding the screen down hides
  controls without hiding facts. That is what makes collapsed-by-default safe.
- **The summary sits UNDER the title in the header, never beside it.** A `Row` measures its
  unweighted children first, so a badge sharing the line claims the width it wants and squeezes the
  weighted title toward zero — this rendered "Connection and identity" one character per line behind
  a long transport error. Stacking removes the competition instead of tuning weights against the
  longest string anyone might one day put in a badge.
- **`ConnectionStatus.Failed`'s badge says "Not reachable" and NOT the reason.** A collapsed header
  is a glance; the full reason renders in an `ErrorCard` inside the expanded card, where there is
  room. That card carries no "Save anyway" when the failure came from the screen's own probe — the
  credentials are already stored, so there is nothing to save.

## Laws / seams

- **Every control with non-obvious reach carries a purpose caption**, always visible rather than
  behind a tooltip. A control whose scope is invisible is not helped by an explanation that is also
  invisible. It is a phrase, never a sentence; anything needing a sentence goes in the section's own
  `caption` instead.
- **Per-surface model defaults** — "Default model" (`selectedModel`, new in-app sessions) and
  "Assistant overlay model" (`overlayDefaultModel`), independently persisted. Both reuse the chat
  `ModelPickerSheet` + a lazy `ModelRepository` catalog load (like the project picker);
  `resolveModelDisplayName` (pure, tested) resolves the row caption, degrading to the raw id offline.
- **The device-tools section renders from `data/`'s `DeviceToolToggles.GROUPS`** — each switch is
  checked iff its id is NOT in `disabledDeviceToolIds`. The persisted set stays tool-id-level even
  though the UI shows clusters. `DeviceToolGroupGlyphs` maps the canonical group TITLE to a glyph and
  falls through to a generic one for a title it does not know, the same forward-compatible posture
  `ActivityToolGlyphs` takes for an unknown tool id. The gate itself is in
  [`data/device/`](../../data/device/CLAUDE.md).
- **"Volume boost" is a stepped PICKER, not a slider, and its status claim is deliberately
  one-sided.** A Compose `Slider` is draggable and its D-pad behaviour has never been measured on a
  remote here, while a list of rows is the one selection vocabulary this screen already traverses
  correctly; coarse steps are also what a control operated from across a room needs. The trailing
  slot shows the chosen level as `StatusTone.Value` (a value the user set, no claim). **A badge
  replaces it ONLY for a measured refusal**: a successful attach is not proof the boost is audible,
  because an on-device TTS engine that plays its own audio never sees the session id the effect
  hangs on — so "Supported" would be the wrong-green this screen exists to prevent, and
  `SpeechBoostState.Applied` therefore renders as nothing. The level comparison lives on
  `SpeechBoostState.refuses`, so a refusal of a level the user has since changed cannot leak through
  as a current one. Mechanics: [`voice/`](../../voice/CLAUDE.md).
- **A screen-control switch stays disabled until Shizuku is ready**, captioned "Waiting on Shizuku
  access". An enabled-looking switch reads as "this works", so leaving it live while Shizuku is down
  makes the screen claim a capability the session does not have. The stored INTENT is untouched.
- **Settings is the ONLY surface for the Shizuku grant** — no banner, modal, or first-run
  interstitial anywhere.
- **Every not-ready state leads somewhere.** A row wired straight to a launcher does NOTHING on a
  permanently-denied permission, with no dialog and no message, which is indistinguishable from a
  broken button. `PermissionRequest.canPrompt` routes the tap to the system dialog while it can
  still appear and to the app's own settings page once it cannot.
- **Live OS reads are refreshed on RESUME, not on composition.** Granting a permission, setting the
  assistant and starting Shizuku all happen in another app, so the moment that matters is the user
  coming back; a one-shot effect leaves every row showing the state from before they left. For
  **"Display over other apps" the resume read is the ONLY one there is** — a special permission has
  no dialog and therefore no result callback, so `openOverlaySettings()` is the request and nothing
  reports back when it is granted.
- **This screen is the ONLY place `SYSTEM_ALERT_WINDOW` is ever asked for**, and without it the
  device-control overlay is inert: `raise()` returns early on `canDrawOverlays`, so the glow, the
  narration bubbles and the Stop pill never appear anywhere while an agent drives the phone
  ([`ui/control/CLAUDE.md`](../control/CLAUDE.md)). Nothing fails and nothing logs — device control
  works in full, silently. That is why the row's caption names the CONSEQUENCE ("Shows on-screen
  when an agent is driving your phone") rather than the mechanism, and why the row is labelled after
  the system toggle it opens rather than after a capability.
- **On a device with no reachable overlay screen — `DeviceShape.hasOverlayPermissionScreen == false`,
  measured Fire OS behaviour — the row's system-intent tap can never succeed, so it falls back to a
  Shizuku-backed grant instead of doing nothing.** `SettingsViewModel.grantOverlayPermissionViaShizuku`
  calls `ShizukuOverlayGrant.grant()` (`data/device/shizuku/CLAUDE.md`'s parent package), which writes
  the `SYSTEM_ALERT_WINDOW` app-op through the same shell-UID channel device control already owns —
  `appops set … allow`, not `pm grant`, because `Settings.canDrawOverlays` checks the app-op first and
  only falls back to the manifest permission when that op is untouched, so `pm grant` cannot move it.
  The system intent (`PermissionRequest.openOverlaySettings`) stays the row's PRIMARY tap wherever it
  works; the Shizuku route is the fallback for where the screen that owns the toggle cannot be reached
  at all, and it is not gated on `DeviceShape` — a screen you cannot reach is the same problem on a
  kiosk build or a stripped AOSP handheld. Verified by a second `canDrawOverlays` read after the
  write, never by the shell command's own exit code — a zero exit says the command parsed, not that
  the window manager will now allow a window. Every outcome, success or refusal, carries a sentence
  written for the person holding the device (`OverlayGrantOutcome`), because a tap that does nothing
  and says nothing is the outcome this screen never ships.
- **The manual OS reads travel as ONE value (`SystemPermissions`), not one flow each.** Both
  `combine` groups in `SettingsViewModel` sit at kotlinx's five-source arity cap, so a sixth flow
  does not compile — and the reads that belong together are the ones ONE call refreshes, not the
  ones that render side by side. Grouping by what ASKS bought headroom in the capability group and
  at the top level from one change, and made a refresh a single emission rather than four.
- **A row on screen and a row counted in the section header are one fact** — `systemPermissionTones`
  is that list, and `permissionSummary` folds it. Adding a row to the section without adding it here
  leaves the header reading "All granted" over a permission that is not, which is the wrong-green
  the whole screen exists to prevent and is silent. `SettingsStatusTest` pins it.
- **"About app" is the ONLY surface for the in-app updater**, and it is deliberately a Settings
  section rather than a banner, a badge on the drawer, or a first-run interstitial — the same
  posture the Shizuku grant takes. It renders `AppUpdateState` through `updateBadge`; the lifecycle
  itself lives in a `@Singleton` repository on the application scope, so **leaving Settings does not
  cancel a hundred-megabyte download** and returning does not restart it.
  [`data/update/`](../../data/update/CLAUDE.md) owns every rule the section merely displays.
  - The check runs on first composition **only while the state is `NotChecked`**. The repository
    outlives the screen, so an unconditional `LaunchedEffect(Unit)` would re-probe a settled result
    or interrupt an in-flight one on every re-entry.
  - `Unsupported` (a build with no release source) renders NON-clickable, not disabled-looking. A
    tap that silently does nothing is the outcome this screen never ships, and there is no screen to
    fall through to.
- **`ProjectPickerSheet`** marks the ephemeral Temporary project with a DISTINCT
  `ChatIcons.TemporaryProjectScope` (Schedule/clock) glyph + a divider below it — chosen over an
  AutoDelete/trash glyph, which misreads as a delete affordance next to a selectable row. Real
  projects get `ChatIcons.ProjectScope` (Folder).
- Icons come from `material-icons-extended` first (app-root CLAUDE.md § Iconography), all Filled
  weight to match the drawer.
- The Debug section's mock-backend toggle is debug-only ([`mock/CLAUDE.md`](../../mock/CLAUDE.md));
  `SlimTextField` (borderless, `accentPrimary` cursor) is the shared field idiom, reused by the
  rename pane. Its vertical padding is a full `internalPadding` rather than the tight gap it carried
  before — the name field's subtext sat hard against the boundary underneath it and read as crowded
  into the border.
