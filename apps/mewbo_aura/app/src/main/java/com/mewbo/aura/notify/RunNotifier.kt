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
 *
 * It is also the one place that knows HOW this app opens a session ([sessionIntent]) — every tap
 * target here plus the tapless return in [openSession] resolve to that single intent.
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
     *
     * Tapping it opens [sessionId] through the SAME handoff intent [postCompletion]/[postQuestion]
     * use ([openSessionIntent]) — an entry naming a running request must be able to take the user to
     * it, and the shade is the only surface they have while the app is backgrounded.
     * [ongoingTapTarget] owns which session that is and when there is none; [ongoingActions] owns
     * which buttons appear, and [ongoingSmallIcon] which glyph states the mode.
     *
     * **There is deliberately no Stop action for an ordinary run** — the reasoning is at
     * [OngoingAction.STOP], which is a different thing and stops something real.
     */
    fun buildOngoing(sessionId: String, label: String, deviceControl: Boolean = false): Notification =
        Notification.Builder(context, CHANNEL_ONGOING)
            .setSmallIcon(ongoingSmallIcon(deviceControl))
            // Says the true thing when the session can drive the phone. The
            // notification is not a formality here — it is the only indication
            // the user has that an agent may act on their screen while they are
            // in another app, and the only place they can stop it.
            .setContentTitle(
                if (deviceControl) "Mewbo can control this device" else "Mewbo is working…",
            )
            .apply {
                val body = when {
                    label.isNotBlank() -> label
                    deviceControl -> "Tap Stop to end the session."
                    else -> ""
                }
                if (body.isNotBlank()) setContentText(body)
                // Brand clay on the shade entry's small-icon badge + app name. It is a SECOND
                // carrier of the same fact the glyph carries, never the only one: `setColor` does
                // not reach the status-bar glyph (SystemUI tints that itself), so a colour-only
                // signal would be invisible in the one place this state has to be readable —
                // see [ongoingSmallIcon].
                if (deviceControl) setColor(context.getColor(R.color.aura_clay_core))
                val target = ongoingTapTarget(sessionId)
                target?.let { setContentIntent(openSessionIntent(it)) }
                for (action in ongoingActions(target, deviceControl)) {
                    val pending = when (action) {
                        // Non-null by construction — [ongoingActions] emits OPEN only for a
                        // non-null target. It is the SAME PendingIntent the content intent uses
                        // (same intent, same `sessionId.hashCode()` request code), so the button
                        // introduces no new request code to collide with [STOP_REQUEST_CODE].
                        OngoingAction.OPEN -> openSessionIntent(checkNotNull(target))
                        OngoingAction.STOP -> PendingIntent.getService(
                            context,
                            STOP_REQUEST_CODE,
                            RunNotificationService.stopIntent(context),
                            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                        )
                    }
                    addAction(Notification.Action.Builder(null, action.title, pending).build())
                }
            }
            .setOngoing(true)
            .setShowWhen(false)
            .build()

    /**
     * Re-posts the ongoing notification in place, which is how the device-control PRESENCE is raised
     * and dropped while the service keeps running.
     *
     * `notify` with the id already passed to `startForeground` UPDATES that notification rather than
     * adding a second one, so the service does not have to re-enter the foreground to change what it
     * says. That matters because the grant is taken and lost MID-run, often while Aura is
     * backgrounded — the one moment a fresh `startForeground` is least welcome.
     */
    fun updateOngoing(sessionId: String, label: String, deviceControl: Boolean) {
        manager.notify(ONGOING_NOTIFICATION_ID, buildOngoing(sessionId, label, deviceControl))
    }

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

    /**
     * **The ONE intent that opens this app on a session**, shared by the shade tap
     * ([openSessionIntent]) and the programmatic return ([openSession]). Both destinations must be
     * the same destination; a second construction of it is how the flags and the handoff extra come
     * to disagree, and each half looks correct in isolation.
     *
     * `FLAG_ACTIVITY_NEW_TASK` is load-bearing for BOTH callers now — a `PendingIntent` activity
     * start needs it, and so does a `startActivity` from a `Service`, which is not an `Activity`
     * context. `CLEAR_TOP` means an already-running app is re-used rather than stacked; the extra is
     * then read by `MainActivity.onNewIntent`.
     */
    private fun sessionIntent(sessionId: String): Intent =
        Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_HANDOFF_SESSION_ID, sessionId)

    private fun openSessionIntent(sessionId: String): PendingIntent =
        PendingIntent.getActivity(
            context,
            sessionId.hashCode(),
            sessionIntent(sessionId),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    /**
     * Brings the app to the front on [sessionId] WITHOUT a tap — the same destination, flags and
     * handoff extra a shade tap lands on, because both are [sessionIntent].
     *
     * **It lives here, next to the notifications, because this class owns how this app opens a
     * session** — the caller ([RunNotificationService], when a device-control grant ends while the
     * user is in another app) owns only the decision to do it.
     *
     * **Deliberately NOT `openSessionIntent(sessionId).send()`, though that would reuse even more.**
     * Creator and sender would be this same process, so the `PendingIntent` grants no privilege it
     * does not already have, while adding a second background-activity-start question (the
     * creator-side opt-in) on top of the one the direct start already asks. One mechanism to reason
     * about is worth more here than one fewer line.
     *
     * Throws whatever the platform throws; a caller that must not be affected by a failed launch is
     * the one that wraps it.
     */
    fun openSession(sessionId: String) {
        context.startActivity(sessionIntent(sessionId))
    }

    /**
     * A button on the ongoing entry. Declaration order IS render order — Android lays actions out
     * left to right in the order they were added, and [ongoingActions] returns them in this order.
     *
     * The two are deliberately asymmetric in what they cost a mis-tap: OPEN is free (it opens a
     * screen), STOP ends a grant. OPEN going first is not decoration — the leftmost button is the
     * one a thumb reaches from a pocket, so the harmless one takes that slot.
     */
    enum class OngoingAction(val title: String) {
        /**
         * Opens the session the entry is reporting on — the same destination as the entry's own
         * tap, promoted to a labelled button because a tappable notification body advertises
         * nothing. The user asked for a visible way to get from the shade into the session.
         */
        OPEN("Open"),

        /**
         * Releases the device-control grant. **This is the ONLY stop button this feature has, and
         * an ordinary run getting none is a decision, not an omission.** No server operation means
         * "stop this run, keep the session": `ChatViewModel.stop()` is a client-side detach (the
         * run continues), `interrupt` stops one STEP and the loop carries on with error results,
         * and `terminate` is an irreversible session kill returning 410 for every later call. So a
         * run Stop is either a lie or a destructive one-way door behind a mis-tappable shade
         * button — the reasoning lives in `notify/CLAUDE.md`. This one earns its button because it
         * stops something real and is reversible: the intent reaches [RunNotificationService],
         * which calls `deviceControlSession.stop()`, and the user can simply grant again.
         */
        STOP("Stop"),
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

        /** Distinct from the session-tap intents so Stop never collides with an
         * open-session PendingIntent for the same session. */
        private const val STOP_REQUEST_CODE = 9001

        /**
         * The pure decision (unit-tested): which session the ongoing notification's tap opens, or
         * `null` for no content intent at all.
         *
         * **The service watches N sessions and posts exactly ONE ongoing notification**
         * ([ONGOING_NOTIFICATION_ID] is fixed), so the tap needs a single well-defined target. It is
         * the session whose label the notification is CURRENTLY showing — the newest run to start,
         * which is the one the body text names. A second run re-posts in place and moves the label
         * and the target together, so what the entry says and where it goes can never disagree. That
         * agreement is the whole rule; picking "the oldest watch" or "the one with device control"
         * would let the notification describe one run and open another.
         *
         * `null` for a blank id — the service's state before any session is watched, and the Stop
         * intent's, which carries none. A tap must not hand `MainActivity` an empty handoff extra:
         * `AuraNavHost` gates its handoff effect on `null`, NOT on blank, so a blank id navigates to
         * `chat?sessionId=` (an empty session arg) rather than doing nothing.
         */
        internal fun ongoingTapTarget(sessionId: String?): String? =
            sessionId?.takeIf { it.isNotBlank() }

        /**
         * The pure decision (unit-tested): which glyph the status bar shows.
         *
         * **This is the one piece of chrome visible without expanding anything**, so it is what
         * distinguishes "a run is going" from "something is touching my screen".
         *
         * **It states the mode by SHAPE, and that is forced rather than chosen.** SystemUI draws
         * every status-bar glyph through its own single foreground tint so it stays legible against
         * any wallpaper; `Notification.setColor` reaches the shade entry's badge and never the
         * status bar. Brand orange therefore cannot be the signal on the surface that matters —
         * a colour that does not render is not a state. `buildOngoing` still sets the colour,
         * because it does render in the shade, but the glyph is what carries the fact.
         *
         * The control glyph also ROTATES, which the drawable owns end to end
         * (`drawable/ic_stat_device_control.xml` is an `<animation-list>`, i.e. an
         * `AnimationDrawable` that starts itself when the view takes it). Nothing here re-posts the
         * notification to animate it.
         */
        internal fun ongoingSmallIcon(deviceControl: Boolean): Int =
            if (deviceControl) R.drawable.ic_stat_device_control else R.drawable.ic_launcher_monochrome

        /**
         * The pure decision (unit-tested): which buttons the ongoing entry carries, in render
         * order. Takes the ALREADY-RESOLVED tap target ([ongoingTapTarget]) rather than a raw
         * session id, so "there is somewhere to open" is decided once and both the content intent
         * and the button read the same answer — an Open button that opens nothing is worse than no
         * button, and passing the raw id would let the two disagree.
         *
         * [OngoingAction.STOP] does NOT need a target: it carries no session (it releases the
         * app-wide grant), which is exactly why the Stop intent is allowed to name none.
         */
        internal fun ongoingActions(tapTarget: String?, deviceControl: Boolean): List<OngoingAction> =
            buildList {
                if (tapTarget != null) add(OngoingAction.OPEN)
                if (deviceControl) add(OngoingAction.STOP)
            }
    }
}
