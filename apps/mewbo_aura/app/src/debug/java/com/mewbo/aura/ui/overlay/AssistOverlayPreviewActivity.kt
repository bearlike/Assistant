package com.mewbo.aura.ui.overlay

import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import com.mewbo.aura.MainActivity
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SessionRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.ui.theme.AuraTheme
import com.mewbo.aura.voice.AssistTurnMachine
import com.mewbo.aura.voice.AuraHaptics
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.flow.first

/**
 * Debug-only host for [AssistOverlayScreen] driven by a REAL [AssistTurnMachine] - not a
 * hand-built static state like [com.mewbo.aura.ui.chat.ChatPreviewActivity], since the whole point
 * here is exercising actual state transitions. Being a normal `ComponentActivity`, it can
 * `@AndroidEntryPoint`-inject directly (unlike `AuraSession`, which needs the `EntryPoint` escape
 * hatch) and supplies its own `LifecycleOwner`/`SavedStateRegistryOwner` for free.
 *
 * v5: mirrors `AuraSession.onShow()` - `machine.show()` then, unless the `autoListen`
 * intent extra is explicitly `false`, `machine.startListening()` right after (default ON, per the
 * spec - there's no `RECORD_AUDIO` gate to mirror here at all: [VoiceBackends][com.mewbo.aura.voice.VoiceBackends]
 * auto-selects `FakeTranscriber` on redroid, which never touches the real microphone API, so
 * auto-listen is always safe to fire). Launch recipe to land on the non-auto-listen `Ready` state
 * instead, for verifying that path without deleting/re-adding code - canonical form, a genuine
 * boolean extra:
 * ```
 * adb shell am start -n com.mewbo.aura/.ui.overlay.AssistOverlayPreviewActivity --ez autoListen false
 * ```
 * `-e autoListen false` (a STRING extra) also works: the read below is string-tolerant, matching
 * `MainActivity`'s `mockBackend`/seed-extra pattern - `Bundle.getBoolean` on a String extra logs a
 * ClassCastException and silently returns its default (`true`), which is why an earlier cut of this
 * read made `-e autoListen false` a silent no-op (baseline finding, harness-only - never a product
 * bug).
 * [SessionRepository]/[RunRepository] are the REAL production repos (only the voice backends are
 * faked), so a real send here really streams/hands off against a real backend session.
 */
@AndroidEntryPoint
class AssistOverlayPreviewActivity : ComponentActivity() {

    @Inject lateinit var transcriber: Transcriber
    @Inject lateinit var synthesizer: Synthesizer
    @Inject lateinit var sessionRepository: SessionRepository
    @Inject lateinit var runRepository: RunRepository
    @Inject lateinit var settingsStore: SettingsStore
    @Inject lateinit var haptics: AuraHaptics

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        // W1-B's nav-bar occlusion trap: the system's 3-button-nav contrast scrim otherwise paints
        // an opaque grey band directly over AuroraEdgeGlow's bottom bloom (where the D-3 clay
        // ignition stop and the docked orb both live) - see LivenessShowcaseActivity's identical fix.
        window.isNavigationBarContrastEnforced = false
        // Parity with AuraSession.onShow's keep-screen-on fix - a real Activity window tears its own
        // flags down with the window on finish()/onDestroy, so no explicit clear is needed here.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val machine = AssistTurnMachine(
            transcriber = transcriber,
            synthesizer = synthesizer,
            sessions = sessionRepository.sessions,
            createSession = {
                sessionRepository.createSession(
                    model = settingsStore.selectedModel.first().ifBlank { null },
                    project = settingsStore.selectedProject.first().ifBlank { null },
                )
            },
            sendQuery = { id, text ->
                runRepository.sendQuery(
                    sessionId = id,
                    text = text,
                    model = settingsStore.selectedModel.first().ifBlank { null },
                    project = settingsStore.selectedProject.first().ifBlank { null },
                    mcpTools = null,
                    attachments = emptyList(),
                )
            },
            liveEvents = { id -> runRepository.live(id) },
            onHandoff = { sessionId, modality ->
                startActivity(
                    Intent(this, MainActivity::class.java)
                        .putExtra(MainActivity.EXTRA_HANDOFF_SESSION_ID, sessionId)
                        .putExtra(MainActivity.EXTRA_HANDOFF_MODALITY, modality.name),
                )
                finish()
            },
            scope = lifecycleScope,
            haptics = haptics,
            refreshSessions = { sessionRepository.refreshSessions() },
            // Pull-up (composer-pill swipe-up) handoff, mirroring onHandoff above but with the
            // session-vs-new-chat + draft extras AuraSession.launchApp sets (user directive 2026-07-04).
            onPullUp = { sessionId, draft, modality ->
                startActivity(
                    Intent(this, MainActivity::class.java).apply {
                        if (sessionId != null) {
                            putExtra(MainActivity.EXTRA_HANDOFF_SESSION_ID, sessionId)
                        } else {
                            putExtra(MainActivity.EXTRA_HANDOFF_NEW_CHAT, true)
                        }
                        draft?.let { putExtra(MainActivity.EXTRA_HANDOFF_DRAFT, it) }
                        putExtra(MainActivity.EXTRA_HANDOFF_MODALITY, modality.name)
                    },
                )
                finish()
            },
        )
        haptics.invocation() // §7.0 t=0, mirroring AuraSession.onShow's real invocation timing.
        machine.show()
        // auto-listen, default ON (see class KDoc) - `--ez autoListen false` opts out.
        // adb `-e` extras arrive as STRINGS - Bundle.getBoolean logs a ClassCastException and
        // returns the default (true), so `-e autoListen false` silently never worked. Same
        // string-tolerant read MainActivity's seed extras use.
        val autoListen = when (val raw = intent.extras?.get("autoListen")) {
            is Boolean -> raw
            is String -> !raw.equals("false", ignoreCase = true)
            else -> true
        }
        if (autoListen) {
            machine.startListening()
        }

        setContent {
            // [R4 2026-07-10] This host called AuraTheme() bare (defaulted reducedMotion = false),
            // silently dropping the in-app SettingsStore toggle - the OS "Remove animations" signal
            // still applied (it's OR-ed inside AuraTheme itself), but a user who only set the
            // in-app toggle saw no effect here. Same fix AuraSession.onCreateContentView() already
            // applies for the real session host - this activity can collect SettingsStore directly
            // (it's @AndroidEntryPoint-injected, no EntryPoint escape hatch needed).
            val reducedMotion by settingsStore.reducedMotion.collectAsState(initial = false)
            AuraTheme(reducedMotion = reducedMotion) {
                val state by machine.state.collectAsState()
                AssistOverlayScreen(
                    state = state,
                    callbacks = AssistOverlayCallbacks(
                        onDismiss = { machine.dismiss(); finish() },
                        onSend = machine::sendText,
                        onContinueLastSession = machine::continueLastSession,
                        onCancelListening = machine::cancelListening,
                        // No RECORD_AUDIO gate here (unlike AuraSession.onMicTap): VoiceBackends
                        // auto-selects FakeTranscriber on redroid, which never touches the real
                        // microphone API.
                        onStartListening = machine::startListening,
                        onStopStreaming = machine::stopStreaming,
                        onToggleSpeak = machine::toggleSpeak,
                        onExpand = machine::expand,
                        onPullUpToApp = machine::pullUpToApp,
                    ),
                )
            }
        }
    }
}
