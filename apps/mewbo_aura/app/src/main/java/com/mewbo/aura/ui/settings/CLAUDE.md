> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Settings Screen — ui/settings/

Scope: `ui/settings/` — `SettingsScreen`, `SettingsViewModel`/`SettingsUiState`, `ProjectPickerSheet`.
All persisted state is [`data/settings/SettingsStore`](../../data/settings/CLAUDE.md); this is the UI
over it.

## The 7-section reorg (2026-07-14)

`SettingsScreen` is grouped into seven icon-headed sections via `SettingsSectionHeader` (a `(text, icon)`
Row): **Connection** (Lock) · **Identity** (Person) · **Defaults** (Star — default project + per-surface
models) · **Voice & Motion** (Mic) · **Device capabilities** (Phone — set-default-assistant + SMS access
+ the per-tool `DeviceToolToggles.GROUPS` switches) · **Widgets** (AddCircle — renamed from "Experimental")
· **Debug** (Build, debug-only). The header icon is decorative (`contentDescription = null`) and the Text
carries `.semantics { heading() }` for TalkBack section-jumps. **Zero contract change** — `SettingsUiState`/
`SettingsViewModel`/tests were untouched; this was a `SettingsScreen.kt`-only reshuffle.

## Laws / seams

- **Per-surface model defaults** — "Default model — app" (`selectedModel`) and "— assistant overlay"
  (`overlayDefaultModel`), independently persisted. Both reuse the chat `ModelPickerSheet` +
  a lazy `ModelRepository` catalog load (like the project picker); `resolveModelDisplayName` (pure,
  tested) resolves the row caption, degrading to the raw id offline.
- **Device tools section** renders from `data/`'s `DeviceToolToggles.GROUPS` — each switch is checked iff
  its id is NOT in `disabledDeviceToolIds`. The persisted set stays tool-id-level even though the UI shows
  clusters (`SettingsSectionHeader`/`DeviceToolGroupLabel` are the two-tier headers). The gate itself is
  in [`data/device/`](../../data/device/CLAUDE.md).
- **`ProjectPickerSheet`** marks the ephemeral Temporary project with a DISTINCT
  `ChatIcons.TemporaryProjectScope` (Schedule/clock) glyph + a divider below it — chosen over an
  AutoDelete/trash glyph, which misreads as a delete affordance next to a selectable row. Real projects
  get `ChatIcons.ProjectScope` (Folder).
- Icons come from `material-icons-extended` first (2026-07-14; app-root CLAUDE.md § Iconography). Section
  glyphs were rebuilt with semantically-correct FILLED weights (matching the drawer's Filled weight).
- The Debug section's mock-backend toggle is debug-only ([`mock/CLAUDE.md`](../../mock/CLAUDE.md));
  `SlimTextField` (borderless, `accentPrimary` cursor) is the shared field idiom, reused by the rename
  pane.
