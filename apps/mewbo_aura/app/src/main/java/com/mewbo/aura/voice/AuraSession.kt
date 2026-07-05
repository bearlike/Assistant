package com.mewbo.aura.voice

import android.Manifest
import android.content.Context
import android.content.Intent
import android.service.voice.VoiceInteractionSession
import android.view.View
import android.view.WindowManager
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import com.mewbo.aura.MainActivity
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.ui.overlay.AssistOverlayCallbacks
import com.mewbo.aura.ui.overlay.AssistOverlayScreen
import com.mewbo.aura.ui.theme.AuraTheme
import dagger.hilt.android.EntryPointAccessors
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first

/**
 * The orb overlay window (§5.1, §8.1): a `VoiceInteractionSession`, NOT an `Activity` - it hosts a
 * [ComposeView] in [onCreateContentView] and must supply its own [LifecycleOwner] /
 * [SavedStateRegistryOwner] on that view's tree, since there is no host `Activity` to inherit them
 * from. [AssistTurnMachine] is a plain, non-Hilt-managed class constructed here by hand (`data/`
 * singletons are reached via [AssistEntryPoint], the Hilt escape hatch for non-`@AndroidEntryPoint`
 * hosts), then driven entirely by [AssistOverlayScreen].
 *
 * v5 (Gitea #181): voice-first - [onShow] auto-starts [AssistTurnMachine.startListening] right
 * after [AssistTurnMachine.show] when `RECORD_AUDIO` is granted (see [AssistTurnMachine]'s KDoc for
 * the full turn-count/streaming contract), and [handleHandoff] is this window's own end of the
 * machine's `onHandoff` callback (fired from the SECOND turn onward, plus `continueLastSession`/
 * `expand` at any point): launch [MainActivity] on the session, then tear this window down exactly
 * like any other dismissal.
 */
class AuraSession(context: Context) :
    VoiceInteractionSession(context),
    LifecycleOwner,
    SavedStateRegistryOwner {

    private val lifecycleRegistry = LifecycleRegistry(this)
    override val lifecycle: Lifecycle get() = lifecycleRegistry

    private val savedStateRegistryController = SavedStateRegistryController.create(this)
    override val savedStateRegistry: SavedStateRegistry get() = savedStateRegistryController.savedStateRegistry

    /** Own scope, not tied to any Activity/ViewModel lifecycle - cancelled in [onDestroy]. */
    private val sessionScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private lateinit var machine: AssistTurnMachine
    private lateinit var haptics: AuraHaptics
    private lateinit var permissionChecker: DevicePermissionChecker

    override fun onCreate() {
        super.onCreate()
        savedStateRegistryController.performRestore(null)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_CREATE)

        val entryPoint = EntryPointAccessors.fromApplication(
            context.applicationContext,
            AssistEntryPoint::class.java,
        )
        haptics = entryPoint.haptics()
        permissionChecker = entryPoint.devicePermissionChecker()
        machine = AssistTurnMachine(
            transcriber = entryPoint.transcriber(),
            synthesizer = entryPoint.synthesizer(),
            sessions = entryPoint.sessionRepository().sessions,
            createSession = {
                entryPoint.sessionRepository().createSession(
                    model = entryPoint.settingsStore().selectedModel.first().ifBlank { null },
                    project = entryPoint.settingsStore().selectedProject.first().ifBlank { null },
                )
            },
            sendQuery = { id, text ->
                entryPoint.runRepository().sendQuery(
                    sessionId = id,
                    text = text,
                    model = entryPoint.settingsStore().selectedModel.first().ifBlank { null },
                    project = entryPoint.settingsStore().selectedProject.first().ifBlank { null },
                    mcpTools = null,
                    attachments = emptyList(),
                )
            },
            liveEvents = { id -> entryPoint.runRepository().live(id) },
            onHandoff = ::handleHandoff,
            scope = sessionScope,
            haptics = haptics,
            refreshSessions = { entryPoint.sessionRepository().refreshSessions() },
            onPullUp = ::handlePullUp,
        )
    }

    override fun onCreateContentView(): View {
        // W1-B's nav-bar occlusion trap, this session's real fix (quiet-box VIS retry, team-lead
        // assignment): confirmed via pixel sampling that the real VoiceInteractionSession window
        // reproduces it identically to the debug preview activity - a flat grey band
        // (RGB ~59,60,64) painted straight over AuroraEdgeGlow's bottom bloom. The earlier "no
        // equivalent API" finding was a one-level-shallow lookup:
        // VoiceInteractionSession.getWindow() returns android.app.Dialog, NOT android.view.Window
        // (confirmed via javap on the platform android.jar - VoiceInteractionSession is Dialog-
        // backed under the hood) - Kotlin's `window` property resolves to that Dialog, which has no
        // isNavigationBarContrastEnforced of its own. Dialog itself wraps a real Window reachable via
        // its OWN getWindow(), so the fix is one property-access deeper than the Activity case.
        window?.window?.isNavigationBarContrastEnforced = false
        // Gitea #181 item 4/6: decorFitsSystemWindows(false) keeps the decor from consuming insets
        // so Compose's imePadding()/systemBarsPadding() see the real values (P3, the Compose-side
        // half). The soft-input mode itself is ADJUST_NOTHING, set in onCreate BEFORE the window
        // attaches - see that call site for the measured double-shift postmortem.
        window?.window?.setDecorFitsSystemWindows(false)
        return ComposeView(context).apply {
            setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed)
            setViewTreeLifecycleOwner(this@AuraSession)
            setViewTreeSavedStateRegistryOwner(this@AuraSession)
            setContent {
                AuraTheme {
                    val uiState by machine.state.collectAsState()
                    AssistOverlayScreen(
                        state = uiState,
                        callbacks = AssistOverlayCallbacks(
                            onDismiss = ::dismissSession,
                            onSend = machine::sendText,
                            onContinueLastSession = machine::continueLastSession,
                            onCancelListening = machine::cancelListening,
                            onStartListening = ::onMicTap,
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

    override fun onShow(args: android.os.Bundle?, showFlags: Int) {
        // §7.0 t=0: fire before anything else, including super.onShow()'s own window/composition
        // work - "one crisp tick" acknowledging the gesture landed, not gated on the window
        // actually being drawn yet.
        haptics.invocation()
        super.onShow(args, showFlags)
        // Keyboard/IME on this window: the WM force-pans TYPE_VOICE_INTERACTION windows regardless
        // of app-side softInputMode (measured: ADJUST_NOTHING set both pre-attach and via live
        // attributes reassignment here never changed dumpsys' adjust=pan). Pan is therefore the
        // single, platform-owned IME mechanism - the overlay tree deliberately has NO imePadding
        // (see AssistOverlayScreen's bottom Column), because pan + imePadding double-shifted the
        // composer ~900px above the keyboard.
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_START)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
        machine.show()
        // Gitea #181 "auto-listen on trigger": nothing in-app ever calls VoiceInteractionService
        // .showSession() (confirmed in the #181 investigation), so on a real device onShow() is
        // always externally triggered - RECORD_AUDIO's OS-runtime grant (the standing sole gate,
        // #179) is what decides whether to go straight into LISTENING, same as the explicit mic-tap
        // gate below. `showFlags` (SHOW_SOURCE_ASSIST_GESTURE et al., per AOSP
        // VoiceInteractionSession) was investigated as a finer-grained trigger-source discriminator,
        // but every source wants this same auto-listen behavior here, so it stays unread - a denied
        // grant on this auto path stays SILENT (plain Ready, no error notice); the notice below is
        // reserved for an explicit tap the user can actually see fail.
        if (permissionChecker.isGranted(Manifest.permission.RECORD_AUDIO)) {
            machine.startListening()
        }
    }

    override fun onHide() {
        machine.dismiss()
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_PAUSE)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_STOP)
        super.onHide()
    }

    override fun onBackPressed() {
        dismissSession()
    }

    private fun dismissSession() {
        machine.dismiss()
        hide()
    }

    /** The overlay's mic tap (Gitea #180 P5). `RECORD_AUDIO` OS-runtime grant is the SOLE gate
     * (standing rule, no extra in-app consent layer) - but a `VoiceInteractionSession` has no
     * Activity to run the normal first-tap `requestPermissions` flow through, so if the grant is
     * already missing this can't prompt for it here; it surfaces the same quiet notice a failed
     * send already uses instead of silently doing nothing or crashing into
     * `SpeechRecognizer.startListening()`'s `SecurityException`. `FakeTranscriber` builds (debug/
     * redroid, see `AssistOverlayPreviewActivity`) don't route through this gate at all - they
     * never touch the real microphone API. */
    private fun onMicTap() {
        if (permissionChecker.isGranted(Manifest.permission.RECORD_AUDIO)) {
            machine.startListening()
        } else {
            machine.microphonePermissionUnavailable()
        }
    }

    /** [AssistTurnMachine]'s `onHandoff` callback: the query is already dispatched (or, for
     * "continue last session", there's nothing to dispatch at all) - hand the user into
     * [MainActivity] on that session and tear this window down like any other dismissal.
     * `FLAG_ACTIVITY_NEW_TASK` is required launching from a session (non-`Activity`) context;
     * `FLAG_ACTIVITY_CLEAR_TOP` reuses `MainActivity`'s existing task/instance (`singleTask` launch
     * mode, see the manifest) instead of stacking a duplicate. [modality] (Gitea #180 P1) rides
     * alongside `EXTRA_HANDOFF_SESSION_ID` as its own plain-string extra (`InputModality.name`) - a
     * client-only hint, never a wire concern - so the chat picking up this handoff's already-live
     * run knows whether it was voice-initiated. */
    private fun handleHandoff(sessionId: String, modality: InputModality) =
        launchApp(sessionId = sessionId, draft = null, modality = modality)

    /** [AssistTurnMachine.pullUpToApp]'s `onPullUp` callback (the composer pill's swipe-up gesture,
     * user directive 2026-07-04): the machine has already made the routing DECISION - a non-null
     * [sessionId] means "route to THIS invocation's session" (the same path [handleHandoff] takes),
     * a null one means "land on a fresh new chat". [draft] (blank already collapsed to null upstream)
     * is the composer's typed-but-unsent text, carried into the app for its composer to seed from. */
    private fun handlePullUp(sessionId: String?, draft: String?, modality: InputModality) =
        launchApp(sessionId = sessionId, draft = draft, modality = modality)

    /** The ONE overlay->app launch, shared by every handoff path (second-turn dispatch,
     * continue-last-session, expand, and the pull-up gesture). [sessionId] present => route
     * `MainActivity` to that session (the second-interaction path); null => `EXTRA_HANDOFF_NEW_CHAT`
     * tells the nav host to land on a fresh new chat (the pull-up-before-any-session case). [draft],
     * when non-null (pull-up only today), rides as `EXTRA_HANDOFF_DRAFT` for the in-app composer to
     * seed from - a RAW string extra (never a nav-route arg: arbitrary composer text isn't URL-safe).
     * `FLAG_ACTIVITY_NEW_TASK` is required launching from a session (non-`Activity`) context;
     * `FLAG_ACTIVITY_CLEAR_TOP` reuses `MainActivity`'s existing `singleTask` instance. */
    private fun launchApp(sessionId: String?, draft: String?, modality: InputModality) {
        val intent = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            if (sessionId != null) {
                putExtra(MainActivity.EXTRA_HANDOFF_SESSION_ID, sessionId)
            } else {
                putExtra(MainActivity.EXTRA_HANDOFF_NEW_CHAT, true)
            }
            draft?.let { putExtra(MainActivity.EXTRA_HANDOFF_DRAFT, it) }
            putExtra(MainActivity.EXTRA_HANDOFF_MODALITY, modality.name)
        }
        context.startActivity(intent)
        dismissSession()
    }

    override fun onDestroy() {
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_DESTROY)
        sessionScope.cancel()
        super.onDestroy()
    }
}
