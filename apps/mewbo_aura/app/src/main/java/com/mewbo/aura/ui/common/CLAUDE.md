> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shared UI Vocabulary — ui/common/

Scope: `ui/common/` — the small composables shared across surfaces. Each has ONE key invariant/trap.

- **`ActionSheet`** — the shared long-press-sheet CHROME (`SheetShape`/`SheetHeader`/`SheetActionRow(enabled)`/
  `SheetErrorCaption`), consumed by both `MessageActionsSheet` and `SessionActionsSheet` (which is where
  they originated). The `ModalBottomSheet` CONTAINER is deliberately NOT shared — the two sheets differ
  in structure (pane machine vs flat rows); chrome shared, structure each sheet's own. `SheetErrorCaption`
  keeps the sheet OPEN on failure.
- **`MarkdownMessage`** — the ONE assistant-text render path (streaming AND finalized; `WordFadeText`
  retired). Two mandatory anti-flicker mechanisms (DESIGN.md §7.16): `rememberMarkdownState(retainState =
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
  `error` `SessionEvent` type, so callers always arrive with an already-resolved human reason.
- **`NoticeHost`** — the §6.9 one-line-at-a-time notice; `show()` REPLACES (no queue); `lastText` is held
  separately so the exit fade plays against the last message instead of snapping blank. **Needs explicit
  `.imePadding()`** or it renders behind the keyboard.
- **`TypingIndicator`** — 3 phase-offset pulsing dots (reads as a wave). The `Row` sets
  `verticalAlignment = CenterVertically` EXPLICITLY (DESIGN.md §7.11: shared row composables must, or the
  trailing label misaligns). Dots inherit `LocalContentColor` — color is the caller's, not themed here.
- **`AttachmentTile`** — metadata-only (`filename`, `mimeType`); NEVER a real thumbnail (no Coil/AsyncImage
  anywhere in the app). `AttachmentGlyphs` is the ONE home of the `image/`-mime check, shared with the
  composer's pre-send chip.
- **`ChatOverflowMenu`** — the one overflow-menu style (`surfaceSelected`, 20dp radius); v1 ships only
  working items, no disabled stubs.
