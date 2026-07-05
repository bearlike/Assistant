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
bubble→response gap), **[#176/#181]** = measured reference-capture values from those Gitea issues,
**[Rev F]** = instrumented uiautomator audit. Precedence: **the newest user directive wins over
every measured reference value**. Do not "fix" a directive value back toward reference parity —
record a new directive instead.

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
| `listItem` | **16 / 23** | Normal | [R1] · [R2] |
| `markdownH1 / H2 / H3` | **24/32 · 20/28 · 18/26** | N · Med · Med | [R1] (measured ramp scaled one step down) |
| markdown H4–H6 | = `bodyMessage` verbatim | — | [#181] (heading by placement only) |
| `sectionHeader` / `metaTrailing` | **14** | Normal | [R1] |
| `chipLabel` | **13** | Normal | [R1] |
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
| `ActivityGroup` | rowHeight 36dp, topGapAfterBubble 32dp [R3], detailMaxHeight 200dp | tool fold geometry [#177] |
| `Markdown.headingTopGap / BottomGap` | 36 / 16dp | headings cling to what follows (~2:1) [#181 measured] |
| `UserBubble` | pad 16/12, rightMargin 24, maxWidth 0.78 | [Rev F] |
| `DrawerRow` recents (rail) | rowHeight **44dp** (vs 56dp action rows), `dateGroupTopPad` 12dp, `runningDotSize` 8dp trailing | compact, date-grouped Recents [2026-07-03 directive] |

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
`surfaceInput #1E1F23` (bubbles, composer, chips), `surfaceSelected #26282C` (code blocks —
always contrasts with its parent, a same-color-on-same-color block is a bug we shipped once),
`outlineHairline #2A2B2E` (ALL dividers). Text tiers: `textPrimary #E9EAED` → `textSecondary
#9AA0A6` → `textTertiary #5F6368` — a hierarchy step means stepping BOTH size and tier where
possible. Accents: `accentPrimary #4C6EF5`, `accentError #E46962` (failure glyphs only).
Shapes: `radiusPill` (stadium), `radiusBubble` 28dp, `radiusThumb` 16dp (code blocks),
`radiusCard` 20dp.

**Aura color law [R2]:** the in-chat aura is the **blue** bottom edge-glow family
(`auroraOverlayBloom` near-black navies). The gold/green `auroraWashTop` palette renders NOWHERE
in the product today (debug `LivenessShowcase` only — the overlay and landing both use the blue
edge glow) and must NEVER render in the chat surface — the user explicitly rejected it there
("shit yellow/brown/green").

## 5. State & liveness language

| App state | Visual | Contract |
|---|---|---|
| **Resting** (idle, no run, invocation window elapsed) | **Solid background. NO aura. Transparent top bar.** | [R2] "the native resting state of the app is just a solid color background" |
| **Fresh invocation** (app/chat screen just opened) | bottom blue glow, ambient, ~10s window, then fades out smoothly | [R2] |
| **Run live** (Sending/Streaming) | bottom blue glow (Thinking), slightly faster than ambient — speed changes phase-continuous, never a jump | [R2]; speed boost constant lives in `ChatScreen` |
| **Run live** (transcript) | `AuraSpark` row visible at the transcript bottom for the ENTIRE run — follow-ups and mid-turn tool phases included | [R1] |

Laws: the bottom/edge glow is **`ChatScreen`'s** layer (user-tuned `IN_APP_GLOW_*` constants —
do not retune against SwiftShader); `ChatSurface` renders **no aura of its own**. Aura alpha
must decay to **exactly 0 before draw bounds** (smoothstep edge window + dither) — clipped
falloff reads as a visible line (§7.9). The **top bar stays transparent** in chat; nothing may
paint an opaque or washed layer behind it (§7.3). When a run **ends**, the glow **eases off over
≥3s** (linear, `AuroraEdgeGlow(dismissFadeMs = AuraMotion.edgeRestFadeMs)`) rather than snapping to
black — an abrupt on→off flash is a photosensitivity trigger [R3]. The fade HOLDS the last active
frame (reach, intensity, wave phase+speed) and ramps only its alpha to 0; handing the shader
`Hidden`'s own uniforms cannot fade (its intensity 0f zeroes alpha, its wide reach pops the bloom) —
see `ui/aurora/CLAUDE.md`. Caller-knob so the overlay keeps its own quick dismiss (§7.15).

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
   background; aura strictly invocation/run-gated.
3. **Lost transparent top bar** (side effect of the wash layer) → §5 law; `ChatSurface` renders
   no backdrop layer behind the top bar.
4. **Gold/green wash in chat** → §4 aura color law: blue bottom glow only in chat.
5. **Auto-expanded fat tool cards** → §6 tool fold: default-collapsed, cardless, forever.
6. **Error banner under successful chats** → §6 error-card law + reducer test.
7. **Disclaimer mid-transcript** (first-turn anchoring — twice: original per-message form, then
   first-turn form) → §6: bottom-anchored single item at the transcript end.
8. **Same-color-on-same-color result block** → §4: code blocks always `surfaceSelected` against
   a differing parent.
9. **Visible line/edge artifact in glow/orb falloff** → §5 edge-window law (alpha 0 before
   bounds; keep dither). First hit the orb (#176), then the edge glow.
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

## 8. Enforcement map

- Ordering/turn shape → `data/model/TranscriptReducer.kt` + `TranscriptReducerTest` (contract
  suite; keep green on any event/serialization change).
- Token discipline → review law (no literals outside `ui/theme/`); tokens carry provenance KDoc.
- Aura contract → `ChatScreen` (sole glow owner) + `ui/aurora/CLAUDE.md` shader laws.
- This file → linked from the app-root `CLAUDE.md` tree table; scoped CLAUDE.mds cite it instead
  of restating values, so there is exactly one place values live.
