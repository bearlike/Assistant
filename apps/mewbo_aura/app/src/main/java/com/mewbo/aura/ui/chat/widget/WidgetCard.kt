package com.mewbo.aura.ui.chat.widget

import android.annotation.SuppressLint
import android.view.MotionEvent
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.navigation.IS_DEBUG_BUILD
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * The full-app renderer for a [ChatItem.Widget]: a bounded [WebView] hosting the
 * console-built, self-contained stlite bundle. The bundle is bundled into `assets/widget-host/` (see
 * `app/build.gradle.kts` `syncWidgetHostAssets`) and served through [WebViewAssetLoader] from the
 * synthetic origin the widget-host's relocatable build expects (`appassets.androidplatform.net`), so
 * everything — the page, its JS/CSS, the stlite wheels, the vendored Pyodide runtime — resolves
 * offline with ZERO network.
 *
 * **Payload delivery is triggered by the host's `mewbo-widget-host-ready` message, NOT
 * `onPageFinished`.** The ready signal exists in the host contract
 * (`apps/mewbo_console/src/widget/host.ts`) precisely so an embedder never depends on the host's
 * internal load order — if the console ever lazy-loads its `message` listener, an `onPageFinished`-
 * timed post would silently drop. The host posts `{type:"mewbo-widget-host-ready"}` to `window`; a
 * document-start bridge script ([READY_FORWARD_SCRIPT]) forwards that onto the injected
 * [ANDROID_BRIDGE_NAME] channel (`WebViewCompat.addWebMessageListener`), and only then does Android
 * post the `{app.py, data.json}` payload back via `window.postMessage`. EXACTLY ONCE per load —
 * re-posting reboots the Pyodide kernel (host contract). `onPageFinished` is a fallback ONLY on a
 * WebView too old for the `WEB_MESSAGE_LISTENER`/`DOCUMENT_START_SCRIPT` features.
 *
 * The assist overlay renders [WidgetSummaryCard] instead — booting Pyodide inside a small floating
 * overlay is wrong; the overlay hands off to this chat surface for the real thing.
 *
 * Known v1 limitation: a `WebView` inside a `LazyColumn` item is disposed when the item scrolls far
 * off-screen and re-created (Pyodide reboots) on scroll-back. The console avoids this by portalling
 * the widget to `document.body`; there is no Compose equivalent without an overlay window, so this is
 * accepted for v1 — the card is keyed by `widget_id`, so re-reduction never rebuilds a visible one.
 *
 * The bounded card is a small window (`AuraSpacing.Widget.height`) onto a widget that can be much
 * taller, so a "view full screen" affordance sits at the card's bottom-right corner and opens
 * [WidgetFullscreenView] — a full-screen [Dialog] where the same widget renders edge-to-edge and
 * scrolls freely. Exit is the dialog's own back-press handling plus a visible close button; no
 * navigation route is involved.
 */
@Composable
fun WidgetCard(widget: ChatItem.Widget, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    // The default WebViewAssetLoader domain is appassets.androidplatform.net; the "/assets/" handler
    // maps that path onto the app's assets, so the bundle at assets/widget-host/ is reachable at
    // .../assets/widget-host/widget-host.html — exactly the relocatable base the console build emits.
    val assetLoader = remember(context) {
        WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(context))
            .build()
    }
    val canvasArgb = AuraColors.surfaceCanvas.toArgb()
    val messageJson = remember(widget.key) { widgetMessageJson(widget) }
    var showFullscreen by remember { mutableStateOf(false) }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        AndroidView(
            modifier = Modifier
                .fillMaxWidth()
                .height(AuraSpacing.Widget.height)
                .clip(RoundedCornerShape(AuraShape.radiusThumb))
                .border(AuraShape.hairlineWidth, AuraColors.outlineHairline, RoundedCornerShape(AuraShape.radiusThumb)),
            factory = { ctx -> buildStliteWebView(ctx, assetLoader, messageJson, canvasArgb) },
            // Destroy the WebView (and its Pyodide worker) when the row leaves composition for good.
            onRelease = { it.destroy() },
        )

        // "View full screen" affordance, pinned to the card's bottom-right corner. A subtle filled
        // backing (the same treatment as the fullscreen view's close button) keeps the neutral glyph
        // legible over any widget content. IconButton is intrinsically a 48dp touch target.
        IconButton(
            onClick = { showFullscreen = true },
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .padding(AuraSpacing.Widget.affordanceGap)
                .background(AuraColors.surfaceInput, AuraShape.radiusPill),
        ) {
            Icon(
                imageVector = ChatIcons.Fullscreen,
                contentDescription = "View widget full screen",
                tint = AuraColors.textPrimary,
                modifier = Modifier.size(AuraSpacing.Widget.affordanceIcon),
            )
        }
    }

    if (showFullscreen) {
        WidgetFullscreenView(
            assetLoader = assetLoader,
            messageJson = messageJson,
            canvasArgb = canvasArgb,
            onExit = { showFullscreen = false },
        )
    }
}

/**
 * The widget shown edge-to-edge in a full-screen [Dialog]. Two ways out, both plain: the system back
 * button (the dialog's `onDismissRequest`) and the visible close button. A fresh [WebView] here reboots
 * the Pyodide kernel — kernel options are init-only, and moving a live WebView between Compose hosts is
 * fragile — which is an acceptable one-time cost for a deliberate expand. Unlike the bounded card, this
 * WebView is NOT inside the chat's `LazyColumn`, so its content scrolls natively with no gesture fight.
 */
@Composable
private fun WidgetFullscreenView(
    assetLoader: WebViewAssetLoader,
    messageJson: String,
    canvasArgb: Int,
    onExit: () -> Unit,
) {
    Dialog(
        onDismissRequest = onExit,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Box(modifier = Modifier.fillMaxSize().background(AuraColors.surfaceCanvas)) {
            AndroidView(
                modifier = Modifier.fillMaxSize().systemBarsPadding(),
                factory = { ctx -> buildStliteWebView(ctx, assetLoader, messageJson, canvasArgb) },
                onRelease = { it.destroy() },
            )
            IconButton(
                onClick = onExit,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .systemBarsPadding()
                    .padding(AuraSpacing.Widget.affordanceGap)
                    // A filled backing keeps the exit affordance legible over any widget content.
                    .background(AuraColors.surfaceInput, AuraShape.radiusPill),
            ) {
                Icon(
                    imageVector = Icons.Filled.Close,
                    contentDescription = "Exit full screen",
                    tint = AuraColors.textPrimary,
                    modifier = Modifier.size(AuraSpacing.Widget.affordanceIcon),
                )
            }
        }
    }
}

/**
 * The generic stlite-in-WebView factory behind BOTH [WidgetCard] (`mewbo-widget-payload`) and the
 * Mewbo Apps detail screen ([com.mewbo.aura.ui.apps], `mewbo-app-payload`) — it is already
 * payload-agnostic (the caller-built [messageJson] is the only thing that varies), so widened from
 * `private` to `internal` rather than duplicated: same ready-signal bridge, same asset loader, same
 * navigation guard, same touch-scroll fix, for both payload shapes the ONE `widget-host.html` page
 * understands (`apps/mewbo_console/src/widget/host.ts`'s `parseWidgetMessage`-style dispatch).
 */
@SuppressLint("SetJavaScriptEnabled", "ClickableViewAccessibility")
internal fun buildStliteWebView(
    ctx: android.content.Context,
    assetLoader: WebViewAssetLoader,
    messageJson: String,
    backgroundArgb: Int,
): WebView = WebView(ctx).apply {
    if (IS_DEBUG_BUILD) WebView.setWebContentsDebuggingEnabled(true)
    setBackgroundColor(backgroundArgb)
    settings.apply {
        javaScriptEnabled = true // stlite/Pyodide is a JS/WASM runtime — the whole point of the host
        domStorageEnabled = true
        // Assets ride the WebViewAssetLoader synthetic origin, never file:// — keep raw file access off.
        allowFileAccess = false
        allowContentAccess = false
        mediaPlaybackRequiresUserGesture = false
    }

    // Post exactly once per load — a second post reboots the Pyodide kernel (host contract).
    var posted = false
    fun postPayloadOnce() {
        if (posted) return
        posted = true
        post { evaluateJavascript("window.postMessage($messageJson, '*')", null) }
    }

    // Preferred path: post ON the host's ready signal (contract), not on page-load timing. Needs both
    // the message-listener channel (page → Android) and a document-start script that forwards the
    // host's window-level `mewbo-widget-host-ready` message onto that channel.
    val readyViaListener = WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) &&
        WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)
    if (readyViaListener) {
        WebViewCompat.addWebMessageListener(this, ANDROID_BRIDGE_NAME, WIDGET_ORIGIN_RULES) { _, message, _, _, _ ->
            if (message.data == HOST_READY_TOKEN) postPayloadOnce()
        }
        WebViewCompat.addDocumentStartJavaScript(this, READY_FORWARD_SCRIPT, WIDGET_ORIGIN_RULES)
    }

    webViewClient = object : WebViewClient() {
        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
            assetLoader.shouldInterceptRequest(request.url)

        // Pin this JS-enabled WebView to the offline widget-host origin — a model-authored widget is
        // untrusted output, so an external link in it must never NAVIGATE the view off appassets.
        // Same-origin navigations (the host page + its own assets) proceed; anything else is blocked
        // (returning true = "the app handled it", so the WebView does not load the URL).
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean =
            request.url.host != WIDGET_HOST_AUTHORITY

        override fun onPageFinished(view: WebView, url: String?) {
            // Fallback ONLY when the ready-signal features are unavailable (a WebView too old for
            // WEB_MESSAGE_LISTENER/DOCUMENT_START_SCRIPT) — post-on-ready is the contract otherwise.
            if (!readyViaListener) postPayloadOnce()
        }
    }

    // A WebView inside the chat's scrolling LazyColumn: Compose's own gesture detector wins the
    // vertical drag, so a widget taller than the bounded card never scrolls under a finger — only
    // programmatic scroll moved it (verified on a Pixel 7 Pro over CDP: a 700px swipe advanced the
    // stlite scroll container 3.7px, the rest went to the chat). While a touch is down, ask every
    // ancestor up to the host ComposeView not to intercept, so the drag reaches the WebView's own
    // scroller; release on up/cancel so a gesture that STARTS outside the card still scrolls the chat.
    setOnTouchListener { view, event ->
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE ->
                view.parent?.requestDisallowInterceptTouchEvent(true)
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
                view.parent?.requestDisallowInterceptTouchEvent(false)
        }
        false // never consume — the WebView still handles its own scrolling, taps, and a11y clicks
    }
    loadUrl(WIDGET_HOST_URL)
}

/** The `{type:"mewbo-widget-payload", payload, theme}` message the host page expects, as compact
 * JSON (a valid JS object literal — kotlinx escapes the arbitrary `app.py`/`data.json` strings). */
private fun widgetMessageJson(widget: ChatItem.Widget): String = buildJsonObject {
    put("type", "mewbo-widget-payload")
    putJsonObject("payload") {
        put("widget_id", widget.widgetId)
        put("session_id", widget.sessionId)
        putJsonObject("files") {
            put("app.py", widget.appPy)
            put("data.json", widget.dataJson)
        }
        putJsonArray("requirements") { widget.requirements.forEach { add(it) } }
    }
    // Aura is dark-only (v1) — flips the stlite bundled palette to the dark theme.
    put("theme", "dark")
}.toString()

/** The synthetic origin the offline bundle is served from ([WebViewAssetLoader]'s default domain);
 * the navigation guard pins the WebView to it. */
private const val WIDGET_HOST_AUTHORITY = "appassets.androidplatform.net"
private const val WIDGET_HOST_URL = "https://$WIDGET_HOST_AUTHORITY/assets/widget-host/widget-host.html"

/** The injected JS object name the document-start bridge posts the ready token onto. */
private const val ANDROID_BRIDGE_NAME = "mewboAndroidWidgetBridge"
private const val HOST_READY_TOKEN = "ready"
private val WIDGET_ORIGIN_RULES = setOf("https://appassets.androidplatform.net")

/** Runs at document start: forwards the host page's window-level `mewbo-widget-host-ready` message
 * onto the [ANDROID_BRIDGE_NAME] channel so Android learns the host's `message` listener is live
 * (the host posts to `window`, not to this object, so it must be bridged). */
private val READY_FORWARD_SCRIPT =
    """
    window.addEventListener('message', function (e) {
      if (e && e.data && e.data.type === 'mewbo-widget-host-ready') {
        window.$ANDROID_BRIDGE_NAME.postMessage('$HOST_READY_TOKEN');
      }
    });
    """.trimIndent()

/**
 * The assist-overlay rendering of a widget: a compact, tappable summary card rather
 * than booting Pyodide inside a small floating overlay. Tapping [onOpenInApp] reuses the overlay's
 * existing expand/handoff plumbing to open the interactive widget on the chat surface.
 */
@Composable
fun WidgetSummaryCard(widget: ChatItem.Widget, onOpenInApp: () -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter)
            .clip(RoundedCornerShape(AuraShape.radiusThumb))
            .background(AuraColors.surfaceInput)
            .clickable(onClick = onOpenInApp)
            .padding(AuraSpacing.Widget.summaryPadding),
    ) {
        Text(text = "Widget", style = AuraType.chipLabel, color = AuraColors.textSecondary)
        Text(
            text = widget.summary?.takeIf { it.isNotBlank() } ?: "Interactive widget",
            style = AuraType.bodyMessage,
            color = AuraColors.textPrimary,
        )
        Text(text = "Open in the app to interact", style = AuraType.caption, color = AuraColors.accentPrimary)
    }
}
