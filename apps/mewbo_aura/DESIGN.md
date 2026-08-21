# Mewbo Aura — Design System (canonical)

> ↑ [app root CLAUDE.md](CLAUDE.md) · consumers: every file under `app/src/main/java/com/mewbo/aura/ui/`
>
> This file is the **single canonical statement of the design system**: the laws, the token
> values, their provenance, and the regression registry. The scoped CLAUDE.md files carry the
> per-package mechanics; when they and this file disagree, THIS file wins and the CLAUDE.md is
> stale — fix it. Every visual change to the app must be checked against §7 (regressions that
> must never recur) before it ships.

Provenance shorthand used below, in directive order (each supersedes the one before it): **[R1]**
= user directive, round 1 (compact design language), **[R2]** = round 2 (breathability + aura
correction), **[R3]** = round 3 (footer under every completed response; 2× breathability —
doubled turn band + doubled bubble→response gap), **[R4]** = user directive (overlay presence
redesign — pill mass: overlay-only height/action-circle/type-scale tokens; context-preserving
scrim gradient; 3-phase invocation bloom + session resting glow; a11y channel),
**[R5]** = user directive (device feedback on 0.0.30-debug: the overlay
aurora must be a pretty MULTI-HUE field [blue + violet + ember], FLUID toward the edges [2-octave
wave + per-row liquid level], and STRONGER at the edges [persistent edge-lit perimeter floor]; plus
a fluid synthesizer-style RMS voice bar),
**[R6]** = user directive (device-control surface: the aura must glow round the ENTIRE PERIMETER as
a border rather than washing the bottom, at a REDUCED radius so it reads as a border and not a haze,
visibly FLOWING rather than "just slightly breathing" — slow, steady, and explicitly never fast
enough to be a photosensitivity risk — with the multiple hues playing more visibly),
**[Ref]** = measured reference-capture
values, **[Rev F]** = instrumented uiautomator audit. Precedence: **the
highest-numbered user directive wins over every measured reference value**. Do not "fix" a directive value
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
| `toolCardDisplay` | **36 / 44** | Normal | action-card payload line (§6); GMS reference capture |
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
| `ToolCard` | paddingVertical **20dp**, headerIconSize **16dp**, headerIconGap **4dp**, headerToContentGap **12dp** | promoted-tool action card (§6); GMS reference capture. Reuses `AssistantText.gutter` (24dp) + `Composer.internalPadding` (16dp) — only these four were uncovered |
| `Markdown.headingTopGap / BottomGap` | 36 / 16dp | headings cling to what follows (~2:1) [Ref measured] |
| `Focus.ringWidth / ringCornerRadius` | **2dp / 12dp** | the D-pad focus ring (§4). One geometry for every surface — a ring that varies stops reading as "you are here" and starts reading as decoration |
| `UserBubble` | pad 16/12, rightMargin 24, maxWidth 0.78 | [Rev F] |
| `DrawerRow` recents (rail) | rowHeight **44dp** (vs 56dp action rows), `dateGroupTopPad` 12dp, `runningDotSize` 8dp trailing | compact, date-grouped Recents [user directive] |
| `Composer.overlayHeight` | **84dp** | [R4] overlay-only pill height (vs docked 64dp) — full conversational surface, not a media strip |
| `Composer.overlayActionCircleSize` | **56dp** | [R4] overlay-only trailing circle/tile size (vs docked 44dp) — primary voice action dominates |
| `Composer.scopeRowStartInset` | **48dp** (= `horizontalMargin` 16 + `height/2` 32) | docked scope-row start anchor: `radiusPill` is `CircleShape` (a 50% stadium), so the corner curve becomes the straight edge exactly `height/2` in from the pill edge — align composer content there, not to `horizontalMargin` alone [user directive] |
| `Composer.scopeRowIconSize` | **16dp** | scope-row glyph, one step down from the 24dp `iconSize` to sit proportionate to `chipLabel`/`sectionHeader` text |
| `DrawerSheet.shadowElevation` | **16dp** | the drop shadow under the left drawer — `ModalDrawerSheet` casts NONE by default (m3 1.4.0 forwards only `drawerTonalElevation`, tinted toward `accentPrimary`), so it is applied via `Modifier.shadow(…, RectangleShape, clip = false)` at the call site [side-rail polish] |
| `NavigationRail.width` | **240dp** | the television shell's permanent left rail (`ui/navigation/AuraNavHost`'s `TelevisionChatHome`) — a FIXED width, unlike the handheld drawer's 0.78 screen fraction: that fraction exists because a sheet laid over content should not fully cover it, and a rail covers nothing, so what it must do instead is leave the transcript enough room to stay the subject. At the 960dp width a 16:9 television reports, this keeps roughly three quarters of the screen for the conversation while still fitting a session title without truncating it to a stub |

**Recents rail [user directive].** The left drawer's session history is a *navigation list*,
not a conversational turn stream — so §1.2's "never cramped" law (which governs turn separation in
the chat surface) does not apply here. History rows are deliberately DENSER than the New chat /
Search chats action rows (44dp vs 56dp), and every recents title is **flush at `screenGutter`**,
aligned with the "Recents" header and the date sub-dividers (Today / Previous 7 days / Older). The
prior row reused the icon-slot action-row layout, which reserved an empty 24dp leading box that
indented every title ~36dp past the header; running-session liveness is now a TRAILING
`accentPrimary` dot so that indent can never return. Scope is filtered — mobile-only by default,
"All" via the "Recents" header's overflow menu — the rail's analogue of the console's
`DEFAULT_VISIBLE_ORIGINS` (mobile ↔ task sessions don't bleed either way).

**Recents-row long-press actions.** Long-pressing a recents row opens
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
**`focusRing #E9EAED`** — where a D-pad currently is, drawn at `AuraSpacing.Focus.ringWidth` **2dp**
(a divider hairline vanishes at couch distance). Deliberately NOT `accentPrimary`, which already
means *selected*: on a television the focused row and the selected row are routinely different rows,
and one colour for both makes the remote's position unreadable exactly when it matters. Near-white
also survives every surface token in this palette, which a tinted ring does not — the focusable set
spans `surfaceCanvas`, `surfaceDrawer` and `surfaceInput`. It is **not** a touch state and is never
gated on being a television: a finger cannot grant focus, so a handheld shows it only with a keyboard
or remote attached.

Shapes: `radiusPill` (stadium), `radiusBubble` 28dp (user bubble **and the action card** — both are
full-width, bubble-scale surfaces), `radiusThumb` 16dp (code blocks), `radiusCard` 20dp (the assist
overlay's SMALL floating response card — deliberately tighter; do not reach for it just because a
thing is called a "card").

**Side-rail + scope tokens.** `surfaceDrawer #0F1012` (the left drawer's whole-column fill,
roughly midway `surfaceCanvas`→`surfaceInput`) is its OWN token — NOT `surfaceSelected` (which is the
rail's selected-row pill fill; a whole-canvas use would erase that highlight, §7.8) nor `surfaceInput`
(bubbles/composer/chips). `scopeProject #B79CE8` (amethyst) / `scopeTool #5CC8D6` (cyan) tint the
composer scope-row + project/tool-picker GLYPHS only (never body text) — a cool violet↔cyan pair,
deliberately distinct from `accentPrimary`/`accentError` and clear of the rejected yellow/brown/green
(§4 aura color law).

**The action card must not wear `surfaceInput`** (§6): that token fills the composer,
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
dark-blend directive) rendered mathematically present but perceptually invisible once
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
| **Fresh invocation** (app/chat screen just opened) | bottom blue glow, ambient, **30s** window (`AMBIENT_INVOCATION_WINDOW_MS`, ≥3× the prior ~10s per user directive; §7.21), then fades out smoothly | [R2] |
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

**Overlay invocation — the perimeter bloom ([R4]).** The assist overlay
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

**Device-control surface — one 2–3s envelope in and out.** The overlay raised for the whole
device-control grant (`ui/control/`, a third surface again: it lives over OTHER apps, so neither the
chat solid-resting law nor the assist overlay's dismiss applies) arrives and leaves over ONE shared
window, `AuraMotion.deviceControlEaseMs` (2400ms), read by the glow, the narration stack and the
Stop pill's entrance so none of them can drift apart. User directive: "let it take about two to
three seconds to appear smoothly", both directions — it superseded a 180ms exit for the whole
surface, which read as a cut.

| Phase | Window | What rides it |
|---|---|---|
| **Arrival** | 2400ms linear | glow, narration stack and Stop pill together — one `graphicsLayer` alpha per window, read in the LAYER phase |
| **Departure — affordance** | `AuraMotion.scrimFadeMs` (180ms), then the window is REMOVED | the Stop pill only |
| **Departure — decoration** | 2400ms linear | glow + narration stack, easing off IN PLACE |
| **Teardown** | `maxOf` of the two exits + one frame | derived, never a second literal — a teardown short of a fade in flight rips the window out mid-animation |

Laws: the ramp is LINEAR at both ends (a fast final segment is the same photosensitivity trigger as
the cut it replaced [R3]). The glow is never handed `EdgeGlowState.Hidden` on the way out — its own
dismiss ramp underneath the envelope would multiply into that fast segment; the envelope alone
fades it. Reduced motion FLATTENS the envelope to `reducedBlockFadeMs`, never removes it (a fade is
opacity, not travel). **Only the pill is asymmetric, and that asymmetry is what makes the long exit
legible**: the pill is the one element asserting the phone can still be taken over, so it leaves at
once and what remains is an afterglow rather than a claim — removing its window, not fading it, is
what ends its touch region. The `ScreenCaptureVeil`'s own 96ms per-action fade is a DIFFERENT
mechanism and must never follow this window: it runs on every injected tap. Full mechanism:
`ui/control/CLAUDE.md` → "Arrival and departure are ONE envelope".

**Device-control surface — a PERIMETER BORDER, not a bottom wash [R6].** The same surface renders
`AuroraEdgeGlow` in `Listening` for the whole grant, and at the bottom-anchored balance every other
caller uses it read as "only bottom lit". The cause is arithmetic, not tuning: `glow` is
`clamp(vGlow + perimeter, 0, 1)`, a SUM, so at Listening's wide reach the bottom-anchored term
saturates the lower third **before the perimeter contributes anything there** — the side rails are
the leftover, not the subject. Measured on the terms (1080×2400 @2.75, noise held at 0):
bottom-centre 1.000 vs rails 0.23 vs top 0.20.

`AuroraEdgeGlow(perimeterBias = 1f)` is the caller knob that shifts it: bottom-centre 0.694, rails
0.679, top 0.679, screen-centre 0.000 — an even border with a saturated corner join and no haze.
Six effects ride one knob because each alone re-opens the imbalance (raising the floor without
damping the bottom only saturates harder; either without contracting the reach leaves a haze with
brighter edges). Reach contracts to 0.28 of the state's own — the "reduced radius" half of the
directive, which tightens the rail thickness with the same number since `sideDecay` derives from
`decayLength`. The hue field's SPATIAL frequency rises ×2.2 so the three families play ALONG each
rail rather than tinting a whole frame one family at a time; it is still bounded aperiodic noise,
never an angle (Rule 4b). **Chat and the assist overlay keep the default 0, where every one of the
six is an exact identity** (`mix(x, y, 0)` is `x`; `* 1.0` is exact) — byte-identity by
construction, not by tuning.

**Flow rate is DERIVED, and a retune re-derives [R6].** The surface passes
`speedScale = AuraMotion.deviceControlFlowScale` (2.3), because at the ambient pace it read as "just
slightly breathing". The fastest drift term in the shader is the reach wave's fine octave at
`WAVE_DRIFT_HZ × 1.9` ≈ 0.067 value-changes/s at a fixed pixel; the fastest periodic term this
family already ships and the design language already calls calm is the `listeningBreathePeriodMs`
breathe at ≈ 0.154 Hz. 2.3 is their ratio, so the fastest drift term lands exactly ON the breathe
cadence and **nothing on the surface runs faster than a rate already accepted** — ~20× under the
3 Hz flash threshold, modulating a smooth gradient's geometry (±22% of the decay length) and never
full-area luminance. Reduced motion keeps the FIELD and drops only the TRAVEL: an even, multi-hue,
frozen border, never a fallback to the bottom wash. Full mechanism: `ui/aurora/CLAUDE.md` → "The
border profile" + "Flow rate".

**The navigation-bar strip cannot be made to match, and that is a platform limit rather than a gap
[R6].** The directive asked for the Pixel's bottom navigation strip to carry the aura colour so the
glow and the system bar read as one surface, gated on "only if possible". It is not possible for the
case that motivated it — an agent driving a DIFFERENT app — and the evidence is two independent
blocks in AOSP, either sufficient alone: the nav bar composites at window layer 24 against
`TYPE_APPLICATION_OVERLAY`'s 11, and `DisplayPolicy`'s nav-bar-appearance candidate admits only an
app window or `TYPE_VOICE_INTERACTION`, which categorically excludes this window type.
`FLAG_LAYOUT_NO_LIMITS` does not help: it governs extent, not z-order, which is exactly why the
overlay reaches into that region and still renders beneath it.

What IS ours is already correct and must stay: `MainActivity` runs `enableEdgeToEdge` with both bars
transparent and `isNavigationBarContrastEnforced = false`, and `AuraSession` matches. Under gesture
navigation the bar is forced transparent by the system, so the seam should not arise; under 3-button
navigation, or beneath an app targeting < SDK 35 still setting an opaque `navigationBarColor`, it
will — and the answer is to record it, not to reach for `TYPE_ACCESSIBILITY_OVERLAY`. **Never
"fix" this by deleting the three deprecated-looking bar calls**: `setDecorFitsSystemWindows` and the
theme's bar colours are disabled on Android 15+ but still live at API 33/34, which is `minSdk`.
*(AOSP source read directly; the on-device rendering consequence is reasoned, not measured — the
reporting device's navigation mode is the one fact that would settle what, if anything, remains.)*

**The aura reaches the true display edge, and `FLAG_LAYOUT_NO_LIMITS` is not what gets it there
[R6].** The decoration window sets `layoutInDisplayCutoutMode = ALWAYS`, and it has to be set
explicitly. No-limits governs extent past the system bars; the display cutout is a SEPARATE
attribute with its own default, under which `WindowLayout` intersects a fullscreen window's PARENT
frame with the display's cutout-safe rect — and the no-limits branch runs afterwards on the DISPLAY
frame only, so it cannot undo it. The glow began where the status-bar strip began: **a hard line,
which is what distinguishes a clipped window from a faint one.** The platform's edge-to-edge
enforcement does not close it either — that is applied in `PhoneWindow.generateLayout`, so it
reaches an Activity's decor and never a window added straight to the `WindowManager`, however recent
the `targetSdk`. `ALWAYS` rather than `SHORT_EDGES`, because short-edges relaxes only the two short
sides per orientation and a long-edge cutout would clip a surface whose whole subject is an unbroken
border. Safe here for the reason a cutout is not extra screen: it is a HOLE, so what extends into it
must be decoration — the narration stack and the Stop pill stay bottom-anchored and never move under
it. *(AOSP `WindowLayout.computeFrames` and `ViewRootImpl.adjustLayoutInDisplayCutoutMode` read
directly; the on-device result is reasoned, not measured.)*

**The device-control border renders at FULL STRENGTH [R6].** The perimeter profile's rails and its
bottom edge both peak at 1.0 of the shader's glow term — the border is the surface's subject, not
what is left over once a bottom bloom has taken its share. The first pass held them at 0.70,
reserving headroom at the corner join; **the corner was already saturated at that level**
(`vGlow 0.697 + perimeter 0.695 = 1.391`, clamped), so the reservation cost 30% of the surface's
luminance and bought nothing. One number (`BORDER_PARITY_LEVEL`) now sets both edges, because a
border whose bottom is brighter than its sides is a bottom wash with extra steps. Measured on the
deterministic terms, composited: rails 0.500 → 0.794, a 1.59× lift, and the rail colour brightens
about 1.75× over a dark backdrop because `glow` also drives the light-vs-deep mix. Above that, only
the window's obscuring-alpha ceiling remains, **and that number is not available**: past it Android
revokes touch pass-through and the window silently swallows every touch on screen.

The two fixes COMPOUND, which is worth knowing before reading either as sufficient alone: an
edge-anchored exponential peaks precisely AT the edge, so while the outermost columns were clipped
the surface was losing exactly its brightest part and showing only the falloff. Some of
"practically invisible" was geometry.

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
  loosening of the fold law above.** *User directive:* the fold exists for the ordinary,
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
  diverge **by decision, not omission** (*user directive*, after the divergence was put to
  them explicitly): a turn's narration can carry more than the tool's own result — a model that sets
  an alarm AND answers something else would lose the something else — and it is not ours to delete.
  The mild restatement on tool-only turns is the accepted cost. Do NOT "fix" this toward reference
  parity, and do not write card copy that assumes the card is the whole answer.
  Anatomy is Google's own, lifted from a GMS-device `uiautomator` capture where the
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
2. **Always-on aurora wash at rest** (shipped, then reverted the same day) → §5: resting = solid
   background; aura strictly invocation/run-gated (chat surface — the overlay's [R4] session pool
   is out of this entry's scope).
3. **Lost transparent top bar** (side effect of the wash layer) → §5 law; `ChatSurface` renders
   no backdrop layer behind the top bar.
4. **Gold/green wash in chat** → §4 aura color law: blue bottom glow only in chat.
5. **Auto-expanded fat tool cards** → §6 tool fold: default-collapsed, cardless, forever. **Scope
   note:** this entry governs the GENERIC many-tools fold only. The promoted-tool
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

17. **A dither that is present, reachable, and still does nothing** (physical Pixel;
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
18. **Absolute uptime narrowed to Float in the shared frame clock** (the same bug
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

20. **Disclaimer renders during a live turn** (supersedes an earlier version of this fix —
    "disclaimer joint collapses to 0dp during streaming"). The bottom-anchored disclaimer was
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
    is **30s** per user directive (≥3× the prior ~10s) — a longer, calmer "the app just
    woke up" breathe (§5). This is the §1 precedence ladder in action (a newer directive over the older
    value); record a new directive rather than "correcting" it back toward the old 10s. A directive-value
    guard, not a shipped-wrong bug — filed here so the value survives a future tuning pass.
22. **The assist overlay must hold the screen awake while it is showing.** A voice turn can
    run long with no touch input to reset the OS dim timer, so without a flag the screen dims
    mid-response. `FLAG_KEEP_SCREEN_ON` is set on the session's real `Window` (the `window?.window`
    Dialog-indirection reference, `voice/CLAUDE.md`) in `AuraSession.onShow()` and cleared on BOTH
    teardown paths (`onHide` AND `onDestroy` — a kill/unbind without a preceding hide would otherwise
    strand it on), mirroring `AssistOverlayPresence.visible`'s set/clear. The debug preview host carries
    a parity one-liner. Overlay-scoped — the chat surface at rest is untouched (§5 solid-resting law).
23. **Transcript items pop / hard-cut on toggle instead of reflowing.** The
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
24. **Bottom sheets rendered past the viewport with no way to reach what fell below the fold.** A
    plain, non-scrollable `Column` sat directly in `ModalBottomSheet`'s `ColumnScope` content slot,
    which caps nothing. Tall, data-driven content — the unbounded model catalog, the project list —
    rendered under the status bar AND past the navigation bar, so the rows below the fold were simply
    unreachable; and with no scrollable descendant to claim a vertical drag past touch slop, the drag
    translated the whole sheet instead of scrolling its content. Worse: with no scroll container to
    consume the gesture first, a scroll attempt landed on a row's `clickable` instead and silently
    changed the user's selected model. → **Every bottom sheet goes through `ui/common/AuraBottomSheet.kt`
    — `ModalBottomSheet` is never called directly anywhere else, enforced by `SheetContainerContractTest`.**
    Bounded content uses the `AuraBottomSheet` entry point (a scrollable `Column`); unbounded,
    data-driven content uses `AuraListBottomSheet` (a `LazyColumn`). A rider on the same defect class:
    the two action sheets WERE bounded and could never overflow, but padded their bottom row with a
    fixed dp constant instead of a real `WindowInsets.navigationBars` inset, leaving the last touch
    target under the gesture bar — an inset is not a padding constant.
25. **`FLAG_LAYOUT_NO_LIMITS` assumed to cover the DISPLAY CUTOUT.** They are separate attributes
    with separate defaults, and the device-control glow shipped a release clipped to the status-bar
    line because of it — no-limits resets the DISPLAY frame while the cutout clamp has already
    intersected the PARENT frame the window is measured against. → **A window added straight to the
    `WindowManager` sets `layoutInDisplayCutoutMode` explicitly.** It inherits nothing from an
    Activity: the platform's edge-to-edge enforcement lives in `PhoneWindow.generateLayout`, so a
    recent `targetSdk` grants such a window neither the cutout mode nor the enforcement — the same
    no-Activity-no-inheritance trap as `FLAG_HARDWARE_ACCELERATED` on the same params.
26. **A perimeter "evened" by damping the bright edge down rather than raising the dim edges up.**
    The first border profile matched its rails and bottom at 0.70 of the glow term and shipped a
    release the device read as "practically invisible"; the headroom it reserved at the corner join
    did not exist, because the corner saturates at any parity above 0.5. → **Evenness is a parity
    LEVEL, declared once (`BORDER_PARITY_LEVEL`) and read by both edges, never an identity that
    emerges from two independently-tuned numbers.** Raise parity, never `iIntensity`: intensity
    leaves `glow` mid-ramp so the border brightens muddier rather than paler, carries the breathe
    swing with it, and manufactures the flat plateau §7 Rule 2 forbids once it clamps.
27. **A focus ring appended AFTER the click modifier — drawn nowhere, warned about nowhere.**
    A whole wave of `Modifier.clickable{ }.auraFocusRing()` calls landed across the drawer, chat and
    settings; every one compiled, every element took focus correctly, and not one ring ever
    rendered. `onFocusChanged` observes only the focus targets that FOLLOW it in the chain, so
    ringing from behind is silently inert — there is no crash, no lint, and the element still
    behaves, which is why reading the diff cannot catch it. → **The ring PRECEDES the click:
    `Modifier.auraFocusRing().clickable{ }`, or `Modifier.auraFocusRing().then(modifier)` where the
    click arrives in a caller's parameter.** A Material `IconButton`/`Switch`/`Button` applies its
    click after its own `modifier`, so passing the ring there is already correct. **Only a rendered
    frame closes this one** — the accessibility tree reports the element as focused either way.
28. **A remote stranded with no focus and no way to get it back.** One press of Down in the composer
    moved focus to a node the IME's reflow then destroyed; from then on the tree reported no focused
    node in any direction, permanently, and the only exit was force-stopping the app. A handheld
    never shows this because a finger re-grants focus. → **A bottom-most control REFUSES the move it
    cannot satisfy** (`focusProperties { down = FocusRequester.Cancel }`), which makes `moveFocus`
    report false and hands the key back to the caret. Do not attempt to recover after the fact:
    Compose dispatches no key event at all once focus is gone, so a root `onPreviewKeyEvent` never
    fires — measured, not assumed.
29. **A modal drawer's focus containment must be conditional in BOTH directions, or it trades one
    trap for a worse one.** `ModalNavigationDrawer` composes its sheet content even while CLOSED, so
    an unconditional `exit = Cancel` on that content would refuse every attempt to leave it — trapping
    a remote inside a drawer the user cannot even see, which is strictly worse than the open-drawer
    escape it exists to fix, and on television unrecoverable for the same reason entry 28 is
    (`ui/common/DpadFocusContainer`'s own law: nothing recovers focus once it is gone). → `containsFocus`
    on `NavigationHost.ModalSheet` is read together with `isActive`: CLOSED refuses ENTRY, OPEN refuses
    EXIT, never both at once and never the closed→trap direction alone (`ui/navigation/AuraDrawerContent`'s
    `focusProperties { enter; exit }`).
30. **A permanent rail must NOT be focus-contained, or the remote is stranded in the navigation list
    with no way into the app.** The modal sheet's containment law (entry 29) does not transfer whole —
    a rail is on screen for the app's entire lifetime, so refusing focus EXIT the way a sheet refuses
    it while open would mean the transcript, composer and every other destination are permanently
    unreachable by D-pad. → `NavigationHost.PersistentRail.containsFocus = false`, always: moving focus
    right into the transcript is the primary way a rail is used, not an edge case to guard against.

## 8. Enforcement map

- Ordering/turn shape → `data/model/TranscriptReducer.kt` + `TranscriptReducerTest` (contract
  suite; keep green on any event/serialization change).
- Token discipline → review law (no literals outside `ui/theme/`); tokens carry provenance KDoc.
- Aura contract → `ChatScreen` (sole glow owner) + `ui/aurora/CLAUDE.md` shader laws.
- Sheet contract (bounding/scroll/insets) → `ui/common/AuraBottomSheet.kt` + `SheetContainerContractTest`
  (no direct `ModalBottomSheet` call anywhere else).
- This file → linked from the app-root `CLAUDE.md` tree table; scoped CLAUDE.mds cite it instead
  of restating values, so there is exactly one place values live.

## 9. Iconography

Icon sourcing has ONE order of preference (user directive: never hand-roll an icon when an
off-the-shelf one covers the case):

1. **`material-icons-extended` first for any NEW glyph** (added to the dependency catalog,
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
