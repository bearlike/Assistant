> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Theme Tokens — ui/theme/

Scope: `ui/theme/` — `AuraTheme`, `AuraColors`, `AuraType`, `AuraShape`, `AuraSpacing`, `AuraMotion`,
`AssistantExtras`. **DESIGN.md is canonical for the VALUES + their provenance; this file documents the
MECHANISM and the token-discipline law.** When a value here disagrees with DESIGN.md, DESIGN.md wins.

## Token discipline (enforced in review)

**No `Color(`, dp/sp radius literal, or `spring(` outside `ui/theme/`.** `AuraColors`/`AuraOrbColors` are
the only design-language `Color(` sites; `AuraMotion` is the only `spring(...)` construction site (M1–M8
named springs — including the `Color`-typed `composerMorphColorSpring`, because spring construction is
reserved here). Need a new value? Add a token, then consume it — literals ship looking right and rot the
first palette change. `AuraSpacing` dp values carry provenance in KDoc (Rev C estimate / Rev F measured /
real-device capture / a dated user directive); governance when sources conflict is DESIGN.md §1's
precedence ladder — the newest directive wins over any measured reference.

## The two mechanism facts

- **`MaterialTheme`, NOT `MaterialExpressiveTheme`/`MotionScheme`** — both are `internal` in m3 1.4.0
  (compiler-verified; `javap` misleadingly shows them public — Kotlin visibility is frontend-enforced).
  `AssistantExtras` is a CompositionLocal carrying exactly TWO fields: `orbPalette: (OrbState) -> List<Color>`
  and `reducedMotion: Boolean` (`glassSheet`/`GlassSheetStyle` and `scrimColor` were DELETED). Provided
  ONCE by `AuraTheme`; `LocalAssistantExtras` throws if not wrapped.
- **`reducedMotion` is OR-ed from two sources in the ONE seam** `AuraTheme`:
  `effectiveReducedMotion = reducedMotion || systemReducedMotion` where `systemReducedMotion` reads
  `Settings.Global.ANIMATOR_DURATION_SCALE == 0f`, in `remember(context)` (once per composition; a
  ContentObserver is deliberate YAGNI). **Trap:** `AuraTheme(reducedMotion = …)` is a defaulted param, so
  a bare `AuraTheme { }` silently drops the in-app toggle — both hosts (`MainActivity`, `AuraSession`)
  must thread it (this bit `AuraSession` for a full release cycle).

## New tokens this wave (2026-07-14) — see DESIGN.md for canonical values

- `AuraColors.surfaceDrawer` (#0F1012) — the left rail's fill, midway canvas→surfaceInput. Its OWN token,
  never `surfaceSelected` (the selected-row pill fill — whole-canvas use erases the highlight, §7.8) nor
  `surfaceInput` (bubbles/composer/chips).
- `AuraColors.scopeProject` (#B79CE8 amethyst) / `scopeTool` (#5CC8D6 cyan) — the composer scope-row +
  picker GLYPH tints (never body text); a cool violet↔cyan pair distinct from `accentPrimary`/`accentError`
  and clear of the rejected yellow/brown/green.
- `AuraSpacing.Composer.scopeRowStartInset` (= `horizontalMargin + height/2` = 48dp) — the "where the
  pill's stadium cap becomes the straight line" anchor; `scopeRowIconSize` (16dp, one step down from the
  24dp `iconSize`).
- `AuraSpacing.DrawerSheet.shadowElevation` (16dp) — documents that `ModalDrawerSheet` casts NO drop
  shadow by default (tonal-only, tinted toward `accentPrimary`), so the shadow is applied via
  `Modifier.shadow(...)` at the call site ([`ui/navigation/CLAUDE.md`](../navigation/CLAUDE.md)).
- `AuraMotion.transcriptItemPlacementSpring` (`FiniteAnimationSpec<IntOffset>`, `DampingRatioNoBouncy` /
  `StiffnessMediumLow`) — the placement spring for the transcript `LazyColumn`'s `Modifier.animateItem`,
  smoothing a live turn's mount/shuffle/unmount reflow (DESIGN.md §7.23). Reduced motion
  drops the placement travel (rows snap); the opacity fades stay. It is a `spring(...)` construction, so
  it lives here in `AuraMotion` per the discipline law above, never at the `ChatTranscript` call site.
