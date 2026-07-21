package com.mewbo.aura

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.ViewTreeObserver
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.lifecycleScope
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.mock.MockBackendFlags
import com.mewbo.aura.notify.RunNotificationLauncher
import com.mewbo.aura.ui.navigation.AuraNavHost
import com.mewbo.aura.ui.navigation.IS_DEBUG_BUILD
import com.mewbo.aura.ui.theme.AuraTheme
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.launch

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject lateinit var settingsStore: SettingsStore
    @Inject lateinit var mockBackendFlags: MockBackendFlags

    /** Emits when a run starts (see [RunNotificationLauncher.runStarted]) so the POST_NOTIFICATIONS
     * request can fire at first relevance — the first query send — rather than on cold launch. */
    @Inject lateinit var runNotificationLauncher: RunNotificationLauncher

    /**
     * POST_NOTIFICATIONS (API 33+) runtime request. Registered as a field so it exists before the
     * activity is STARTED (the framework requirement). The result is intentionally ignored: the OS
     * grant is the SOLE consent gate (hard user directive — no in-app pre-consent dialog, no nagging
     * on denial; a denial just means no completion notifications).
     */
    private val requestNotificationsPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* OS grant is the sole gate */ }

    /** One-shot guard so the notification permission is requested at most once per process. */
    private var askedForNotifications = false

    /**
     * Flips once the first real reduced-motion value loads from DataStore. Read by the pre-draw
     * listener below, not by Compose - a view-system callback can't observe Compose state, so this
     * stays a plain field, set from a [SideEffect].
     */
    @Volatile private var settingsLoaded = false

    /** Session id carried by the assist overlay's handoff intent ([EXTRA_HANDOFF_SESSION_ID]) -
     * read on cold start ([onCreate]) and again on a warm one ([onNewIntent]), consumed once by
     * [AuraNavHost]'s own navigation effect (its `onHandoffConsumed` clears this back to null so a
     * later recomposition doesn't re-navigate). A plain mutable-state field, not a `ViewModel`
     * property: `AuraNavHost` is the only reader and this Activity is the only writer. */
    private var pendingHandoffSessionId by mutableStateOf<String?>(null)

    /** Paired with [pendingHandoffSessionId] - the raw `EXTRA_HANDOFF_MODALITY`
     * string (an `InputModality.name`, e.g. "Voice"/"Text"). Kept as a plain `String` rather than
     * `com.mewbo.aura.voice.InputModality` here: this Activity and `AuraNavHost` are the `ui`/app-root
     * layer, which never imports `voice/` (apps/mewbo_aura/CLAUDE.md package layering) - only
     * `ChatViewModel.bind` (a legal `voice/` consumer) parses it. Read/cleared in lockstep with
     * [pendingHandoffSessionId] since both come off the SAME handoff intent. */
    private var pendingHandoffModality by mutableStateOf<String?>(null)

    /** The composer draft carried by a pull-up handoff ([EXTRA_HANDOFF_DRAFT], user directive
     * 2026-07-04) - the overlay's typed-but-unsent text, for the in-app composer to seed from. A RAW
     * string extra (never a nav-route arg - arbitrary text isn't URL-safe). Read/cleared in lockstep
     * with [pendingHandoffSessionId]. */
    private var pendingHandoffDraft by mutableStateOf<String?>(null)

    /** Set by a pull-up handoff that had NO session yet ([EXTRA_HANDOFF_NEW_CHAT], user directive
     * 2026-07-04): tells [AuraNavHost] to land on a fresh new chat rather than staying on whatever
     * (possibly stale) session a warm `MainActivity` last showed. The session-present pull-up and
     * every ordinary handoff leave this false. */
    private var pendingHandoffNewChat by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingHandoffSessionId = intent.getStringExtra(EXTRA_HANDOFF_SESSION_ID)
        pendingHandoffModality = intent.getStringExtra(EXTRA_HANDOFF_MODALITY)
        pendingHandoffDraft = intent.getStringExtra(EXTRA_HANDOFF_DRAFT)
        pendingHandoffNewChat = intent.getBooleanExtra(EXTRA_HANDOFF_NEW_CHAT, false)
        // Debug-only deterministic credential seeding: the shared dev device (redroid) loses
        // DataStore whenever the container/data is recreated, and every agent re-typing the API
        // key through the Settings UI is slow and error-prone (recurring-401 class). One command
        // seeds both:  adb shell am start -n com.mewbo.aura/.MainActivity \
        //   -e seedBaseUrl http://api:5125 -e seedApiKey <token>
        if (IS_DEBUG_BUILD) {
            val seedBaseUrl = intent.getStringExtra("seedBaseUrl")
            val seedApiKey = intent.getStringExtra("seedApiKey")
            if (seedBaseUrl != null || seedApiKey != null) {
                lifecycleScope.launch {
                    seedBaseUrl?.let { settingsStore.setBaseUrl(it) }
                    seedApiKey?.let { settingsStore.setApiKey(it) }
                }
            }
            // Same seed-extra pattern, for the mock backend toggle:
            //   adb shell am start -n com.mewbo.aura/.MainActivity -e mockBackend true
            // `hasExtra` guards this so a normal launch (no extra passed at all) never touches the
            // toggle - only an EXPLICIT true/false flips it, leaving whatever the Settings row last
            // set untouched otherwise. Read STRING-tolerantly: adb's `-e` passes a String extra,
            // and getBooleanExtra silently returns the default for a String - the first cut read
            // it as boolean-only, so the documented `-e mockBackend true` command actively set the
            // toggle to FALSE (caught live: a "mock" E2E run answered with a real LLM reply).
            // `--ez` (a genuine boolean extra) is also accepted.
            if (intent.hasExtra("mockBackend")) {
                val enabled = when (val raw = intent.extras?.get("mockBackend")) {
                    is Boolean -> raw
                    is String -> raw.equals("true", ignoreCase = true)
                    else -> false
                }
                lifecycleScope.launch { mockBackendFlags.setEnabled(enabled) }
            }
        }
        // Request POST_NOTIFICATIONS at first relevance — the first query send — via
        // RunNotificationLauncher.runStarted (replay=1 so an overlay-started run before the app was
        // open still counts on launch). No pre-consent dialog: the OS prompt is the only gate.
        lifecycleScope.launch {
            runNotificationLauncher.runStarted.collect { maybeRequestNotificationsPermission() }
        }
        // Holds the platform cold-start splash (native since API 31; minSdk here is 33) past its
        // first frame until settingsLoaded flips - the same mechanism
        // androidx.core:core-splashscreen's setKeepOnScreenCondition uses internally, done
        // directly since that library isn't in the dependency catalog. Without this, the first
        // composed frame would use collectAsState's `initial` fallback (motion on) instead of the
        // saved preference, flashing full-motion before DataStore responds.
        val contentView: View = findViewById(android.R.id.content)
        contentView.viewTreeObserver.addOnPreDrawListener(
            object : ViewTreeObserver.OnPreDrawListener {
                override fun onPreDraw(): Boolean {
                    if (!settingsLoaded) return false
                    contentView.viewTreeObserver.removeOnPreDrawListener(this)
                    return true
                }
            },
        )
        // Frameless/full-bleed (user directive, reference parity): FORCE dark
        // transparent system bars instead of enableEdgeToEdge()'s auto style - auto follows the
        // SYSTEM light/dark theme, so a light-themed device got a light (white) navigation-bar
        // scrim under this dark-only app: a visible white frame at the bottom. Dark+transparent on
        // both bars lets the canvas/aurora run edge-to-edge; the contrast-enforcement flag would
        // otherwise re-add an opaque scrim on 3-button-nav devices (same trap the overlay hit -
        // voice/CLAUDE.md).
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        window.isNavigationBarContrastEnforced = false
        setContent {
            // Top-level collectAsState (§8.4): reduced-motion drives AuraTheme directly, no
            // intermediate ViewModel needed for one DataStore-backed flow. `initial` is null (not
            // a hardcoded default) so the pre-draw listener above can tell "not loaded yet" apart
            // from a real value.
            val reducedMotion: Boolean? by settingsStore.reducedMotion.collectAsState(initial = null)
            // Delegated properties don't smart-cast - copy to a plain local for the null check.
            val loadedReducedMotion = reducedMotion

            if (loadedReducedMotion != null) {
                SideEffect { settingsLoaded = true }
                AuraTheme(reducedMotion = loadedReducedMotion) {
                    AuraNavHost(
                        pendingHandoffSessionId = pendingHandoffSessionId,
                        pendingHandoffModality = pendingHandoffModality,
                        pendingHandoffDraft = pendingHandoffDraft,
                        pendingHandoffNewChat = pendingHandoffNewChat,
                        onHandoffConsumed = {
                            pendingHandoffSessionId = null
                            pendingHandoffModality = null
                            pendingHandoffDraft = null
                            pendingHandoffNewChat = false
                        },
                    )
                }
            }
        }
    }

    /** Warm-start counterpart to [onCreate]'s read - `MainActivity` is `singleTask` (manifest) so
     * a second assist-overlay handoff while already running redelivers here instead of creating a
     * new instance. */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        pendingHandoffSessionId = intent.getStringExtra(EXTRA_HANDOFF_SESSION_ID)
        pendingHandoffModality = intent.getStringExtra(EXTRA_HANDOFF_MODALITY)
        pendingHandoffDraft = intent.getStringExtra(EXTRA_HANDOFF_DRAFT)
        pendingHandoffNewChat = intent.getBooleanExtra(EXTRA_HANDOFF_NEW_CHAT, false)
    }

    /** Fires the system POST_NOTIFICATIONS prompt once, only when it can matter: API 33+, not yet
     * asked this process, not already granted. Below API 33 the permission is auto-granted, so there
     * is nothing to request. */
    private fun maybeRequestNotificationsPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (askedForNotifications) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        askedForNotifications = true
        requestNotificationsPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    companion object {
        const val EXTRA_HANDOFF_SESSION_ID = "com.mewbo.aura.EXTRA_HANDOFF_SESSION_ID"
        const val EXTRA_HANDOFF_MODALITY = "com.mewbo.aura.EXTRA_HANDOFF_MODALITY"
        const val EXTRA_HANDOFF_DRAFT = "com.mewbo.aura.EXTRA_HANDOFF_DRAFT"
        const val EXTRA_HANDOFF_NEW_CHAT = "com.mewbo.aura.EXTRA_HANDOFF_NEW_CHAT"
    }
}
