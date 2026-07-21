# Mewbo Aura — Design System (canonical)

> ↑ [app root CLAUDE.md](CLAUDE.md) · consumers: every file under `app/src/main/java/com/mewbo/aura/ui/`
>
> This file is the **single canonical statement of the design system**: the laws, the token
> values, their provenance, and the regression registry. The scoped CLAUDE.md files carry the
> per-package mechanics; when they and this file disagree, THIS file wins and the CLAUDE.md is
> stale — fix it. Every visual change to the app must be checked against §7 (regressions that
> must never recur) before it ships.

Provenance shorthand used below: **[R1]** = user directive 2026-07-04 round 1 (compact design
language), **[R2]** = 2026-07-04 round 2 (breathability + aura correction), **[R3]** = 2026-07-04
round 3 (footer under every completed response; 2× breathability — doubled turn band + doubled
bubble→response gap), **[R4]** = user directive 2026-07-10 (overlay presence redesign —
pill mass: overlay-only height/action-circle/type-scale tokens; context-preserving scrim gradient;
3-phase invocation bloom + session resting glow; a11y channel),
**[R5]** = user directive 2026-07-11 (device feedback on 0.0.30-debug: the overlay
aurora must be a pretty MULTI-HUE field [blue + violet + ember], FLUID toward the edges [2-octave
wave + per-row liquid level], and STRONGER at the edges [persistent edge-lit perimeter floor]; plus
a fluid synthesizer-style RMS voice bar),
**[Ref]** = measured reference-capture
values, **[Rev F]** = instrumented uiautomator audit. Precedence: **the
newest user directive wins over every measured reference value**. Do not "fix" a directive value
back toward reference parity — record a new directive instead.

## 1. Philosophy

1. **Hierarchy is structural, not decorative.** Rank is expressed by the type scale, indentation
   (gutters), and hairlines — never by nested filled cards, and never by ad-hoc size/color
   literals at call sites.
2. **Space is a language ("space philosophy") [R2].** Separation between conversational units is
   deliberate and generous: border + space, not whitespace alone, and never cramped. When in
   doubt, more air between turns, less chrome around content.
3. **State must be glanceable.** The user must always be able to tell *running* from *dead* at a
   glance (spark + aura contract, §5). The app's *resting* state is calm: solid background, no
   ambient motion.
4. **The data structure enforces the layout.** Ordering guarantees (activity above response,
   footer last, disclaimer at the very end) live in `TranscriptReducer` and are contract-tested —
   the UI must never re-derive or “fix” ordering positionally.
5. **Tokens are the only source of values.** No `Color(`, dp/sp literal, or `spring(` outside
   `ui/theme/` (long-standing law). Every token carries its provenance in KDoc.

## 2. Typography (Figtree, one family app-wide; dark-only v1)

| Token | Size / line-height | Weight | Provenance |
|---|---|---|---|
| `greetingDisplay` | 32 / 40 | Normal | spec §4 |
| `titleBar` / `wordmark` | 22 / 28 · 24 | Medium | spec §4 |
| `bodyMessage` | **16 / 24** | Normal | [R1] size · [R2] line-height |
| `composerOverlay` | **20 / 26** | Normal | [R4] overlay-only composer field + invitation |
| `listItem` | **16 / 23** | Normal | [R1] · [R2] |
| `markdownH1 / H2 / H3` | **24/32 · 20/28 · 18/26** | N · Med · Med | [R1] (measured ramp scaled one step down) |
| markdown H4–H6 | = `bodyMessage` verbatim | — | [Ref] (heading by placement only) |
| `sectionHeader` / `metaTrailing` | **14** | Normal | [R1] |
| `chipLabel` | **13** | Normal | [R1] |
| `toolCardDisplay` | **36 / 44** | Normal | action-card payload line (§6); GMS reference capture 2026-07-12 |
| `caption` | **12 / 17** | Normal | [R1] · [R2] |

Laws: user bubbles and assistant text share `bodyMessage` (voice is distinguished by the bubble
surface, not by type). Tool-row **titles** are `chipLabel` + monospace; their **summaries** are
`caption` + `textTertiary` — one visible step down in both size and color [R1]. Font SIZES are
settled [R2: "the font looks about all right"] — future breathability tuning adjusts LINE HEIGHT
and spacing, not size.

## 3. Space & rhythm (all values in `AuraSpacing`)

| Token | Value | Meaning / provenance |
|---|---|---|
| `screenGutter` / `AssistantText.gutter` | 24dp | content gutter; also the chip-family indent [Rev F] |
| `AssistantText.topGapAfterBubble` | **32dp** | bubble→reply air [R2]; doubled to 2× [R3]; explicit — never leading-derived 0dp again |
| `AssistantText.paragraphGap` | **14dp** | intra-turn rhythm + markdown inter-block [R2] |
| `Turn.gapAbove / gapBelow` | **48dp / 48dp** | the turn band: 48 + hairline + 48 [R2 introduced, R3 doubled] |
| `ActionRow` | icon **20dp** in 48dp cells, `topMargin` **8dp** | ≈22dp ink response→footer [R2]; 48dp = a11y floor, never shrink |
| `ActivityGroup` | rowHeight 36dp, topGapAfterBubble 32dp [R3], detailMaxHeight 200dp | tool fold geometry [Ref] |
| `ToolCard` | paddingVertical **20dp**, headerIconSize **16dp**, headerIconGap **4dp**, headerToContentGap **12dp** | promoted-tool action card (§6); GMS reference capture 2026-07-12. Reuses `AssistantText.gutter` (24dp) + `Composer.internalPadding` (16dp) — only these four were uncovered |
| `Markdown.headingTopGap / BottomGap` | 36 / 16dp | headings cling to what follows (~2:1) [Ref measured] |
| `UserBubble` | pad 16/12, rightMargin 24, maxWidth 0.78 | [Rev F] |
| `DrawerRow` recents (rail) | rowHeight **44dp** (vs 56dp action rows), `dateGroupTopPad` 12dp, `runningDotSize` 8dp trailing | compact, date-grouped Recents [2026-07-03 directive] |
| `Composer.overlayHeight` | **84dp** | [R4] overlay-only pill height (vs docked 64dp) — full conversational surface, not a media strip |
| `Composer.overlayActionCircleSize` | **56dp** | [R4] overlay-only trailing circle/tile size (vs docked 44dp) — primary voice action dominates |
| `Composer.scopeRowStartInset` | **48dp** (= `horizontalMargin` 16 + `height/2` 32) | docked scope-row start anchor: `radiusPill` is `CircleShape` (a 50% stadium), so the corner curve becomes the straight edge exactly `height/2` in from the pill edge — align composer content there, not to `horizontalMargin` alone [2026-07-14 directive] |
| `Composer.scopeRowIconSize` | **16dp** | scope-row glyph, one step down from the 24dp `iconSize` to sit proportionate to `chipLabel`/`sectionHeader` text [2026-07-14] |
| `DrawerSheet.shadowElevation` | **16dp** | the drop shadow under the left drawer — `ModalDrawerSheet` casts NONE by default (m3 1.4.0 forwards only `drawerTonalElevation`, tinted toward `accentPrimary`), so it is applied via `Modifier.shadow(…, RectangleShape, clip = false)` at the call site [2026-07-14 side-rail polish] |

**Recents rail [2026-07-03 directive].** The left drawer's session history is a *navigation list*,
not a conversational turn stream — so §1.2's "never cramped" law (which governs turn separation in
the chat surface) does not apply here. History rows are deliberately DENSER than the New chat /
Search chats action rows (44dp vs 56dp), and every recents title is **flush at `screenGutter`**,
aligned with the "Recents" header and the date sub-dividers (Today / Previous 7 days / Older). The
prior row reused the icon-slot action-row layout, which reserved an empty 24dp leading box that
indented every title ~36dp past the header; running-session liveness is now a TRAILING
`accentPrimary` dot so that indent can never return. Scope is filtered — mobile-only by default,
"All" via the "Recents" header's overflow menu — the rail's analogue of the console's
`DEFAULT_VISIBLE_ORIGINS` (mobile ↔ task sessions don't bleed either way).

**Recents-row long-press actions [2026-07-04].** Long-pressing a recents row opens
`SessionActionsSheet` (Rename / Archive — the API has no session hard-delete, so no Delete row):
standard sheet anatomy (§4 — `surfaceInput`, `radiusBubble` top corners, drag handle, 56dp action
rows at `screenGutter`), header = the session title, archive glyph hand-ported (no
`material-icons-extended`). The long-press adds NO leading affordance to the row (the §7.13 indent
must never return); the one haptic is `combinedClickable`'s built-in. Rename is an in-sheet
drill-in pane (composer-options precedent): the field **auto-focuses with the full title
selected** so the IME raises immediately and the first keystroke overtypes (device-verified: no
auto-focus ⇒ the focus tap collapses the selection), commit trims + no-ops on blank/unchanged +
caps at the server's 120 chars, and the keyboard is hidden **eagerly on commit** plus on-dispose
for back/scrim exits (the dispose hook alone races the sheet-window teardown and strands the IME —
device-verified). Archiving the currently-open session lands on a fresh new chat via the same
callback the New chat row uses; any other row simply leaves the list (the repository mutates its
cached list in place, so the rail updates before the reconciling re-fetch).

**Turn separation law [R1+R2+R3]:** every turn boundary renders a `HorizontalDivider`
(`outlineHairline`) inside the `Turn` band — drawn by `ChatTranscript` above each user bubble
that has a predecessor. Separation between turns is **border + space**; between a response and
its footer it is space (+ the footer's own muted styling); the *next* turn's divider closes the
unit. Item top-gaps are asymmetric by chronological-predecessor type — never a uniform
`Arrangement.spacedBy`.

## 4. Color & shape

Surfaces: `surfaceCanvas #000000` (the app IS a solid black canvas at rest — see §5),
`surfaceInput #1E1F23` (bubbles, composer, chips), `surfaceSelected #26282C` (code blocks **and the
promoted-tool action card** §6 — always contrasts with its parent, a same-color-on-same-color block
is a bug we shipped once), `outlineHairline #2A2B2E` (ALL dividers). Text tiers: `textPrimary
#E9EAED` → `textSecondary #9AA0A6` → `textTertiary #5F6368` — a hierarchy step means stepping BOTH
size and tier where possible. Accents: `accentPrimary #4C6EF5`, `accentError #E46962` (failure
glyphs only).
Shapes: `radiusPill` (stadium), `radiusBubble` 28dp (user bubble **and the action card** — both are
full-width, bubble-scale surfaces), `radiusThumb` 16dp (code blocks), `radiusCard` 20dp (the assist
overlay's SMALL floating response card — deliberately tighter; do not reach for it just because a
thing is called a "card").

**Side-rail + scope tokens [2026-07-14].** `surfaceDrawer #0F1012` (the left drawer's whole-column fill,
roughly midway `surfaceCanvas`→`surfaceInput`) is its OWN token — NOT `surfaceSelected` (which is the
rail's selected-row pill fill; a whole-canvas use would erase that highlight, §7.8) nor `surfaceInput`
(bubbles/composer/chips). `scopeProject #B79CE8` (amethyst) / `scopeTool #5CC8D6` (cyan) tint the
composer scope-row + project/tool-picker GLYPHS only (never body text) — a cool violet↔cyan pair,
deliberately distinct from `accentPrimary`/`accentError` and clear of the rejected yellow/brown/green
(§4 aura color law).

**The action card must not wear `surfaceInput`** (§6, 2026-07-12): that token fills the composer,
every bottom sheet, the attachment tiles AND the chip family, so a card wearing it dissolves into the
chrome around it — the precise opposite of a surface whose only purpose is to be seen. It takes
`surfaceSelected`, one step lighter and uncontested at that scale. Picking a token by its NAME
("radiusCard for a card") rather than by the surface it was measured against is how both of this
card's first-round visual bugs happened; read the token's KDoc, not its name.

**Overlay scrim [R4]:** `AuraColors.scrimOverlayGradient` — a 3-stop vertical gradient
(alpha-over-black) replacing the flat 60%-black Rev E §E-2 fill, because a screen-aware assistant
must not dim the screen it claims to understand:

| Stop | Alpha | Fraction | Meaning |
|---|---|---|---|
| 1 | 8% | 0f | status/dismiss region — app legible |
| 2 | 10% | 0.60f | context preserved |
| 3 | 55% | 1f | contrast bed behind pill + resting glow |

Rendered by `ui/aurora/OverlayScrim` (`Brush.verticalGradient`), static/state-free — the invocation
fade is owned by the caller's own `AnimatedVisibility`. Superseded seams, all deleted:
`AuraColors.scrimOverlay` (flat 40%-black token), `AssistantExtras.scrimColor`, `OverlayScrim`'s own
`SCRIM_ALPHA` (60%) override.

**Overlay bloom colors [R4]:** the overlay's live/resting bottom glow renders `AuraColors.
auroraOverlayLiveBloom` — `#6D85B9` (bright stop) / `#4562A0` (fade stop), the raw reference-capture
scanline values — restored because chat's darkened `auroraOverlayBloom` pair (`#33436E`/`#131C36`,
2026-07-03 dark-blend directive) rendered mathematically present but perceptually invisible once
composited under the overlay's own scrim; chat keeps the darkened pair untouched. The one-shot
ignition phase lerps toward `AuraColors.auroraIgnitionBloom` — `#5C7BF0` (bright) / `#23336B` (deep),
`accentPrimary`-adjacent — before settling back to whichever pair the caller owns (CPU-side lerp,
`ui/aurora/CLAUDE.md` Rule 4).

**Overlay multi-hue aurora families [R5]:** the overlay's live glow is no longer blue-only — a
bounded, aperiodic hue-drift field (Rule 4b, `ui/aurora/CLAUDE.md`) travels the two-stop pair through
THREE hue families, each a light/deep pair. Violet sits BETWEEN blue and ember so the blend path
never crosses muddy gray. Chat never drifts (`hueDriftAmount` 0 → the shader branch-skips the field
entirely) and renders only family A.

| Family | Token | Light stop | Deep stop | Note |
|---|---|---|---|---|
| A (blue) | `auroraOverlayLiveBloom` | `#6D85B9` | `#4562A0` | the landed [R4] pair, reused verbatim |
| B (violet) | `auroraOverlayVioletBloom` | `#9A7BD8` | `#5B3E96` | amethyst over navy — the bridge hue |
| C (ember) | `auroraOverlayEmberBloom` | `#E8956B` | `#A34E2E` | warm ember/burnt-sienna — deliberately NOT yellow/gold/green ([R2] chat rejection informs taste) |

INTENSITY still selects light-vs-deep within the sampled family and still drives alpha (Rule 4's
coupling intact); only the FAMILY drifts. A perimeter bloom lerps all three families toward
`auroraIgnitionBloom` (the whole field unifies into brand ignition light, then drifts back apart as
it settles).

**Aura color law [R2]:** the in-chat aura is the **blue** bottom edge-glow family
(`auroraOverlayBloom` near-black navies). The gold/green `auroraWashTop` palette renders NOWHERE
in the product today (debug `LivenessShowcase` only — the overlay and landing both use the blue
edge glow) and must NEVER render in the chat surface — the user explicitly rejected it there
("shit yellow/brown/green").

## 5. State & liveness language

| App state | Visual | Contract |
|---|---|---|
| **Resting** (idle, no run, invocation window elapsed) | **Solid background. NO aura. Transparent top bar.** | [R2] "the native resting state of the app is just a solid color background" |
| **Fresh invocation** (app/chat screen just opened) | bottom blue glow, ambient, **30s** window (`AMBIENT_INVOCATION_WINDOW_MS`, ≥3× the prior ~10s per a 2026-07-14 directive; §7.21), then fades out smoothly | [R2] |
| **Run live** (Sending/Streaming) | bottom blue glow (Thinking), slightly faster than ambient — speed changes phase-continuous, never a jump | [R2]; speed boost constant lives in `ChatScreen` |
| **Run live** (transcript) | `AuraSpark` row visible at the transcript bottom for the ENTIRE run — follow-ups and mid-turn tool phases included | [R1] |
| **Run complete** (run just settled, chat still open) | bottom glow blooms from Thinking's contracted hug into the wider `Listening` ambient breathe, **30s** window **re-armed on every completion** (`completionArmToken`), then fades to `Hidden` — the SAME bounded linger a fresh invocation gives, deliberately NOT empty-gated (it fires precisely because a response landed into a non-empty transcript) | [R2] window · re-arm |

Laws: the bottom/edge glow is **`ChatScreen`'s** layer (user-tuned `IN_APP_GLOW_*` constants —
do not retune against SwiftShader); `ChatSurface` renders **no aura of its own**. Aura alpha
must decay to **exactly 0 before draw bounds** (smoothstep edge window + dither) — clipped
falloff reads as a visible line (§7.9). The **top bar stays transparent** in chat; nothing may
paint an opaque or washed layer behind it (§7.3). When a run **ends**, the glow does NOT snap to
black: it first **blooms from `Thinking`'s contracted hug into the wider `Listening` ambient pool**
and lingers there for the re-armed 30s completion window (the "Run complete" row above). The
**≥3s photosensitivity ease-off** (linear, `AuroraEdgeGlow(dismissFadeMs = AuraMotion.edgeRestFadeMs)`)
then applies at the END of that linger — not at the instant the run settles. That ease-off HOLDS the
last active frame (reach, intensity, wave phase+speed) and ramps only its alpha to 0; handing the
shader `Hidden`'s own uniforms cannot fade (its intensity 0f zeroes alpha, its wide reach pops the
bloom) — see `ui/aurora/CLAUDE.md`. An abrupt on→off flash is a photosensitivity trigger [R3]; registry
§7.15 stays valid — the ease-off itself is unchanged, only WHEN it fires moved to the linger's end.
Caller-knob so the overlay keeps its own quick dismiss (§7.15).

**Overlay invocation — the perimeter bloom ([R4 2026-07-10]).** The assist overlay
(distinct surface from chat — an on-screen overlay is always live, so it is NOT bound by the
solid-resting law above) opens with a one-shot PERIMETER bloom that rides `AuroraEdgeGlow`'s
entrance and decays into the bottom-only live glow. Keyframes, from the moment the overlay leaves
Idle:

| Phase | t (from invocation) | Bloom envelope | Perimeter floor [R5] | What renders |
|---|---|---|---|---|
| **Invocation bloom** | 0ms (leaves Idle) | snaps to **1** | 0.35 (Igniting) | full perimeter — side rails + lit bottom corners, ~25% top, dark center; alpha rides `AuroraEdgeGlow`'s own visible ramp |
| **Ignite** | 0–450ms (`edgeSweepMs`) | held at 1 | 0.35 (Igniting) | perimeter fills from the edges inward as `decayLength` grows; inner boundary undulates with the existing wave |
| **Settle** | 450→1400ms (`bloomSettleMs` 950, FastOutSlowIn) | **1→0** | 0.35 (Igniting → Listening) | top/side edges fade first; the bottom term persists into the live state |
| **Live** | run-gated | 0 | 0.35 (Listening) · 0.15 (Thinking) | bottom glow plus a persistent, faint edge-lit perimeter (`max(bloom, floor)`) — `Listening` ambient / `Thinking` contracted and corner-suppressed |
| **Resting** | after `Streaming.done`, overlay still on screen | 0 | 0.30 | low, **static** bottom pool plus the same persistent edge floor (no breathe), "assistant present"; overlay-scoped, never chat |
| **Dismiss** | on Idle | 0 (reset) | 0 (Hidden) | glow extinguishes WITH the pill over the shared `OVERLAY_GLOW_DISMISS_MS` window (≈169ms) — `AuroraEdgeGlow(dismissFadeMs = OVERLAY_GLOW_DISMISS_MS)`, the SAME constant the pill's own exit tween reads so the two can't drift; still the overlay's own quick dismiss, NOT the ≥3s chat ease-off |

Laws: the bloom is three EDGE-ANCHORED exponentials (side + bottom-biased + faint top) — no radial
mask (§7 Rule 1), no 3rd color stop (the ignition pair lerp is CPU-side, Rule 4). Edge-anchored
terms PEAK at the draw bounds by construction, so the top-fade window scopes to the bottom term
ONLY — the perimeter's bright top/side edges are a light source, not a clipped falloff (§7.9 is
about clipped mid-falloffs). The envelope is caller-owned (`AuroraEdgeGlow(perimeterBloom =)`),
forced to 0 under reduced motion (guarded caller-side AND in the shader). Chat never blooms
(`ChatScreen` passes no envelope → byte-identical). Full mechanism: `ui/aurora/CLAUDE.md` →
"Perimeter bloom (R4)".

**Persistent edge-lit perimeter floor [R5]** (device feedback on 0.0.30-debug: the overlay glow
"isn't strong towards the edges"). Per-state floor values are in the Phase table's floor column
above, applied via `perimeter × max(bloom, floor)` (`EdgeGlowUniformMath.perimeterFloor`) so a live
bloom still wins but the perimeter no longer collapses to bottom-only once it settles; gated by the
overlay-only `perimeterPresence` knob (overlay 1, chat 0 → bottom-only byte-path). NOT
reduced-motion-gated: the floor is a STATIC edge presence (only the drift speeds are 0 there), so
reduced motion keeps a static edge-lit multi-hue frame. The edges are also tuned stronger overall,
independent of state (perimeter bottom-bias floor 0.25→0.45, top term 0.35→0.5).

**Fluid aurora motion [R5].** The reach wave is now 2-octave (a coarse swell + a finer, faster
octave that breaks its silhouette so the boundary reads as liquid, not one sine), and the side
reach gains a per-ROW "liquid level" wave so the edge light visibly rises and falls along the
screen edges. Both are `iTime × rate` terms — phase-continuous (§7.12), 0-speed under reduced
motion. Full mechanism: `ui/aurora/CLAUDE.md` → "Rule 4b" + "Perimeter bloom".

**Scope note.** §5's solid-resting law above and §7.2 govern the CHAT surface at rest. The assist
overlay ON SCREEN is a live state by that same language — its [R4] session-resting pool (the
`Resting` row above) is not a regression of §7.2 and must not be "fixed" back to hide-on-done;
record a new directive instead.

## 6. Component laws (chat surface)

- **Turn shape (reducer-enforced, contract-tested):** within a turn, activity items (tool
  groups, agent chips, todo list) precede the assistant text; the text is the turn's last item
  (ErrorCards excepted). One tool group per turn; only a user message closes it. Real sessions
  interleave tool→narration→tool (9/122 measured) — arrival-order rendering is provably wrong.
- **Tool fold (reference-style):** cardless plain row; default-COLLAPSED even while the run is live
  (tap always overrides); header always carries the count ("Using N tools…" pulsing / "Used N
  tools" settled); hairline under the header and between rows when expanded; per-call summary
  only when expanded; input AND result share one monospace `ToolCallCodeBlock` on
  `surfaceSelected`. Never a filled card, never a sheet, never auto-expanded.
- **Action card (promoted tools) — the ONE sanctioned exception to "never a filled card", and NOT a
  loosening of the fold law above.** *User directive 2026-07-12:* the fold exists for the ordinary,
  typically-uninteresting tool call; a tool we deliberately pick **because its result is something
  the user must SEE** (an alarm being set) gets its own dedicated component. Emphasis is a curated
  editorial decision we make — never a property a tool can claim for itself. Mechanically: a short
  allowlist (`data/model/PromotedTools`) leaves the fold entirely and renders as its own filled
  transcript card (`ui/chat/toolcards/ToolActionCard`); everything else keeps the cardless fold,
  unchanged — which is why this is an exception, not a repeal. Read the two laws as one sentence:
  **the generic many-tools fold is never a card; a hand-picked single tool is nothing but one.**
  **The card does NOT suppress the turn's prose reply** — the wire sends exactly one `assistant`
  event per turn and the reducer never drops it, so a promoted turn renders `[card] → [prose]`, card
  above narration (§6 turn shape). The reference app *does* replace its prose with the card; we
  diverge **by decision, not omission** (*user directive 2026-07-12*, after the divergence was put to
  them explicitly): a turn's narration can carry more than the tool's own result — a model that sets
  an alarm AND answers something else would lose the something else — and it is not ours to delete.
  The mild restatement on tool-only turns is the accepted cost. Do NOT "fix" this toward reference
  parity, and do not write card copy that assumes the card is the whole answer.
  Anatomy is Google's own, lifted from a GMS-device `uiautomator` capture (2026-07-12) where the
  resource-ids spell the architecture out — `action_card_entity` = an **entity header** (glyph +
  quiet label: *which tool is this*) over **swappable content blocks** (the payload). We cloned that
  ANATOMY and nothing else: every value is an Aura token (§2/§3/§4), never Google's pixels/hexes.
  Laws: informational only (no chevron, no expand, no click — the fold remains where you dig for raw
  JSON); the header label never competes with the payload; **promotion requires `success`** — a
  failed allowlisted call stays in the fold, because a confident singled-out card for a tool that
  failed is the UI lying; and an allowlisted id with no bespoke renderer must still render (generic
  fallback), never a blank row. Adding the next card = one id in `PromotedTools` + one `when` branch
  in `ToolCardRegistry` + one composable over `ToolActionCard`. Nothing else moves.
- **Assistant text:** bare full-width; streaming AND finalized render markdown
  (`MarkdownBuffer.sanitize` guards half-open fences). There is no plain-text streaming mode.
  Streaming must not flicker: the renderer parses through a `retainState = true` markdown state — the
  previous parsed tree stays visible until the new parse lands, so a delta never blanks the reply to
  a `State.Loading` frame [R16] — and `rememberStreamedText` throttles the reparse to
  `AuraMotion.markdownStreamReparseMs` (~20 Hz) so the growing tree isn't relaid-out every token.
  Smoothness is achieved by removing churn, never by a per-word reveal animation (`WordFadeText` is
  retired).
- **Footer (`ActionRow`):** under EVERY completed (settled, non-streaming) assistant turn — one
  footer per response, not just the last [R3]; 20dp glyphs in 48dp cells; detached from content by
  ~22dp ink.
- **Disclaimer:** "Mewbo is an AI tool and can make mistakes." renders EXACTLY ONCE, as its own bottom-anchored
  transcript item, below the final turn, only after a reply has settled. Never per-turn, never
  mid-transcript, never above a divider.
- **Error cards:** only on genuine run failure (`completion.error`). A successful run carrying
  `last_error` residue from a failed tool call renders NOTHING.

## 7. Regression registry — never again

Each entry: what shipped wrong → the law that prevents it. A change reintroducing any of these
is a blocking review failure.

1. **Tool groups below the response/footer** → reducer turn invariant (§6), contract-tested in
   `TranscriptReducerTest`.
2. **Always-on aurora wash at rest** (2026-07-04, reverted same day) → §5: resting = solid
   background; aura strictly invocation/run-gated (chat surface — the overlay's [R4] session pool
   is out of this entry's scope).
3. **Lost transparent top bar** (side effect of the wash layer) → §5 law; `ChatSurface` renders
   no backdrop layer behind the top bar.
4. **Gold/green wash in chat** → §4 aura color law: blue bottom glow only in chat.
5. **Auto-expanded fat tool cards** → §6 tool fold: default-collapsed, cardless, forever. **Scope
   note (2026-07-12):** this entry governs the GENERIC many-tools fold only. The promoted-tool
   action card (§6) is a filled card BY DESIGN and is not a re-run of this regression — do not
   "fix" it back into the fold. The distinction that keeps both laws true: the fold renders whatever
   arrives, so it must stay cheap and quiet; the action card renders only what we hand-picked, so it
   is allowed to be loud. If you find yourself wanting a card for a tool nobody allowlisted, the
   answer is the fold, not a wider card.
6. **Error banner under successful chats** → §6 error-card law + reducer test.
7. **Disclaimer mid-transcript** (first-turn anchoring — twice: original per-message form, then
   first-turn form) → §6: bottom-anchored single item at the transcript end.
8. **Same-color-on-same-color result block** → §4: code blocks always `surfaceSelected` against
   a differing parent.
9. **Visible line/edge artifact in glow/orb falloff** → §5 edge-window law (alpha 0 before
   bounds; keep dither). First hit the orb, then the edge glow.
10. **Leading-derived “0dp” gaps** → §3: separation values are explicit tokens; intrinsic font
    leading is never load-bearing.
11. **Un-centered pulse dots** (TypingIndicator inner Row defaulted to Top) → shared row
    composables must set `verticalAlignment` explicitly.
12. **Speed changes that jump** → §5: scale rates, not accumulated phase (aurora time traps,
    `ui/aurora/CLAUDE.md`).
13. **Recents titles indented under an empty icon slot** (session rows reused the action-row
    `DrawerRow`, reserving a 24dp leading box that pushed every title ~36dp past the "Recents"
    header) → §3 Recents-rail law: history rows are a dedicated compact composable, titles flush at
    `screenGutter`, liveness dot TRAILING; scope filtered mobile-only by default.
14. **Footer under the last turn only** (the footer must render under EVERY completed response —
    user directive [R3]; the prior "last completed turn only" form is superseded) → §6 footer law.
15. **Glow snaps off on run-end** (abrupt on→off flash — a photosensitivity trigger; `Hidden`'s own
    intensity-0f / wide-reach uniforms made the pre-existing `visible` fade effectively instant) →
    §5 run-end ease-off: hold the last active frame and ramp alpha to 0 over
    `AuraMotion.edgeRestFadeMs` (≥3s, linear); caller-knob so the overlay keeps its quick dismiss.
16. **Streaming markdown blank-flashes / thrashes per token** (mikepenz's default markdown state
    resets to `State.Loading` on every content change — blanking the whole reply for a frame before
    the async reparse lands — and reparsing the full growing tree on every SSE delta relays-out the
    whole message 30–80×/sec) → §6 assistant-text law: parse through a `retainState = true` markdown
    state so the last tree stays visible during reparse, and throttle the reparse to ~20 Hz via
    `rememberStreamedText`. Never feed the raw per-token buffer straight into a fresh parse.

17. **A dither that is present, reachable, and still does nothing** (2026-07-14, physical Pixel;
    shipped and survived four aurora rounds). `AuroraEdgeGlow`/`AuroraWashTop` added
    their dither to the UNPREMULTIPLIED colour and only then multiplied by alpha. Skia surfaces are
    premultiplied — the framebuffer receives `rgb * alpha` — so the dither's real amplitude was
    `alpha` LSB, not 1 LSB: full strength at the peak (where the ramp is steep and banding is
    invisible) and ~0 in the faint reaches (where the ramp is flattest and the contour bands are
    widest). Exactly backwards. On chat it was correctly scaled in only the bottom ~8% of the
    screen. → **§7.17 law: dither the PREMULTIPLIED value, clamped to `[0, alpha]`, as the last
    operation before the return** — via the ONE shared `GlslNoise.ditherPremul`, never a per-shader
    hand-roll. "Reachable from the return" is NOT "effective"; `ui/aurora/CLAUDE.md` Rule 3's old
    (a)+(b) checklist PASSED on the broken shader, which is why it needed clause (c).
    Two riders, same entry: the dither hash must be fed SMALL arguments (`hash21(fragCoord)`
    collapsed to ~8 distinct values at the bottom of a 1080×2400 display — float32 ULP at 1.09e6 is
    0.125 — i.e. degenerate exactly where the aurora is brightest; use `ign()`), and the amplitude
    is **±0.5 LSB**, matching Skia's own `make_dither_effect`, not ±1 LSB (the 2× rule is for WHITE
    noise; IGN is ordered).
    **Accepted consequence, not a bug:** the chat ramp spans only ~50–84 codes, so a CORRECT dither
    necessarily converts contours into fine grain. Do not "fix" the grain by weakening the dither.
18. **Absolute uptime narrowed to Float in the shared frame clock** (2026-07-14; the same bug
    class an earlier single-call-site fix missed). `ShaderFrameClock` fed `withInfiniteAnimationFrameMillis`'s value — milliseconds
    since BOOT — straight into a Float. At 5 days' uptime that is ~4.3e8 ms, where the float32 ULP
    is 32 ms, so shader phase advanced only every OTHER frame (4-frame stalls at ~11 days): the
    aurora juddered instead of drifting. It degrades WITH UPTIME, which is why redroid (minutes of
    uptime) can never reproduce it and a real phone always will. → Anchor to the first observed
    frame **in `Long`, where the subtraction is exact**, and narrow only the small difference.
    General law: never let absolute wall/boot time reach a float32 animation input.
19. **A verification harness that structurally cannot see the defect class it is gating.** Entries
    17 and 18 both shipped through four aurora rounds of pixel-diff + screenshot verification on
    redroid. A pixel-diff cannot see banding (it compares frames, not ramp smoothness), and a
    minutes-old container cannot exhibit an uptime-dependent clock bug. → When a fix's whole purpose
    is perceptual or hardware-dependent, the acceptance gate is the physical Pixel, and the
    emulator check must be a NUMERIC one that would actually fail: flat-run-length along a dithered
    ramp (a correctly dithered ramp has NO flat runs), and distinct-value count of the dither at
    high `fragCoord.y`.

20. **Disclaimer renders during a live turn** (2026-07-14 — supersedes the same-day
    "disclaimer joint collapses to 0dp during streaming" form). The bottom-anchored disclaimer was
    gated on `hasSettledReply` ALONE — correct on turn 1, but on every FOLLOW-UP turn a prior reply is
    already settled, so during Sending/Streaming the disclaimer stayed pinned at the transcript bottom
    under the fresh user bubble + spark, AHEAD of the new turn's response and its footer. It must NOT
    render during a live turn at all. → **Gate it on `hasSettledReply && !isRunLive`**
    (`shouldShowDisclaimer`, the transcript-level twin of the per-row `showActionRow`), so its
    appearance coincides EXACTLY with the settled `ActionRow` footer's and it is MUTUALLY EXCLUSIVE
    with the live spark (spark ⇔ live, disclaimer ⇔ settled). The settled footer is therefore the
    disclaimer's SOLE predecessor: `AuraSpacing.ActionRow.disclaimerGap` stays `0.dp` (the footer's
    48dp touch-cell inset supplies its air), and the spark — no longer the disclaimer's neighbour
    during a live turn — carries ONLY its own `top = ActionRow.topMargin` gap (the `LazyColumn`'s
    bottom contentPadding buffers it off the composer). The old `bottom = Composer.gapTight` spark
    inset was this-entry-superseded compensation to hold the disclaimer off the spark; it is gone. Any
    future predicate above the disclaimer must preserve the "settled turn only" gate (`ui/chat/CLAUDE.md`).
21. **Do NOT retune the fresh-invocation ambient linger back down.** `ChatScreen.AMBIENT_INVOCATION_WINDOW_MS`
    is **30s** as of a user directive 2026-07-14 (≥3× the prior ~10s) — a longer, calmer "the app just
    woke up" breathe (§5). This is the §1 precedence ladder in action (a newer directive over the older
    value); record a new directive rather than "correcting" it back toward the old 10s. A directive-value
    guard, not a shipped-wrong bug — filed here so the value survives a future tuning pass.
22. **The assist overlay must hold the screen awake while it is showing** (2026-07-14). A voice turn can
    run long with no touch input to reset the OS dim timer, so without a flag the screen dims
    mid-response. `FLAG_KEEP_SCREEN_ON` is set on the session's real `Window` (the `window?.window`
    Dialog-indirection reference, `voice/CLAUDE.md`) in `AuraSession.onShow()` and cleared on BOTH
    teardown paths (`onHide` AND `onDestroy` — a kill/unbind without a preceding hide would otherwise
    strand it on), mirroring `AssistOverlayPresence.visible`'s set/clear. The debug preview host carries
    a parity one-liner. Overlay-scoped — the chat surface at rest is untouched (§5 solid-resting law).
23. **Transcript items pop / hard-cut on toggle instead of reflowing** (2026-07-14). The
    transcript `LazyColumn` had NO item animation — the spark and disclaimer hard-cut on their
    run-phase toggle, tool rows had an entrance (`ChipEntranceAnimator`) but zero placement/removal
    transition, so a live turn's mount/shuffle/unmount read as flicker. → **Every transcript row
    animates through the ONE shared `transcriptItemTransition` (`Modifier.animateItem`)** — a placement
    spring (`AuraMotion.transcriptItemPlacementSpring`) plus fades — never a bespoke per-item
    animation. Reduced-motion-gated: rows SNAP (placement travel dropped), the opacity fades stay at
    the flat `AuraMotion.reducedBlockFadeMs` (a fade is opacity, not travel — M8 spirit). Chip
    ENTRANCE stays owned by `ChipEntranceAnimator` and the streaming assistant text is never
    per-delta-faded — content rows pass `fadeEdges = false` (placement + removal fade only); only the
    spark and disclaimer, which have no entrance animator of their own, pass `fadeEdges = true` to fade
    on both mount and unmount.

## 8. Enforcement map

- Ordering/turn shape → `data/model/TranscriptReducer.kt` + `TranscriptReducerTest` (contract
  suite; keep green on any event/serialization change).
- Token discipline → review law (no literals outside `ui/theme/`); tokens carry provenance KDoc.
- Aura contract → `ChatScreen` (sole glow owner) + `ui/aurora/CLAUDE.md` shader laws.
- This file → linked from the app-root `CLAUDE.md` tree table; scoped CLAUDE.mds cite it instead
  of restating values, so there is exactly one place values live.

## 9. Iconography

Icon sourcing has ONE order of preference (user directive 2026-07-14: never hand-roll an icon when an
off-the-shelf one covers the case):

1. **`material-icons-extended` first for any NEW glyph** (added to the dependency catalog 2026-07-14,
   BOM-managed 1.7.8; R8 strips the unused glyphs in release). A semantically-correct extended glyph
   beats a hand-rolled path.
2. **`ui/chat/ChatIcons.kt`'s hand-rolled set is FROZEN LEGACY.** Existing reuses stay — a reused
   hand-rolled glyph is not a "new hand-roll" (`ChatIcons.Clock`/`Fullscreen`, the drawer's `ArchiveGlyph`
   are legitimate legacy reuses) — but do NOT add a new hand-rolled path. Precedent: the recents-header
   filter's hand-ported `filter_alt` was DELETED for `Icons.Default.FilterAlt` the moment extended landed.
3. **Brand/provider LOGOS have no Material analog, so they ARE hand-converted.** `ModelProviderIcons`'
   `res/drawable/ic_provider_*.xml` are converted from MIT-licensed `@lobehub/icons` **monochrome** SVGs
   (single `currentColor` path → tintable; never the colored/gradient variants, §1.5 token discipline —
   a themed tint, not a baked brand hex). Each drawable carries its upstream source + MIT license as an
   XML provenance comment.

Two standing rules:

- **One icon weight per surface.** The drawer and settings are all `Icons.Filled.*`, so a new drawer
  glyph is Filled — never mix an Outlined glyph into a Filled surface.
- **The delete-affordance trap: Temporary = `Schedule` (a clock), NOT `AutoDelete`/trash.** The ephemeral
  Temporary project reads "time-bound / transient" via a clock (paired with a divider below it); a
  trash/auto-delete glyph next to a selectable, tappable row misreads as "tap to delete."
