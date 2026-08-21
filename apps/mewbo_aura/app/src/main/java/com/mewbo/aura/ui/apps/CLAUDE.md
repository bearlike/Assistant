> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Mewbo Apps — ui/apps/

Scope: `ui/apps/` — the gallery, detail and creation screens for the Mewbo Apps sub-product, plus
`AppWebView`, the detail screen's frontend renderer. The advertise half (the `apps` capability
header), the repository and the wire DTOs live elsewhere: [`di/CLAUDE.md`](../../di/CLAUDE.md),
[`data/repo/CLAUDE.md`](../../data/repo/CLAUDE.md), [`data/api/CLAUDE.md`](../../data/api/CLAUDE.md).

## The payload reaches the WebView through exactly two doors — never a third

`AppWebView` reuses `buildStliteWebView` ([`ui/chat/widget/`](../chat/widget/CLAUDE.md)) verbatim; it
was already payload-agnostic (it takes a `messageJson: String`). Delivery is split, and the split is
what keeps it correct:

- **The FIRST payload is `buildStliteWebView`'s ready-signal-gated `postPayloadOnce()`** — the page
  posts `mewbo-widget-host-ready` when the kernel is live. Never `onPageFinished`; the widget card's
  own CLAUDE.md owns that rule and it is binding here.
- **A LATER payload is the `AndroidView` `update` block, guarded by the WebView's `tag`.** `factory`
  runs ONCE, so a `refresh()` that mints a new token or new file contents had nowhere to go.

**The `tag` IS "the payload currently loaded", and it is stamped in `factory` too.** That is the
whole mechanism: `AndroidView` fires `update` immediately after `factory`, and that first call sees an
equal tag and SKIPS — so the true first delivery stays with `postPayloadOnce()` and the two never
race. Drop the `factory` stamp and the first `update` reposts into a kernel that has not booted.

A repost is not free: the host page's `render()` unmounts the running kernel and mounts a fresh
Pyodide worker every time (kernel options are init-only — there is no in-place update path). An
unchanged `messageJson` must stay a no-op.

**Known, unreachable:** the `update` repost bypasses the ready gate, so in theory a pre-ready repost
is dropped and the ready signal then delivers the stale `factory` payload. The ready signal fires
local-asset-fast while a refresh needs a human tap plus a network round-trip. Don't add a second gate
without a real repro.

## `mewbo-app-payload` is a FIXED cross-stream interface — a rename fails SILENTLY

`appMessageJson()` builds `{type:"mewbo-app-payload", payload:{entrypoint, files, requirements,
app_context:{token, api_base, app_id}}, theme}`, mirroring the console's `host.ts` `parseAppMessage`
+ `types/apps.ts` `AppFrontendPayload`/`AppContext` field-for-field.

The failure mode is why this is stated so strongly: **`parseAppMessage` returns `null` on ANY
deviation** — a missing key, a renamed key, a non-string `token`, or a `files` map lacking the
`entrypoint` key. `parseHostMessage` then ignores the message entirely. There is no error, no
console warning and no partial render — the app just never mounts. So a field rename on either side
is not a degraded render, it is a blank screen with nothing to grep for.

Two traps inside the shape:

- **`token` is the whole `token_id`** — the bearer credential itself, an HMAC-signed opaque string.
  There is no separate lookup step, and the console's `AppReadToken` has no distinct `token` field
  either.
- **`api_base`, NOT `base_url`.** It resolves from `SettingsStore.baseUrl` (cached on
  `AppDetailViewModel.apiBase`), the SAME value `AuthInterceptor` reads, so the injected Python SDK
  talks to the identical backend — the master key never enters the WebView, only the scoped token.

`AppContext.scope` is optional console-side and Aura does not send it. That is fine today precisely
because it is optional; making it required upstream would silently blank every Aura render.

An absent token serializes as `""` rather than dropping the key — the shape must stay structurally
stable for `parseAppMessage` while a mint is still in flight, and a dropped key would fail the parse
outright.

## Nullable degrades are deliberate, not unwired

`AppDetailUiState.Loaded` carries a nullable `token` AND a nullable `health`: a mint failure renders
an unauthenticated WebView (the injected SDK surfaces its own "refresh the app" state on an invalid
token) and a `/system` failure renders the status dot alone. Neither blocks the WebView, which is the
actual point of the screen. Same idiom as the gallery's `freshness: Map<String, AppFreshness?>`,
where `null` reads identically for "never run" and "the per-card fetch failed" — a card must never
paint a false-green.
