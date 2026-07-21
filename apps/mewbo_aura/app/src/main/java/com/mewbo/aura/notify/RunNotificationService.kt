package com.mewbo.aura.notify

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import dagger.hilt.android.AndroidEntryPoint
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Foreground service that keeps the process alive while a backgrounded run finishes, so its
 * completion can be observed and announced. Deliberately thin: all watch/notify LOGIC is
 * [RunNotificationController]'s; this class owns only the Android lifecycle — `startForeground`, the
 * per-session watch jobs, and self-reaping when the last one ends.
 *
 * **`dataSync` foreground-service type.** The work is exactly "follow a network operation to
 * completion", which is what `dataSync` describes; `shortService` (the tempting no-permission
 * alternative) caps at ~3 minutes and would kill a genuinely long agentic turn — the whole case this
 * feature exists for. `dataSync` costs one install-time permission (`FOREGROUND_SERVICE_DATA_SYNC`)
 * and stays well under the platform's cumulative runtime cap because each watch stops the instant its
 * run ends.
 *
 * **Must be started while the app is foreground.** [RunNotificationLauncher] fires this the moment a
 * run STARTS (from `RunRepository`), i.e. while the user is still on screen, because Android 12+
 * forbids starting a foreground service from the background — start it at send time and it legally
 * survives the app being backgrounded, which is the point.
 */
@AndroidEntryPoint
class RunNotificationService : Service() {

    @Inject lateinit var controller: RunNotificationController
    @Inject lateinit var notifier: RunNotifier

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    /** sessionId → its watch job. One watch per session; a duplicate start for a session already
     * being watched is ignored. Guards self-reaping — the service stops only when this empties. */
    private val watches = ConcurrentHashMap<String, Job>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val sessionId = intent?.getStringExtra(EXTRA_SESSION_ID)?.takeIf { it.isNotBlank() }
        if (sessionId == null) {
            stopIfIdle()
            return Service.START_NOT_STICKY
        }
        val label = intent.getStringExtra(EXTRA_LABEL).orEmpty()

        // Re-asserting foreground on every start keeps us inside the 5s startForegroundService
        // deadline and refreshes the ongoing notification; the type must match the manifest.
        notifier.ensureChannels()
        startForeground(
            RunNotifier.ONGOING_NOTIFICATION_ID,
            notifier.buildOngoing(label),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )

        // One watch per session — a re-send for a session already watched is a no-op (onStartCommand
        // is single-threaded on the main thread, so this check-then-put needs no extra locking). The
        // job is started LAZILY and only after it is in the map, so a fast-completing run's `finally`
        // (which removes it) can never fire before the entry exists and strand a reap.
        if (!watches.containsKey(sessionId)) {
            val job = scope.launch(start = CoroutineStart.LAZY) {
                try {
                    withTimeoutOrNull(MAX_WATCH_DURATION_MS) { controller.watchAndNotify(sessionId, label) }
                } finally {
                    onWatchEnded(sessionId)
                }
            }
            watches[sessionId] = job
            job.start()
        }
        return Service.START_NOT_STICKY
    }

    private fun onWatchEnded(sessionId: String) {
        watches.remove(sessionId)
        stopIfIdle()
    }

    @Synchronized
    private fun stopIfIdle() {
        if (watches.isEmpty()) {
            stopForeground(Service.STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val EXTRA_SESSION_ID = "com.mewbo.aura.notify.SESSION_ID"
        private const val EXTRA_LABEL = "com.mewbo.aura.notify.LABEL"

        /** Belt-and-suspenders reap: the watch normally ends promptly on `completion`/`stream_end`,
         * but a run that emits neither (a wedged stream past the server's own idle close) must not pin
         * a foreground service forever. Generous — above any realistic turn, far under the platform's
         * `dataSync` runtime cap. Hitting it drops the notification but frees the service; the run
         * continues server-side and the user sees it on reopen. */
        private const val MAX_WATCH_DURATION_MS = 20L * 60L * 1000L

        fun intent(context: Context, sessionId: String, label: String?): Intent =
            Intent(context, RunNotificationService::class.java)
                .putExtra(EXTRA_SESSION_ID, sessionId)
                .putExtra(EXTRA_LABEL, label.orEmpty())
    }
}
