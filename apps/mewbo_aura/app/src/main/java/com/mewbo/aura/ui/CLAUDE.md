> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md) · children: [theme](theme/CLAUDE.md) · [orb](orb/CLAUDE.md) · [aurora](aurora/CLAUDE.md) · [chat](chat/CLAUDE.md) · [composer](composer/CLAUDE.md) · [overlay](overlay/CLAUDE.md) · [apps](apps/CLAUDE.md) · [control](control/CLAUDE.md) · [common](common/CLAUDE.md) · [navigation](navigation/CLAUDE.md) · [sessions](sessions/CLAUDE.md) · [search](search/CLAUDE.md) · [settings](settings/CLAUDE.md)

# Aura UI — Compose Surface Guidance (hub)

> **Canonical design system: [`DESIGN.md`](../../../../../../../../DESIGN.md).** Token VALUES, visual
> laws, provenance, and the "never again" regression registry live there — one place, not restated
> across the scoped CLAUDE.mds. When a number or a law below disagrees with DESIGN.md, DESIGN.md wins
> and this file is stale — fix it.

Scope: `ui/` — theme, orb, screens, shared chat components. **This file is the thin router + whatever
is genuinely shared across ALL of `ui/`; every decision-heavy subtree has its own CLAUDE.md — read the
child before re-deriving anything about it.**

| Sub-package | Owns |
|---|---|
| [`theme/`](theme/CLAUDE.md) | tokens, `AuraTheme`, token discipline, `AssistantExtras`, the reduced-motion two-source seam |
| [`orb/`](orb/CLAUDE.md) | the brand orb + the shared AGSL primitives (`GlslNoise`/`ClayFlowerSdf`/`ShaderFrameClock`, incl. `ditherPremul`) |
| [`aurora/`](aurora/CLAUDE.md) | `AuroraEdgeGlow`/`AuroraWashTop` shader family rebuild rules |
| [`chat/`](chat/CLAUDE.md) | docked chat surface: `ChatScreen`/`Surface`/`Transcript`, `ChatViewModel`, message-action long-press, the composer scope row, `ModelProviderIcons` |
| [`chat/toolcards/`](chat/toolcards/CLAUDE.md) | promoted-tool action cards + **the `PromotedTools` allowlist trap** |
| [`chat/widget/`](chat/widget/CLAUDE.md) | the Streamlit widget WebView card (ready-signal contract) |
| [`composer/`](composer/CLAUDE.md) | `AuraComposer`, five `ComposerState`s, `RmsWaveform`, docked-scope-row alignment anchor |
| [`overlay/`](overlay/CLAUDE.md) | the assist-overlay render of `AssistUiState` |
| [`apps/`](apps/CLAUDE.md) | Mewbo Apps gallery/detail/create + `AppWebView`'s two-door payload delivery and the fixed `mewbo-app-payload` envelope |
| [`control/`](control/CLAUDE.md) | the device-control overlay: the two-window host, the capture/tap veil, the narration fold |
| [`common/`](common/CLAUDE.md) | shared vocabulary: `ActionSheet`, `AuraBottomSheet`, `MarkdownMessage`/`MarkdownBuffer`, `ErrorCard`, `NoticeHost`, `TypingIndicator`, `AttachmentTile` |
| [`navigation/`](navigation/CLAUDE.md) | the drawer + routes + `SessionActionsSheet` |
| [`sessions/`](sessions/CLAUDE.md) | recents view-state + the pure rail helpers (`RecentsFilter`/`SessionGrouping`/`RelativeTime`) |
| [`search/`](search/CLAUDE.md) | the client-side title-only chat search |
| [`settings/`](settings/CLAUDE.md) | the 7-section settings screen |

## Theme discipline (→ [`theme/`](theme/CLAUDE.md))

No `Color(`, dp radius literal, or `spring(` outside `ui/theme/`. All tokens come from
`AuraTheme`/`AssistantExtras`; add a token, then consume it — literals ship looking right and rot the
first palette change. Mechanism + this wave's new tokens live in the theme child.

## Compose stability — measured, not claimed (ui-wide)

`@Immutable` on `ChatItem`/`ChatUiState` is load-bearing: a single `List<...>` field makes the whole
sealed hierarchy Compose-unstable and every visible row re-executes per streaming delta. When touching
item models or row composables, re-verify with temporary Log-in-composition counters + a scripted delta
sequence in `ChatPreviewActivity` → streaming row N/N, other rows 0/N. Numbers, not narrative, before
claiming recomposition discipline.

Three real breaks found this way, all silent (compiled fine, just recomposed too much):

- A per-item callback CURRIED at the list-dispatch site (a fresh `{ onX(item) }` built inside the
  dispatch function) breaks the row's own parameter stability and forces it to recompose on ANY
  sibling's change — curry only inside the row's own body, pass the raw `(Item) -> Unit` down.
- A callback-BUNDLE (e.g. `ChatCallbacks`) constructed inline on every recomposition of the host
  defeats stability no matter how far downstream you curry — hoist it behind
  `remember(viewModel, …) { … }`.
- `ChatTranscript`'s `ChipEntranceAnimator` tracks "seen keys" in a plain
  `remember { mutableSetOf<String> }`, deliberately NOT `mutableStateOf`-backed — reading a shared
  `State`-backed set from every item's composition would recompose EVERY chip whenever any one new key
  got added.

## One chat tree, reused as a COMPONENT — never forked (cross-surface law)

`ChatTranscript` is the ONE composable both `ChatSurface` (docked, `MainActivity` host) and the assist
overlay's `ResponseCard` render — never a second rendering path. It carries two additive parameters for
the overlay: `fillParent: Boolean = true` (sizing) and `allowRichWidgets` (the widget
docked-vs-summary split); both defaults preserve docked behavior byte-for-byte. **Extend the shared
component's parameter surface for any future overlay-vs-docked difference, never fork the composable.**
Chat-surface mechanics: [`chat/`](chat/CLAUDE.md); the overlay's render: [`overlay/`](overlay/CLAUDE.md).

## Accessibility

TalkBack labels for orb states; touch targets ≥ 48dp; reduced-motion honored everywhere motion exists.

**Reduced-motion has TWO sources, OR-ed in ONE seam (`AuraTheme`,)** — mechanism in
[`theme/`](theme/CLAUDE.md). Every downstream consumer branches on `extras.reducedMotion` regardless of
source. The ui-wide consequence: **every `AuraTheme` caller must actually thread the setting.**
`AuraTheme(reducedMotion = …)` is a defaulted parameter, so a bare `AuraTheme { … }` silently compiles
and drops the in-app toggle; this bit `AuraSession` (the real `VoiceInteractionSession` host) for a full
release cycle, and the debug-only `AssistOverlayPreviewActivity` had the same gap. Both now collect
`settingsStore.reducedMotion.collectAsState(...)` into their `setContent` (via the Hilt `EntryPoint`
escape hatch, same as `haptics`/`permissionChecker`).
