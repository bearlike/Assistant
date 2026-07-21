package com.mewbo.aura.ui.apps

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewAssetLoader
import com.mewbo.aura.data.model.AppDetail
import com.mewbo.aura.data.model.AppToken
import com.mewbo.aura.ui.chat.widget.buildStliteWebView
import com.mewbo.aura.ui.theme.AuraColors
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * The app detail screen's frontend renderer — the SAME `widget-host.html` WebView seam
 * [com.mewbo.aura.ui.chat.widget.WidgetCard] uses (design spec §4D: "reuse the PROVEN widget-host
 * WebView seam"), posting the NEW `mewbo-app-payload` message alongside the existing
 * `mewbo-widget-payload` (backward-compatible; the widget flow is untouched — see
 * [buildStliteWebView]'s KDoc). Unlike [WidgetCard]'s bounded card + fullscreen-dialog duality, an
 * app IS its own screen already, so this fills whatever space [modifier] gives it directly.
 */
@Composable
fun AppWebView(detail: AppDetail, token: AppToken?, apiBase: String, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val assetLoader = remember(context) {
        WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(context))
            .build()
    }
    val canvasArgb = AuraColors.surfaceCanvas.toArgb()
    val messageJson = remember(detail.appId, detail.version, token?.tokenId, apiBase) {
        appMessageJson(detail, token, apiBase)
    }

    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            // `tag` doubles as "the payload this WebView currently has loaded" — read/written by
            // `update` below, never anything else. Stamped here (not left null) so the FIRST
            // `update` call (AndroidView invokes it immediately after `factory`, same composition)
            // sees an equal tag and skips reposting: the initial payload delivery is already owned
            // by `buildStliteWebView`'s own ready-signal-gated `postPayloadOnce()`, and posting it
            // a second time here would race that gate and reboot a kernel that hasn't even booted
            // once yet.
            buildStliteWebView(ctx, assetLoader, messageJson, canvasArgb).also { it.tag = messageJson }
        },
        update = { webView ->
            // Re-post into the ALREADY-mounted WebView when the payload actually changed — a
            // refreshed short-lived token (spec §2.7) or refreshed file contents after a trigger
            // pause/resume (AppDetailViewModel.refresh()) otherwise never reaches a WebView that
            // was created once on first composition and never told again. `update` runs on every
            // recomposition regardless of whether `messageJson` changed, so the tag comparison is
            // what stops an unrelated recomposition from rebooting a perfectly fine kernel — the
            // host page's `render()` tears down and remounts on every re-post (host.ts contract),
            // which is a real cost, not a free no-op.
            if (webView.tag != messageJson) {
                webView.tag = messageJson
                webView.post { webView.evaluateJavascript("window.postMessage($messageJson, '*')", null) }
            }
        },
        // Destroy the WebView (and its Pyodide worker) when the screen leaves composition.
        onRelease = { it.destroy() },
    )
}

/**
 * The `{type:"mewbo-app-payload", payload:{entrypoint, files, requirements, app_context}, theme}`
 * message (design spec §4D) as compact JSON. `app_context` mirrors the console's `AppContext` type
 * (`apps/mewbo_console/src/types.ts` — `{token, api_base, app_id}`) FIELD-FOR-FIELD; the console's
 * `widget-host.html` (`src/widget/host.ts`'s `parseAppMessage`) validates exactly this shape, and
 * `stliteBoot.ts`'s `buildAppKernelOptions` re-serializes it into an injected `_app_context.json` the
 * bundled Python SDK reads. `api_base` is what lets the SDK's requests resolve against the SAME
 * backend the WebView's own asset requests never touch (same-origin serving, spec §2.7 — the master
 * key never enters the WebView; only this scoped [token] does). An absent token serializes as an
 * empty string rather than omitting the key, so the shape stays structurally stable for the SDK to
 * parse even while a mint is still in flight.
 */
private fun appMessageJson(detail: AppDetail, token: AppToken?, apiBase: String): String = buildJsonObject {
    put("type", "mewbo-app-payload")
    putJsonObject("payload") {
        put("entrypoint", detail.frontend.entrypoint)
        putJsonObject("files") {
            detail.frontend.files.forEach { (name, content) -> put(name, content) }
        }
        putJsonArray("requirements") { detail.frontend.requirements.forEach { add(it) } }
        putJsonObject("app_context") {
            put("token", token?.tokenId ?: "")
            put("api_base", apiBase)
            put("app_id", detail.appId)
        }
    }
    // Aura is dark-only (v1) — same convention WidgetCard's own payload already follows.
    put("theme", "dark")
}.toString()
