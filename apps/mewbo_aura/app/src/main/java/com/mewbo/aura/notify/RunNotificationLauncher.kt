package com.mewbo.aura.notify

import android.content.Context
import com.mewbo.aura.data.repo.RunNotifications
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * The concrete [RunNotifications] the data layer calls when a run starts. Two jobs, both because a
 * run start is the ONE moment that matters for this feature:
 *
 * 1. Start [RunNotificationService] so the completion is observed even if the app is backgrounded
 *    next. Started HERE (holding an application `Context`) rather than in `RunRepository`, keeping the
 *    data layer free of `Context`/service/`MainActivity` knowledge — the same seam shape
 *    `DeviceModule` uses for `AppForegroundChecker`/`DeviceToolResultReporter`.
 * 2. Emit on [runStarted] so `MainActivity` can request `POST_NOTIFICATIONS` at first relevance (the
 *    first query send), with no in-app pre-consent dialog — the OS grant is the sole gate.
 *
 * `replay = 1` so a run that started before `MainActivity` began observing (e.g. one kicked off from
 * the assist overlay) still triggers the permission request when the app opens.
 */
@Singleton
class RunNotificationLauncher @Inject constructor(
    @ApplicationContext private val context: Context,
) : RunNotifications {

    private val _runStarted = MutableSharedFlow<Unit>(replay = 1, extraBufferCapacity = 1)
    val runStarted: SharedFlow<Unit> = _runStarted.asSharedFlow()

    override fun onRunStarted(sessionId: String, preview: String?) {
        _runStarted.tryEmit(Unit)
        try {
            context.startForegroundService(RunNotificationService.intent(context, sessionId, preview))
        } catch (e: IllegalStateException) {
            // Android 12+ forbids starting a foreground service from the background. A run started
            // from the app or a showing overlay is foreground-enough to be allowed; a rare edge (e.g.
            // an overlay teardown racing the start) can still land here. Degrade silently — the run
            // completes server-side and the user sees it on reopen; a missed background alert is not
            // worth a crash. ForegroundServiceStartNotAllowedException (API 31+) is an IllegalStateException.
        }
    }
}
