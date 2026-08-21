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
import com.mewbo.aura.data.device.AssistOverlayPresence
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.settings.SettingsStore
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
 * v5: voice-first - [onShow] auto-starts [AssistTurnMachine.startListening] right
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
    private lateinit var settingsStore: SettingsStore

    /** This window's own visibility, published to the `device_*` activity-launch guard - see
     * [AssistOverlayPresence]. Set in [onShow], cleared in BOTH [onHide] and [onDestroy] (a session
     * destroyed without a preceding hide must not leave the flag stuck true). */
    private lateinit var overlayPresence: AssistOverlayPresence

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
        settingsStore = entryPoint.settingsStore()
        overlayPresence = entryPoint.assistOverlayPresence()
        machine = AssistTurnMachine(
            transcriber = entryPoint.transcriber(),
            synthesizer = entryPoint.synthesizer(),
            sessions = entryPoint.sessionRepository().sessions,
            // the overlay resolves its OWN default model here (its
            // session-creation seam), independent of the app's SettingsStore.selectedModel. Empty =
            // server default, the pre-F3 behavior. Both turn-one createSession and any turn-two
            // sendQuery read the SAME overlayDefaultModel so the session's context.model is
            // consistent; on handoff MainActivity's bind() then hydrates that model from the
            // session's persisted context, so the overlay's choice carries into the app for free.
            createSession = {
                entryPoint.sessionRepository().createSession(
                    model = entryPoint.settingsStore().overlayDefaultModel.first().ifBlank { null },
                    project = entryPoint.settingsStore().selectedProject.first().ifBlank { null },
                )
            },
            sendQuery = { id, text ->
                entryPoint.runRepository().sendQuery(
                    sessionId = id,
                    text = text,
                    model = entryPoint.settingsStore().overlayDefaultModel.first().ifBlank { null },
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
        // That this flag is reachable HERE and nowhere else on this app's surfaces is a platform
        // fact, not an oversight. DisplayPolicy admits a window as a navigation-bar appearance
        // candidate only when it is a fullscreen app window OR TYPE_VOICE_INTERACTION, so this
        // session qualifies while the device-control glow (TYPE_APPLICATION_OVERLAY) is excluded
        // outright and has no equivalent lever - see ui/control/. It is also the only system-bar
        // appearance setter left undeprecated at compileSdk 37; every colour setter beside it is
        // deprecated, so do not read the neighbours below as live API.
        // decorFitsSystemWindows(false) keeps the decor from consuming insets
        // so Compose's systemBarsPadding() sees the real values (P3, the Compose-side half). The
        // soft-input mode is left at the platform DEFAULT - there is NO setSoftInputMode call
        // anywhere: the WM force-pans TYPE_VOICE_INTERACTION windows regardless (measured), so the
        // overlay tree deliberately carries NO imePadding - see AssistOverlayScreen's bottom Column
        // (and ui/overlay/CLAUDE.md's IME section) for the measured double-shift postmortem.
        // Deprecated AND documented as disabled from Android 15 for a targetSdk-35+ app, so on a
        // current device this call does nothing. It stays because minSdk is 33, where it still does
        // the work; on 15+ the enforced edge-to-edge regime produces the same result without it.
        // A "remove deprecated APIs" pass that deletes it silently regresses API 33/34 only.
        window?.window?.setDecorFitsSystemWindows(false)
        return ComposeView(context).apply {
            setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed)
            setViewTreeLifecycleOwner(this@AuraSession)
            setViewTreeSavedStateRegistryOwner(this@AuraSession)
            setContent {
                // Reduced-motion setting must thread through explicitly - a bare AuraTheme() call
                // here would silently default to false for every overlay invocation regardless of
                // the stored setting. initial=false keeps the pre-load frame from ever blocking
                // show while the real value is still loading.
                val reducedMotion by settingsStore.reducedMotion.collectAsState(initial = false)
                AuraTheme(reducedMotion = reducedMotion) {
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
        // From here until onHide/onDestroy the app HAS a visible, user-initiated window, so the
        // `device_*` clock tools (set alarm/timer, dismiss alarm) ARE allowed to launch the clock app
        // - Android grants a BAL exemption for any visible system window, TYPE_VOICE_INTERACTION
        // included. Their process-importance-only guard refused it anyway: showing this session makes
        // the process IMPORTANCE_FOREGROUND_SERVICE (125), not IMPORTANCE_FOREGROUND (100), so the
        // `<= 100` test read "backgrounded" (AOSP-confirmed - full chain in `canStartActivityNow`).
        // Set BEFORE machine.startListening() below: this turn's own device tool calls can land while
        // this window is still the only thing on screen.
        overlayPresence.visible = true
        super.onShow(args, showFlags)
        // A response can take a while to stream; without this the screen sleeps mid-response and
        // kills the overlay experience. FLAG_KEEP_SCREEN_ON needs no permission (unlike a WAKE_LOCK)
        // - it only keeps the screen on while THIS window is showing, cleared in onHide/onDestroy
        // below so the overlay never holds the screen awake after it's gone. window?.window is the
        // same Dialog-wrapped real Window the nav-bar-contrast/decorFitsSystemWindows fixes above
        // reach (VoiceInteractionSession.getWindow() returns Dialog, not Window) - already valid here
        // since onCreateContentView() runs before the first onShow, and the session (and its window)
        // survives every later hide->show cycle (voice/CLAUDE.md's session-reuse lifecycle law).
        window?.window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        // Keyboard/IME on this window: the WM force-pans TYPE_VOICE_INTERACTION windows regardless
        // of app-side softInputMode (measured: ADJUST_NOTHING set both pre-attach and via live
        // attributes reassignment here never changed dumpsys' adjust=pan). Pan is therefore the
        // single, platform-owned IME mechanism - the overlay tree deliberately has NO imePadding
        // (see AssistOverlayScreen's bottom Column), because pan + imePadding double-shifted the
        // composer ~900px above the keyboard.
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_START)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
        machine.show()
        // "auto-listen on trigger": nothing in-app ever calls VoiceInteractionService
        // .showSession() (device-confirmed), so on a real device onShow() is
        // always externally triggered - RECORD_AUDIO's OS-runtime grant (the standing sole
        // gate) is what decides whether to go straight into LISTENING, same as the explicit mic-tap
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
        overlayPresence.visible = false
        // Pairs with the addFlags in onShow - the screen must go back to normal sleep behavior the
        // moment the overlay is gone, not stay pinned awake by a window nobody can see.
        window?.window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
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

    /** The overlay's mic tap. `RECORD_AUDIO` OS-runtime grant is the SOLE gate
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
     * mode, see the manifest) instead of stacking a duplicate. [modality] rides
     * alongside `EXTRA_HANDOFF_SESSION_ID` as its own plain-string extra (`InputModality.name`) - a
     * client-only hint, never a wire concern - so the chat picking up this handoff's already-live
     * run knows whether it was voice-initiated. */
    private fun handleHandoff(sessionId: String, modality: InputModality) =
        launchApp(sessionId = sessionId, draft = null, modality = modality)

    /** [AssistTurnMachine.pullUpToApp]'s `onPullUp` callback (the composer pill's swipe-up gesture,
     * user directive): the machine has already made the routing DECISION - a non-null
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
        // Cleared here as well as in onHide (the app's only two teardown paths - every dismissal
        // funnels through dismissSession() -> hide() -> onHide, and finish() is never called): a
        // session torn down without a preceding hide would otherwise leave the flag stuck true
        // forever, and the launch guard would then wave through a genuinely backgrounded
        // startActivity - which Android does NOT reject, it DISCARDS it and still reports
        // START_SUCCESS, so a handler would report `handed_to_clock_app: true` for an alarm that
        // never reached the screen. Always err toward false (AssistOverlayPresence's own KDoc).
        overlayPresence.visible = false
        // Same "destroyed without a preceding hide" belt-and-suspenders as overlayPresence.visible
        // above - clearFlags is a no-op if onHide already cleared it.
        window?.window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_DESTROY)
        sessionScope.cancel()
        super.onDestroy()
    }
}
