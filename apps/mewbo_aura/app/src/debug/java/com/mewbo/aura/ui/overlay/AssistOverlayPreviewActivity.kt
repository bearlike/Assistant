package com.mewbo.aura.ui.overlay

import android.content.Intent
import android.os.Bundle
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
 * v5 (Gitea #181): mirrors `AuraSession.onShow()` - `machine.show()` then, unless the `autoListen`
 * intent extra is explicitly `false`, `machine.startListening()` right after (default ON, per the
 * spec - there's no `RECORD_AUDIO` gate to mirror here at all: [VoiceBackends][com.mewbo.aura.voice.VoiceBackends]
 * auto-selects `FakeTranscriber` on redroid, which never touches the real microphone API, so
 * auto-listen is always safe to fire). Passing `-e autoListen false` (adb) lands on the
 * non-auto-listen `Ready` state instead, for verifying that path without deleting/re-adding code.
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
        // Gitea #181 auto-listen, default ON (see class KDoc) - `-e autoListen false` opts out.
        if (intent.getBooleanExtra("autoListen", true)) {
            machine.startListening()
        }

        setContent {
            AuraTheme {
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
