package com.mewbo.aura.notify

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import com.mewbo.aura.MainActivity
import com.mewbo.aura.R
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * The ONE place a `Notification` is built or posted for this feature — an atomic wrapper over the
 * platform [NotificationManager] (no `androidx.core` dependency: `NotificationChannel` and
 * `Notification.Builder` are the platform APIs, and `minSdk 33` is well past the API-26 channel
 * floor, so the compat shims buy nothing). [RunNotificationService] owns the ongoing notification's
 * lifecycle (it must be the one passed to `startForeground`); [RunNotificationController] posts the
 * completion. Both go through here so channel setup and copy live in exactly one file.
 */
@Singleton
class RunNotifier @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val manager: NotificationManager =
        context.getSystemService(NotificationManager::class.java)

    /** Idempotent — `createNotificationChannel` re-creates an existing channel in place, so calling
     * this on every service start (before `startForeground`) is free and needs no "created?" flag. */
    fun ensureChannels() {
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ONGOING, "Working", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while Mewbo is completing a request in the background."
                setShowBadge(false)
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_COMPLETION, "Finished", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Alerts you when a Mewbo request finishes while the app is in the background."
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_QUESTION, "Questions", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Alerts you when Mewbo is waiting on your answer to continue."
            },
        )
    }

    /**
     * The low-importance ongoing notification a run's foreground service must display. Ongoing +
     * silent (`IMPORTANCE_LOW`, no vibration/sound): honest disclosure that a background task is
     * running, without competing with the completion alert. [label] (the query text) is shown when
     * present so the shade entry is identifiable.
     */
    fun buildOngoing(label: String): Notification =
        Notification.Builder(context, CHANNEL_ONGOING)
            .setSmallIcon(R.drawable.ic_launcher_monochrome)
            .setContentTitle("Mewbo is working…")
            .apply { if (label.isNotBlank()) setContentText(label) }
            .setOngoing(true)
            .setShowWhen(false)
            .build()

    /**
     * Posts the high-visibility completion alert, tagged by [sessionId] so concurrent sessions never
     * overwrite each other (the tag+id pair is the notification identity, and a fixed id keeps it off
     * the ongoing notification). Tapping it opens the finished session by reusing the EXISTING
     * assist-overlay handoff extra ([MainActivity.EXTRA_HANDOFF_SESSION_ID]) — `MainActivity` already
     * routes that to the session via `AuraNavHost`, so no new navigation code is needed.
     */
    fun postCompletion(sessionId: String, label: String, notice: CompletionNotice) {
        val (title, fallback) = when (notice) {
            CompletionNotice.Done -> "Mewbo finished" to "Your session is ready."
            is CompletionNotice.Failed -> "Mewbo couldn't finish" to (notice.reason?.takeIf { it.isNotBlank() } ?: "The request didn't complete.")
        }
        val notification = Notification.Builder(context, CHANNEL_COMPLETION)
            .setSmallIcon(R.drawable.ic_launcher_monochrome)
            .setContentTitle(title)
            .setContentText(label.ifBlank { fallback })
            .setAutoCancel(true)
            .setContentIntent(openSessionIntent(sessionId))
            .build()
        manager.notify(sessionId, COMPLETION_NOTIFICATION_ID, notification)
    }

    /**
     * Posts the high-visibility "a question is waiting" alert while a run BLOCKS on an
     * `ask_user_question` tool call ([RunNotificationController]). [body] is the first question's text
     * (built by the controller). Tapping opens the session — the SAME assist-overlay handoff extra as
     * [postCompletion] — where the interactive question card is answered. Tagged by [sessionId] with
     * its OWN fixed id so it never collides with the completion alert; `setAutoCancel` so a tap (the
     * usual way to reach the card) clears it, and [cancelQuestion] clears it when the answer lands
     * while the watch is still alive.
     */
    fun postQuestion(sessionId: String, body: String) {
        val notification = Notification.Builder(context, CHANNEL_QUESTION)
            .setSmallIcon(R.drawable.ic_launcher_monochrome)
            .setContentTitle("Mewbo is asking you a question")
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(openSessionIntent(sessionId))
            .build()
        manager.notify(sessionId, QUESTION_NOTIFICATION_ID, notification)
    }

    /** Clears a [postQuestion] alert once the question resolves (answered here or elsewhere) or the
     * run ends. Idempotent — cancelling an absent notification is a no-op. */
    fun cancelQuestion(sessionId: String) {
        manager.cancel(sessionId, QUESTION_NOTIFICATION_ID)
    }

    private fun openSessionIntent(sessionId: String): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_HANDOFF_SESSION_ID, sessionId)
        return PendingIntent.getActivity(
            context,
            sessionId.hashCode(),
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    companion object {
        private const val CHANNEL_ONGOING = "mewbo_run_ongoing"
        private const val CHANNEL_COMPLETION = "mewbo_run_completion"
        private const val CHANNEL_QUESTION = "mewbo_run_question"

        /** The ongoing notification id, passed to `startForeground`. Fixed: one per service, not per
         * session — a single "Mewbo is working…" entry covers however many runs are being watched. */
        const val ONGOING_NOTIFICATION_ID = 1

        /** Fixed id for every completion; uniqueness comes from the per-session TAG in [postCompletion]
         * (`notify(tag, id, …)`), so two sessions finishing produce two distinct notifications and
         * neither collides with [ONGOING_NOTIFICATION_ID]. */
        private const val COMPLETION_NOTIFICATION_ID = 2

        /** Fixed id for every pending-question alert ([postQuestion]/[cancelQuestion]); per-session TAG
         * gives uniqueness. Distinct from [COMPLETION_NOTIFICATION_ID] so a session's question and its
         * later completion coexist and [cancelQuestion] targets only the question. */
        private const val QUESTION_NOTIFICATION_ID = 3
    }
}
