> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shared UI Vocabulary — ui/common/

Scope: `ui/common/` — the small composables shared across surfaces. Each has ONE key invariant/trap.

- **`ActionSheet`** — the shared long-press-sheet CHROME (`SheetHeader`/`SheetActionRow(enabled)`/
  `SheetErrorCaption`), consumed by both `MessageActionsSheet` and `SessionActionsSheet` (which is where
  they originated). The CONTAINER is `AuraBottomSheet` (below), not `ActionSheet` — the two sheets
  differ in structure (pane machine vs flat rows), so chrome stays shared while structure stays each
  sheet's own. **Do not treat the container as "only a lambda pass-through" and leave it unshared:**
  bounding, scrolling, and insets are load-bearing shared behavior, and their absence independently
  produced the same overflow defect in every sheet (DESIGN.md). `SheetErrorCaption` keeps the
  sheet OPEN on failure.
- **`ProjectRowKind`** — what a project row IS (`Saved`/`Temporary`/`Auto`), carrying its glyph and
  tint as MEMBERS. Three readers: the two project pickers (`ui/settings/ProjectPickerSheet`, the
  app-wide default, and `ui/composer/ComposerOptionsSheet`'s project pane, the per-chat one) render
  structurally different lists over the same three kinds, and `ChatTopBar`'s project line is the third
  — the glyph-and-tint `when` lives here so it is not three copies in three packages edited for
  different reasons. `Auto` wears the PROJECT tint, not `Temporary`'s muted one: an auto session ends up in a real
  project, it just is not in one yet. `AutoRowLabel`/`AutoRowCaption` are here for the same reason —
  two spellings of one mode read as two modes.
- **`AuraBottomSheet`** — the ONE shared sheet CONTAINER: viewport-capped height, internal scrolling,
  system-bar insets, the top-corners-only shape, and the container color, owned in one place instead of
  re-derived per sheet. Two entry points share the one private frame: `AuraBottomSheet` (a scrollable
  `Column`, for bounded content — short content still wrap-content sizes, a 2-row action sheet stays
  small) and `AuraListBottomSheet` (a `LazyColumn`, for unbounded, data-driven content like the model
  catalog or the project list). `ModalBottomSheet` is never called directly anywhere else — enforced by
  `SheetContainerContractTest`.
- **`MarkdownMessage`** — the ONE assistant-text render path (streaming AND finalized; `WordFadeText`
  retired). Two mandatory anti-flicker mechanisms (DESIGN.md): `rememberMarkdownState(retainState =
  true, immediate = true)` (the default resets to a blank `State.Loading` per content change — that
  blank-flash WAS the flicker); and `rememberStreamedText(text, isStreaming)` throttling the reparse to
  `AuraMotion.markdownStreamReparseMs` (~20Hz) while streaming and passing finalized text through
  byte-identically. The caller MUST run `MarkdownBuffer.sanitize` first. Custom layout emits type-aware
  Spacers only BETWEEN blocks (never before the first — `topGapAfterBubble` owns the space above); h4–h6
  reuse `bodyMessage` (heading by placement only).
- **`MarkdownBuffer.sanitize`** — a pure defensive closer (NOT a parser) at the streaming→finalized swap:
  closes half-open ``` fences, `**` bold, `*` italics so a truncated buffer can't swallow the rest of the
  transcript into one code block. A fence only closes when it starts its own line; italic counting
  excludes CommonMark list-marker `*` bullets.
- **`ErrorCard`** — a quiet inline error (warning glyph + reason + optional Retry text-button), NO red
  fills. `onRetry == null` renders without the button — the composable does not know WHY (the caller's
  `ChatUiState.sessionEnded` decides; a terminated session 410s every retry). There is no dedicated
  `error()` `SessionEvent` type, so callers always arrive with an already-resolved human reason.
- **`NoticeHost`** — the one-line-at-a-time notice; `show()` REPLACES (no queue); `lastText` is held
  separately so the exit fade plays against the last message instead of snapping blank. **Needs explicit
  `.imePadding()`** or it renders behind the keyboard.
- **`TypingIndicator`** — 3 phase-offset pulsing dots (reads as a wave). The `Row` sets
  `verticalAlignment = CenterVertically` EXPLICITLY (DESIGN.md: shared row composables must, or the
  trailing label misaligns). Dots inherit `LocalContentColor` — color is the caller's, not themed here.
- **`AttachmentTile`** — metadata-only (`filename`, `mimeType`); NEVER a real thumbnail (no Coil/AsyncImage
  anywhere in the app). `AttachmentGlyphs` is the ONE home of the `image/`-mime check, shared with the
  composer's pre-send chip.
- **`ChatOverflowMenu`** — the one overflow-menu style (`surfaceSelected`, 20dp radius); v1 ships only
  working items, no disabled stubs.

## The D-pad layer — five files now, and none of the first four is gated on being a television

Focus is not a touch state: a finger never grants it, so a handheld renders none of this unless a
keyboard or remote is attached. Gating on `TelevisionChecker`/`DeviceShape` would buy nothing and add
a code path that only runs on hardware nobody in the loop is holding, for the three files below that
say so. `LocalDeviceShape` (below) is read only where a control's affordance genuinely differs by
device — `ImeOnConfirmOnly` is that narrow case, not a general license to gate focus mechanics on it.

- **`FocusRing` (`Modifier.auraFocusRing`)** — the one visible focus state, tokens from `ui/theme/`
  (`AuraColors.focusRing`, `AuraSpacing.Focus`). **⚠️ It must precede the click modifier.**
  `onFocusChanged` observes only focus targets that FOLLOW it in the chain, so
  `Modifier.clickable{}.auraFocusRing()` compiles, draws nothing, and warns about nothing — an entire
  wave of ring calls shipped in that order and every one was dead code. A Material
  `IconButton`/`Switch`/`Button` applies its click after its `modifier`, so passing the ring as that
  component's `modifier` is already correct; only hand-built `Box`/`Row` chains can get it wrong.
  Where the click arrives inside a caller's `modifier` parameter, the ring leads:
  `Modifier.auraFocusRing().then(modifier)`.
- **`DpadFocusEscape` (`TextFieldFocusEscape` + `Modifier.dpadFocusEscape`)** — lets a remote out of a
  text field, which is the single reason the app was navigable by nothing at all before it existed.
  The decision is a pure function of key (+ selection + length, where a caret exists), so the whole
  trade table is testable without composing a field. Two overloads share one key-handling body
  (`dpadEscape`), differing only in which table they consult:
  - **`dpadFocusEscape(selection, textLength)`** — for a field backed by `TextFieldValue`. Up/Down
    always escape (a wrapped draft has no reliable last-line signal, and re-trapping the remote is
    the unbounded failure); Left/Right escape only from a collapsed caret at the boundary, so
    in-text editing survives.
  - **`dpadFocusEscape()`** — the no-selection form, for a field backed by a plain `String` (no caret
    to consult): every arrow escapes, horizontal included. A strictly worse trade than the overload
    above, taken only where the richer one is unavailable — guessing "not at the boundary" would
    re-trap the remote (the unbounded failure), while guessing "at the boundary" costs only
    within-text caret movement in a single-line field. **Do not "fix" a caller onto the richer
    overload by converting its `String` state to a `TextFieldValue`** — that moves selection
    ownership into the field and reintroduces cursor-jump on every external state change, a worse
    regression than the one being traded.
- **`ImeOnConfirmOnly` (`Modifier.imeOnConfirmOnly`)** — separates "this field is focused" from
  "the user wants to type", the fact `dpadFocusEscape` alone does not fix. A Compose field raises the soft keyboard the
  moment it gains focus; on a handheld that is correct (focus only ever arrives from a tap), but a
  remote's D-pad traversal moves focus THROUGH a field on the way past it, so merely navigating
  raised a full-screen IME — and BACK dismisses that IME instead of moving focus, so the remote
  oscillated and never got past the field even with the escape modifier applied. On television the
  keyboard now opens only on an explicit confirm (`DirectionCenter`/`Enter`/`NumPadEnter`); BACK then
  closes it and leaves focus ON the field, so the next arrow navigates normally. **On a handheld this
  returns the receiver completely unchanged** — no focus observer, no key handler added — gated on
  `LocalDeviceShape.current.opensKeyboardOnFocus`, and safe as an early return for the same reason
  that local is `static`: a device does not stop being a television, so the composition never takes
  the other arm later. Applied on every text field in the app now (composer, settings fields,
  search, question-card answers, session rename), not only the composer.
- **`DpadFocusContainer`** — a `focusGroup` + `exit = Cancel` wrapping every route, so focus cannot
  leave the rendered tree. **Do not re-add a recover-after-the-fact leg: it cannot work.** One was
  built, wired at the navigation host, and observed doing nothing — Compose dispatches no key event
  at all once nothing holds focus, which is exactly the state it would need to recover from. It was
  deleted rather than left in place looking like a safety net. This wrapper is a backstop; the one
  strand actually reproduced on a device was cured at its source, the composer refusing a downward
  move (`ui/composer/`).
- **`LocalDeviceShape`** (`LocalDeviceShape.kt`) — `staticCompositionLocalOf<DeviceShape>`, provided
  once by `MainActivity` from `DeviceShape.of(televisionChecker)`. Defaults to `DeviceShape.Handheld`,
  the safe direction: a preview, test, or future host that forgets to provide it renders the touch
  design, which is merely wrong-looking on a television, where the reverse would hide affordances a
  finger needs. Carries the device differences as MEMBERS (`opensKeyboardOnFocus`,
  `hasOverlayPermissionScreen` — `data/device/CLAUDE.md`) rather than a
  boolean answered per call site. **The ONE device-shape seam left in the UI layer** — it replaced an
  earlier `LocalIsTelevision` boolean local outright (deleted, not deprecated); every reader in this
  package, `ImeOnConfirmOnly` included, now asks a `DeviceShape` member instead of a raw boolean.
