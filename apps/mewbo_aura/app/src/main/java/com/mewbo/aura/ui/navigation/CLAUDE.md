> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Navigation + Drawer — ui/navigation/

Scope: `ui/navigation/` — `AuraNavHost` (routes + the device-shape shell pick), `NavigationHost`
(the chrome abstraction), `AuraDrawerContent` (pure content, shared by both shells),
`SessionActionsSheet` (recents long-press). There is no standalone sessions list screen;
[`ui/sessions/`](../sessions/CLAUDE.md) holds only view-state + pure rail helpers. Visual laws +
provenance: [`DESIGN.md`](../../../../../../../../../DESIGN.md) (Recents rail).

## Two shells, picked by device shape

`AuraNavHost`'s `ChatHomeDestination` runs the ONE exhaustive `when (LocalDeviceShape.current)` this
package owns — the case `DeviceShape`'s own KDoc blesses (`data/device/CLAUDE.md`): it picks between
two whole navigation compositions, never a boolean asked at each control.

- **`HandheldChatHome`** — today's `ModalNavigationDrawer`, unchanged (anatomy below).
- **`TelevisionChatHome`** — a permanent left rail (`AuraSpacing.NavigationRail.width`, no scrim, no
  open/close state, no menu button). `ChatScreen`'s `onMenuTap` is `null` here — there is no drawer
  to reveal, so no button that would reveal it, and `ChatTopBar` renders no menu glyph when it is
  `null`.

**A rail is the right shape for a remote, and patching the modal drawer to work by remote was tried
in spirit and rejected.** A sheet has to be summoned (no summoning gesture on a remote), it lands
over content behind a scrim (nothing to see through on a television), and every press inside it then
has to happen inside a container focus must be prevented from leaking out of — none of that buys
anything at 960dp of width, where the rail simply fits. Keeping navigation permanently on screen
removes the open/close state, the scrim, the summoning button and the containment question outright:
focus moves left into navigation and right into the transcript, and the reported "I can never reach
Settings" cannot occur because Settings is never off screen.

`NavigationHost` (`ModalSheet(isOpen)` / `PersistentRail`) is the ONE difference between the shells,
expressed as data so `AuraDrawerContent` never asks what device it is on to answer it — the rows are
shared; only the frame differs.

- `isActive` — whether the content is on screen and should refresh its sessions + claim initial
  focus. A modal sheet's is its open/closed state; a rail's is always `true`.
- `containsFocus` — whether focus must be prevented from crossing the content's boundary. **`true`
  only for the modal sheet, and it acts in BOTH directions**: a sheet composed while closed still
  sits in the focus graph (so an unconditional `exit = Cancel` would trap a remote inside an
  invisible drawer — worse than the escape it fixes), and an open sheet sits over content whose rows
  must not be reachable through it. A rail is `false` — focus moving right into the transcript is the
  primary way it is used, and containing it would strand the remote in the navigation list.

## Routes

Start destination is `chat` (nullable `sessionId`) — there is NO standalone landing/sessions screen.
The assist-overlay **handoff modality is baked INTO the `chat` route** (`&handoffModality={}`) so it
survives `MainActivity` nulling the prop after `navigate()`. `search`/`settings` are pushed WITHOUT the
drawer (Gmail-style "drawer only on home"); the `orb-gallery`/`liveness-gallery` routes exist ONLY when
`IS_DEBUG_BUILD` (a per-variant const standing in for the disabled `BuildConfig.DEBUG`). Both
open-session and new-chat use `popUpTo(CHAT){inclusive} + launchSingleTop` (replace, never a second chat
entry). The **handoff draft** is a RAW `SavedStateHandle` value (arbitrary composer text isn't
URL-safe), consumed as a `StateFlow` (NOT a one-shot `LaunchedEffect(entry)` — a `launchSingleTop`
handoff onto the current entry reuses it and a plain effect would never re-fire).

## Drawer anatomy (handheld shell — the rail shares the rows, not the frame)

Wordmark → New chat → Search chats → **hairline divider** (`outlineHairline`) → `RecentsHeader` →
date-bucketed recents → **hairline divider** → pinned footer, over a `surfaceDrawer` (#0F1012) fill.
This is `AuraDrawerContent`'s content in both shells; what follows is the handheld sheet's own frame
and anatomy. The television differences are two, both about a remote's ONE-dimensional reach:

- **Settings is hoisted into the top action group** (New chat / Search chats / Apps), and the
  footer's own Settings icon button drops — one affordance per shape, never two. In the footer it sat
  AFTER an unbounded `LazyColumn`, which composes only what is on screen, so two-dimensional focus
  search into not-yet-composed recents rows overshot it entirely and Settings was unreachable by any
  path. The footer itself still renders (account line only) when there is a display name to show —
  dropping it would lose information with no other home in the rail.
- **Recents is capped at `TvRecentsCap` (6) unpinned rows** — a reach budget, not a design number:
  every row is one more D-pad press between the top group and anything below the list. Pinned rows
  are exempt (a pin means "keep this in reach"), and whatever the cap hides stays reachable through
  Search chats.

- **`ModalDrawerSheet` never casts a shadow by default** — m3 1.4.0 forwards `drawerTonalElevation` into
  an inner `Surface` that never sets `shadowElevation` (byte-verified vs `NavigationDrawer.kt`), and its
  tonal blend tints toward `accentPrimary` (wrong for a neutral rail). The drop shadow is applied via
  `Modifier.shadow(AuraSpacing.DrawerSheet.shadowElevation, RectangleShape, clip = false)`.
- **`surfaceDrawer` is its OWN token, not `surfaceSelected`** (which is the rail's selected-row pill fill
  — whole-canvas use would erase the highlight, DESIGN.md) nor `surfaceInput` (bubbles/composer/chips).
- `RecentsHeader` carries a leading `ChatIcons.Clock` (reused house glyph — kept over a swap to
  `Icons.Default.History` because an existing hand-rolled glyph being reused isn't a "new hand-roll") +
  a trailing `Icons.Default.FilterAlt` funnel from `material-icons-extended` (core has
  no filter glyph — the prior hand-ported path was deleted). Picked `Icons.Default.*` (Filled) to match
  every other drawer icon — one icon weight per surface. `.semantics { heading }` is on the Text ONLY,
  keeping the filter button an independent a11y node.
- **`RecentSessionRow` is a dedicated COMPACT composable, NOT `DrawerRow`** — title flush at `screenGutter`
  (the icon-slot `DrawerRow` indented titles ~36dp, DESIGN.md), liveness a TRAILING `accentPrimary`
  dot gated on `SessionSummary.running` (the wire's real signal, never `status`), placed trailing so the
  indent can't return. Archiving the OPEN session routes to a fresh new chat via `onNewChat`. A pinned
  row carries a small LEADING `textTertiary` pin glyph (before the title, `PinnedMarkerSize = 14.dp` —
  no `AuraSpacing` token covers it, same documented-gap pattern as `SessionActionsSheet`'s
  `ConfirmSpinnerSize`) — deliberately subdued, since the `PINNED` section header above it already says
  why the row is there; it must not compete with the trailing dot's `accentPrimary`, which signals
  something actionable (a live run) rather than a static state.

## `SessionActionsSheet` — Pin/Unpin / Rename / Archive, no Delete

The API has no session hard-delete. `ModalBottomSheet` over `SheetShape`/`surfaceInput`/56dp rows;
dismisses on success, stays OPEN with an inline error caption + retry on failure. Two device-verified IME
laws that must not be simplified: **auto-focus the rename field on pane entry with the full title
selected** (without it the first tap collapses the selection and overtype never happens) and **hide the
keyboard EAGERLY in `commit()` before dispatching AND in `onDispose`** (the dispose hook alone races the
window teardown on the commit path and strands the IME). `RenameMaxLength = 120` mirrors the server cap.
`ArchiveGlyph` is hand-ported (legacy) — now replaceable by `material-icons-extended`, though an existing
reuse is fine (app-root CLAUDE.md § Iconography).

**Pin/Unpin is the FIRST row**, using `material-icons-extended`'s `PushPin`/`PushPinOutlined` (filled
when pinned, matching the toggled-state convention). `onSetPinned(pinned: Boolean, onResult)` flips the
REQUESTED state (`!session.pinned`), not a locally-tracked toggle — the sheet re-renders from the
`SessionSummary` its caller passed in, same as Rename/Archive read `session.title`/`session.archived`.
