package com.mewbo.aura.data.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.resume
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext

/**
 * What the platform installer did with an APK — never a bare boolean, for the reason
 * [com.mewbo.aura.data.device.shizuku.OverlayGrantOutcome] spells out: "it did not install" has
 * several causes and each needs a different action from the person holding the device.
 *
 * [message] is written for that person, so a caller can render it verbatim.
 */
sealed interface InstallOutcome {
    /**
     * What to tell the person, always — refusals included, because an update that stops with no
     * explanation is indistinguishable from an app that has quietly broken.
     */
    val message: String

    /** The package manager replaced the installed app. The new code runs on next launch. */
    data object Succeeded : InstallOutcome {
        override val message: String = "Update installed. It takes effect the next time Aura starts."
    }

    /**
     * The user dismissed the system confirmation, or the platform aborted before committing.
     *
     * Its own arm rather than a refusal: nothing is wrong and there is nothing to fix, so a caller
     * must be able to stay quiet about it instead of raising an error.
     */
    data object Cancelled : InstallOutcome {
        override val message: String = "Update cancelled. Nothing was changed."
    }

    /**
     * The installed app and this APK are signed with different keys. Android cannot update
     * across a signature change; the only cure is an uninstall, which loses the app's data.
     *
     * `STATUS_FAILURE_CONFLICT` is how the platform reports this, and the message names the
     * remedy explicitly because the failure is otherwise unactionable — every retry of the same
     * download fails identically.
     */
    data object SignatureMismatch : InstallOutcome {
        override val message: String =
            "This build is signed with a different key than the installed one, so Android will " +
                "not update over it. Uninstall Aura first, then install this file — uninstalling " +
                "erases the app's settings and saved sign-in."
    }

    /** Any other terminal refusal, carrying whatever the platform said about it. */
    data class Refused(override val message: String) : InstallOutcome
}

/**
 * Hands a downloaded APK to the platform package installer and reports what it did.
 *
 * **The session API, never `ACTION_VIEW` / `ACTION_INSTALL_PACKAGES`.** A view intent hands the
 * file to whatever package-installer activity resolves and returns nothing: the app is left
 * guessing whether the user confirmed, declined, or hit an error, so the only honest thing it can
 * render afterwards is silence. [PackageInstaller] streams the bytes from inside this app's own
 * flow and broadcasts a discriminated terminal status back, which is what makes
 * [InstallOutcome.SignatureMismatch] distinguishable from [InstallOutcome.Cancelled] at all. It
 * also needs no `FileProvider`: the bytes go into the session directly, so no content URI is
 * exported and no grant has to be handed to another process.
 */
@Singleton
class ApkInstaller @Inject constructor(
    @ApplicationContext private val context: Context,
) : PlatformInstaller {

    /**
     * Whether the OS will let this app install packages ("Install unknown apps"). `O(1)`.
     *
     * The manifest's `REQUEST_INSTALL_PACKAGES` is not the grant — it is a SPECIAL permission with
     * no runtime dialog, so this read is the only thing that answers, and it is re-read on resume
     * rather than cached (the toggle lives on a system screen this app cannot observe).
     */
    override fun canInstallPackages(): Boolean = context.packageManager.canRequestPackageInstalls()

    /**
     * Open the system screen that grants it. Returns false when no such screen resolves on this
     * device, so the caller can say so instead of leaving a tap that does nothing.
     *
     * **Deliberately NOT gated on `DeviceShape`.** A device-shape member would be an unmeasured
     * guess — nobody has run this on a Fire TV, and the same screen can be missing from a kiosk
     * build or a stripped AOSP handheld, neither of which is a television. Reachability is
     * therefore answered at runtime by whether the intent resolves, exactly as
     * `PermissionRequest.openOverlaySettings` already does for the overlay screen.
     */
    override fun openInstallPermissionScreen(): Boolean {
        val scoped = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (runCatching { context.startActivity(scoped) }.isSuccess) return true

        // The app's own details page still carries the toggle on most images that lack the
        // dedicated screen; a tap that silently does nothing is the one outcome to avoid.
        val details = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return runCatching { context.startActivity(details) }.isSuccess
    }

    /**
     * Hand [apk] to [PackageInstaller] and suspend until the platform reports a terminal status.
     *
     * `O(size of the APK)` — it streams the file into the session.
     *
     * Every platform call here is caught and degrades to [InstallOutcome.Refused]: this runs from
     * a Settings row, and a `SecurityException` or a full disk crashing the screen the user was on
     * is strictly worse than a sentence explaining that the update did not land.
     */
    override suspend fun install(apk: File): InstallOutcome {
        val installer = context.packageManager.packageInstaller
        val sessionId = runCatching { installer.createSession(sessionParams(apk)) }
            .getOrElse { return refused("Android would not open an install session", it) }

        val staged = runCatching { stage(installer, sessionId, apk) }
        staged.exceptionOrNull()?.let { failure ->
            runCatching { installer.abandonSession(sessionId) }
            return refused("The update file could not be handed to Android", failure)
        }

        return awaitTerminalStatus(installer, sessionId)
    }

    private fun sessionParams(apk: File): PackageInstaller.SessionParams =
        PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            // Naming ourselves lets the platform match the session against the installed app and
            // show the update — rather than first-install — confirmation.
            setAppPackageName(context.packageName)
            setSize(apk.length())
        }

    /**
     * Stream the file in. `fsync` before closing is not belt-and-braces: without it the bytes may
     * still be in the page cache when `commit` runs, and the commit fails on a short session with
     * a message that names neither the file nor the cause.
     */
    private suspend fun stage(installer: PackageInstaller, sessionId: Int, apk: File) {
        withContext(Dispatchers.IO) {
            installer.openSession(sessionId).use { session ->
                session.openWrite(WRITE_NAME, 0, apk.length()).use { out ->
                    apk.inputStream().use { input -> input.copyTo(out) }
                    session.fsync(out)
                }
            }
        }
    }

    /**
     * Commit, then wait for the broadcast the platform sends back.
     *
     * **The receiver is registered at RUNTIME, never in the manifest.** A manifest receiver is a
     * process-wide entry point that any status broadcast could reach; this one is scoped to a
     * single session's action string and torn down on every exit path, so a second concurrent
     * commit cannot be answered by the wrong waiter.
     */
    private suspend fun awaitTerminalStatus(installer: PackageInstaller, sessionId: Int): InstallOutcome =
        suspendCancellableCoroutine { continuation ->
            // The session id is in the action so two commits in one process cannot cross.
            val action = "$RESULT_ACTION_PREFIX$sessionId"
            val settled = AtomicBoolean(false)
            lateinit var receiver: BroadcastReceiver

            fun finish(outcome: InstallOutcome) {
                if (!settled.compareAndSet(false, true)) return
                runCatching { context.unregisterReceiver(receiver) }
                continuation.resume(outcome)
            }

            receiver = object : BroadcastReceiver() {
                override fun onReceive(received: Context?, intent: Intent?) {
                    val status = intent?.getIntExtra(PackageInstaller.EXTRA_STATUS, Int.MIN_VALUE)
                        ?: return
                    // PENDING_USER_ACTION arrives FIRST and is not terminal — it is the platform
                    // asking us to show its confirmation. Launch it and keep waiting.
                    if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                        launchConfirmation(intent)
                        return
                    }
                    finish(
                        outcomeFor(status, intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)),
                    )
                }
            }

            val registered = runCatching {
                val filter = IntentFilter(action)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
                } else {
                    context.registerReceiver(receiver, filter)
                }
            }
            registered.exceptionOrNull()?.let { failure ->
                settled.set(true)
                runCatching { installer.abandonSession(sessionId) }
                continuation.resume(refused("Android would not report the install result", failure))
                return@suspendCancellableCoroutine
            }

            continuation.invokeOnCancellation {
                if (settled.compareAndSet(false, true)) {
                    runCatching { context.unregisterReceiver(receiver) }
                }
                runCatching { installer.abandonSession(sessionId) }
            }

            val committed = runCatching {
                val pending = PendingIntent.getBroadcast(
                    context,
                    sessionId,
                    // Explicit package: the result is ours and must not be deliverable elsewhere.
                    Intent(action).setPackage(context.packageName),
                    pendingIntentFlags(),
                )
                installer.openSession(sessionId).use { it.commit(pending.intentSender) }
            }
            committed.exceptionOrNull()?.let { failure ->
                runCatching { installer.abandonSession(sessionId) }
                finish(refused("Android refused to start the install", failure))
            }
        }

    /**
     * Show the platform's own confirmation. Best effort inside [runCatching]: a device that cannot
     * start it leaves the wait to time out with the caller's own cancellation rather than taking
     * the screen down, and `FLAG_ACTIVITY_NEW_TASK` is required because the broadcast receiver is
     * not an activity context.
     */
    private fun launchConfirmation(intent: Intent) {
        val confirm = confirmationIntent(intent) ?: return
        runCatching { context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    private fun confirmationIntent(intent: Intent): Intent? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(Intent.EXTRA_INTENT)
        }

    /**
     * `FLAG_MUTABLE` is load-bearing: the system fills `EXTRA_STATUS` (and the confirmation intent)
     * into this `PendingIntent` before sending it, which an immutable one forbids. The constant
     * only exists from API 31, and below that a `PendingIntent` is mutable by default — hence the
     * branch rather than a suppressed lint warning.
     */
    private fun pendingIntentFlags(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }

    /** Never a raw status number alone — a caller renders [InstallOutcome.message] verbatim. */
    private fun outcomeFor(status: Int, statusMessage: String?): InstallOutcome = when (status) {
        PackageInstaller.STATUS_SUCCESS -> InstallOutcome.Succeeded
        PackageInstaller.STATUS_FAILURE_ABORTED -> InstallOutcome.Cancelled
        PackageInstaller.STATUS_FAILURE_CONFLICT -> InstallOutcome.SignatureMismatch
        else -> InstallOutcome.Refused(
            listOfNotNull(
                "Android refused the update: ${reasonFor(status)}.",
                statusMessage?.takeIf { it.isNotBlank() }?.let { "It reported: $it" },
            ).joinToString(" "),
        )
    }

    /**
     * A sentence fragment per status, because the number is meaningless to the person reading it
     * and `EXTRA_STATUS_MESSAGE` is frequently absent or an internal string.
     */
    private fun reasonFor(status: Int): String = when (status) {
        PackageInstaller.STATUS_FAILURE_BLOCKED -> "the device blocked it"
        PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> "this build is not compatible with this device"
        PackageInstaller.STATUS_FAILURE_INVALID -> "the update file is damaged or incomplete"
        PackageInstaller.STATUS_FAILURE_STORAGE -> "there is not enough free storage"
        PackageInstaller.STATUS_FAILURE_TIMEOUT -> "the install timed out"
        PackageInstaller.STATUS_FAILURE -> "the install failed"
        else -> "an unrecognised error (status $status)"
    }

    /** Carries the exception's own message, which is routinely the only statement of the cause. */
    private fun refused(what: String, cause: Throwable): InstallOutcome.Refused =
        InstallOutcome.Refused(
            listOfNotNull("$what.", cause.message?.takeIf { it.isNotBlank() }?.let { "It reported: $it" })
                .joinToString(" "),
        )

    private companion object {
        /** One entry per session; the name is internal to the session and never surfaces. */
        const val WRITE_NAME = "aura_update.apk"

        /** The session id is appended, so two commits in this process cannot answer each other. */
        const val RESULT_ACTION_PREFIX = "com.mewbo.aura.APK_INSTALL_RESULT."
    }
}
