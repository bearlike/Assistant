package com.mewbo.aura.notify

import android.content.Context
import com.mewbo.aura.data.repo.RunNotifications
import com.mewbo.aura.ui.control.DeviceControlOverlay
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * The concrete [RunNotifications] the data layer calls when a run starts. Three jobs, all because a
 * run start is the ONE moment that matters for this feature:
 *
 * 1. Start [RunNotificationService] so the completion is observed even if the app is backgrounded
 *    next. Started HERE (holding an application `Context`) rather than in `RunRepository`, keeping the
 *    data layer free of `Context`/service/`MainActivity` knowledge — the same seam shape
 *    `DeviceModule` uses for `AppForegroundChecker`/`DeviceToolResultReporter`.
 * 2. Emit on [runStarted] so `MainActivity` can request `POST_NOTIFICATIONS` at first relevance (the
 *    first query send), with no in-app pre-consent dialog — the OS grant is the sole gate.
 * 3. Tell [DeviceControlOverlay] which session to narrate. **A grant is app-wide and carries no
 *    session**, so the only way the on-screen surface can know whose events to draw is to be told,
 *    and run start is the one moment that knows — strictly BEFORE the grant exists, since the model
 *    calls `device_control_start` several steps into the run.
 *
 * `replay = 1` so a run that started before `MainActivity` began observing (e.g. one kicked off from
 * the assist overlay) still triggers the permission request when the app opens.
 */
@Singleton
class RunNotificationLauncher @Inject constructor(
    @ApplicationContext private val context: Context,
    private val controlOverlay: DeviceControlOverlay,
) : RunNotifications {

    private val _runStarted = MutableSharedFlow<Unit>(replay = 1, extraBufferCapacity = 1)
    val runStarted: SharedFlow<Unit> = _runStarted.asSharedFlow()

    override fun onRunStarted(sessionId: String, preview: String?, deviceControl: Boolean) {
        _runStarted.tryEmit(Unit)
        // Told on EVERY run, not only a device-control one. `deviceControl` is the arming
        // predicate evaluated at run start, and a grant can be taken part-way through a run that
        // did not look like one — the model calls start, is refused, the user fixes Shizuku, the
        // model retries. Naming the session costs nothing and raises no window; the grant alone
        // does that. Gating it on the flag would leave exactly those late grants narrating
        // nothing, which is the silent half-feature this seam exists to avoid.
        controlOverlay.follow(sessionId)
        try {
            context.startForegroundService(
                RunNotificationService.intent(context, sessionId, preview, deviceControl),
            )
        } catch (e: IllegalStateException) {
            // Android 12+ forbids starting a foreground service from the background. A run started
            // from the app or a showing overlay is foreground-enough to be allowed; a rare edge (e.g.
            // an overlay teardown racing the start) can still land here. Degrade silently — the run
            // completes server-side and the user sees it on reopen; a missed background alert is not
            // worth a crash. ForegroundServiceStartNotAllowedException (API 31+) is an IllegalStateException.
        }
    }
}
