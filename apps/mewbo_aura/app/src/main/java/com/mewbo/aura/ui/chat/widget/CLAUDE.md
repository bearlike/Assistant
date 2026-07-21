> ↑ [chat/CLAUDE.md](../CLAUDE.md) · [ui/CLAUDE.md](../../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../../CLAUDE.md)

# Aura Streamlit Widget Card — ui/chat/widget/

Scope: `ui/chat/widget/` — `WidgetCard`, the `ChatItem.Widget` renderer that hosts a model-built
Streamlit app. A bounded `WebView` (via `AndroidView`) that serves the console-built offline
stlite bundle through `WebViewAssetLoader` from `appassets.androidplatform.net/assets/widget-host/`.
The advertise-half (the `stlite` capability header + the reducer's `foldWidget`) lives elsewhere, both
gated on `streamlitWidgetsEnabled` — see [`di/CLAUDE.md`](../../../di/CLAUDE.md) and
[`data/model/CLAUDE.md`](../../../data/model/CLAUDE.md).

## The ready-signal contract — NEVER `onPageFinished`

Post the `{app.py, data.json}` payload **on the host's `mewbo-widget-host-ready` window message, NOT
`onPageFinished`.** The ready signal is in the host contract precisely so an embedder never depends on
the host's internal load order — a lazy-loaded listener would silently drop an `onPageFinished`-timed
post. A document-start bridge script forwards the host's window-level ready message onto a
`WebViewCompat.addWebMessageListener` channel; only then does Android post, **EXACTLY ONCE per load** (a
re-post reboots the Pyodide kernel). `onPageFinished` is a fallback ONLY on a WebView too old for
`WEB_MESSAGE_LISTENER`/`DOCUMENT_START_SCRIPT`.

## Page-side traps (fix the PAGE, never Kotlin)

- **WebView viewport units read 0.** `vh`/`dvh`/`%`-on-root all evaluate to `0` inside a `WebView` — a
  widget that sizes off them collapses. This is a PAGE-side fix (the console bundle), never Kotlin.
- **`.mjs` MIME.** If the bundle is ever served over a proxy whose `mime.types` lacks `mjs`, Pyodide is
  handed `application/octet-stream` and the kernel dies — a serving-side fix, not a client one.

## Docked vs overlay, and the shared-component rule

`ChatItemRow` picks the renderer by the `allowRichWidgets` flag threaded from `ChatTranscript`
(`true` docked / `false` overlay) — the same "extend the shared component's param surface, never fork"
move as v5's `fillParent`. The overlay renders `WidgetSummaryCard` (a compact tap-to-open "Widget ·
{summary}" card whose tap reuses the overlay's expand/handoff) — booting Pyodide in a small floating
overlay is wrong. `WidgetCard` is `isChipFamily` for spacing only (a full-width payload card, not a
glance row), `widget_id`-keyed so re-reduction never rebuilds a visible one. A full-screen affordance
(`ChatIcons.Fullscreen`) opens the widget in a full-screen dialog. **v1 limitation:** a `WebView` in a
`LazyColumn` item reboots Pyodide on far scroll-off (no Compose portal equivalent) — accepted.

Device-verified end-to-end on redroid via the mock `widget` scenario (offline bundle booted, no network);
Streamlit's full paint is a Pixel-tier check (Pyodide boot on SwiftShader is too slow to demonstrate).
