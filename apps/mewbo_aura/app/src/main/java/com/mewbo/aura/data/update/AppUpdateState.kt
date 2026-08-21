package com.mewbo.aura.data.update

/** A release this device could install, reduced to what the screen and the downloader need. */
data class AvailableUpdate(
    /** What the release calls itself — the tag with its product prefix removed, e.g. `0.0.21.0`. */
    val versionLabel: String,
    val tagName: String,
    /** The release's own title, when it has one. Never the body: a release note here runs to pages. */
    val title: String?,
    val assetName: String,
    val downloadUrl: String,
    /** The forge's DECLARED byte count. The only integrity signal either release API offers. */
    val sizeBytes: Long,
    val prerelease: Boolean,
)

/**
 * Everything the updater can honestly say about itself, as one closed union.
 *
 * **The arms exist because the alternatives are all wrong in the same way.** This screen's standing
 * law is that a status which guesses is worse than none (`ui/settings/CLAUDE.md`), and every
 * collapsing of these states into "up to date" is a guess:
 *
 * - [CheckFailed] is NOT [UpToDate]. An unreachable forge means nobody asked, which is a different
 *   fact from an answer of no.
 * - [NoInstallableBuild] is NOT [UpToDate] and NOT [CheckFailed]. A newer release exists and simply
 *   carries no file this device can install — the measured state of the public GitHub mirror, whose
 *   releases carry no APK assets at all. Nothing is broken and nothing is available.
 * - [Unsupported] is NOT [UpToDate]. A build given no release source never looked anywhere.
 */
sealed interface AppUpdateState {

    /** True while work is in flight, so a second tap cannot start a duplicate of it. A member
     * rather than a predicate at the call site: adding an arm that is also busy must not need
     * finding every place that asked. */
    val isBusy: Boolean get() = this is Checking || this is Downloading || this is Installing

    /** The update this state is about, when it is about one. */
    val update: AvailableUpdate? get() = null

    /** Nothing has asked yet. The only honest state a freshly-opened screen can be in. */
    data object NotChecked : AppUpdateState

    /** This build carries no release source, so it cannot check at all. */
    data object Unsupported : AppUpdateState

    data object Checking : AppUpdateState

    /** The newest installable Aura release is the one already running. */
    data class UpToDate(val installedVersion: String) : AppUpdateState

    /** A newer Aura release exists, and it publishes no file that fits this device. */
    data class NoInstallableBuild(val tagName: String) : AppUpdateState

    /** The check itself did not complete. [reason] is written for the person holding the device. */
    data class CheckFailed(val reason: String) : AppUpdateState

    data class Available(override val update: AvailableUpdate) : AppUpdateState

    /** [totalBytes] is the declared asset size, so the fraction is known from the first byte —
     * a server that omits `Content-Length` cannot flatten the bar to indeterminate. */
    data class Downloading(
        override val update: AvailableUpdate,
        val downloadedBytes: Long,
        val totalBytes: Long,
    ) : AppUpdateState {
        /** `0f..1f`, clamped — a server sending MORE than it declared must not overflow the bar. */
        val fraction: Float
            get() = if (totalBytes <= 0) 0f else (downloadedBytes.toFloat() / totalBytes).coerceIn(0f, 1f)
    }

    /** Downloaded, size-checked, and confirmed by the archive's own manifest to be a newer build
     * of this very package. Nothing reaches the installer that has not passed all three. */
    data class ReadyToInstall(override val update: AvailableUpdate, val apkPath: String) : AppUpdateState

    data class Installing(override val update: AvailableUpdate) : AppUpdateState

    /**
     * A download or an install did not finish.
     *
     * ONE arm rather than two: the user's next move is the same either way (read the sentence, try
     * again), and the sentence itself is what distinguishes a truncated download from a signature
     * conflict. A second arm would buy a `when` branch and no extra information.
     */
    data class Failed(override val update: AvailableUpdate, val reason: String) : AppUpdateState
}
