package com.mewbo.aura.data.update

import com.mewbo.aura.di.ApplicationScope
import com.mewbo.aura.di.UpdateDownloads
import java.io.File
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers

/**
 * The app's own update lifecycle: find a newer release, fetch the file that fits this device,
 * prove it is one, and hand it to the installer.
 *
 * **`@Singleton`, and scoped to the application rather than to the Settings screen.** The artifact
 * is around a hundred megabytes. A download tied to a ViewModel would be cancelled by the user
 * backing out of Settings for ten seconds, and would restart from zero on the way back in — so the
 * state and the job live here, above every screen, and Settings is one observer of them.
 *
 * Cost: [check] is `O(collection)` bounded to one page ([UpdateChannel.PAGE_SIZE]) — one request,
 * fixed size, no per-release follow-up. [download] is `O(size of the asset)` and is the only
 * unbounded work in the class; it is explicitly started, reports progress, and is cancellable.
 */
@Singleton
class AppUpdateRepository @Inject constructor(
    private val channel: UpdateChannel,
    private val releaseApi: ReleaseApi,
    private val packageFacts: PackageFacts,
    private val installer: PlatformInstaller,
    @UpdateDownloads private val downloadDir: File,
    @ApplicationScope private val scope: CoroutineScope,
) {
    private val _state = MutableStateFlow<AppUpdateState>(
        if (channel.isConfigured) AppUpdateState.NotChecked else AppUpdateState.Unsupported,
    )
    val state: StateFlow<AppUpdateState> = _state.asStateFlow()

    /** What is running right now, as the package manager reports it — `0.0.20-enterprise (30)`.
     * Read from the INSTALLED package rather than from a build constant, because that is the thing
     * an update would replace. */
    val installedVersionLabel: String
        get() = packageFacts.installed().let { "${it.versionName} (${it.versionCode})" }

    /** Whether the OS will currently let this app install a package. Re-read on every ask: it is a
     * special grant changed on a system screen, so there is no callback to observe. */
    fun canInstallPackages(): Boolean = installer.canInstallPackages()

    /** Send the user to the system screen that grants it; `false` when this device has none. */
    fun openInstallPermissionScreen(): Boolean = installer.openInstallPermissionScreen()

    private var work: Job? = null

    /**
     * Ask the forge what the newest installable Aura release is.
     *
     * Re-entrant by design — the screen's re-check button and its first open both land here — but
     * it will not interrupt a download in flight, because a check's answer is worth strictly less
     * than the megabytes already fetched.
     */
    fun check() {
        if (!channel.isConfigured) {
            _state.value = AppUpdateState.Unsupported
            return
        }
        if (_state.value.isBusy) return
        work = scope.launch {
            _state.value = AppUpdateState.Checking
            _state.value = runCatching { resolve() }
                .getOrElse { failure -> AppUpdateState.CheckFailed(readableReason(failure)) }
        }
    }

    /**
     * Choose the newest Aura release this device can actually install.
     *
     * The three filters are independent and each drops a real thing seen on a live forge: a draft
     * (invisible to an anonymous reader anyway, but never trusted to be), a release belonging to
     * the SERVER rather than to this app (the tag namespace is shared), and a release carrying no
     * file for this flavor and build type (every release on the public mirror).
     *
     * **Prereleases are deliberately INCLUDED.** Aura's newest build is routinely published as one,
     * so excluding them would offer a device that is already running `0.0.20.0` an "update" to
     * `0.0.19.0`, or nothing at all. The state carries the flag so the screen can say which it is.
     */
    private suspend fun resolve(): AppUpdateState {
        val installed = packageFacts.installed()
        val installedVersion = installed.version
        val releases = releaseApi.releases(channel.owner, channel.repo, UpdateChannel.PAGE_SIZE, UpdateChannel.PAGE_SIZE)

        val auraReleases = releases
            .filterNot { it.draft }
            .mapNotNull { release -> channel.releaseVersion(release)?.let { release to it } }
            .sortedByDescending { (_, version) -> version }

        val newer = auraReleases.filter { (_, version) -> installedVersion == null || version > installedVersion }
        if (newer.isEmpty()) return AppUpdateState.UpToDate(installed.versionName)

        val installable = newer.firstNotNullOfOrNull { (release, version) ->
            release.assets.firstOrNull(channel::fits)?.let { asset ->
                AvailableUpdate(
                    versionLabel = version.toString(),
                    tagName = release.tagName,
                    title = release.name?.takeIf { it.isNotBlank() },
                    assetName = asset.name,
                    downloadUrl = asset.browserDownloadUrl,
                    sizeBytes = asset.size,
                    prerelease = release.prerelease,
                )
            }
        }
        // A newer release exists and publishes nothing this device can install. Reported as its own
        // state and named by tag, so the user can go and look rather than be told "up to date"
        // about a version they can see does not match theirs.
        return installable?.let(AppUpdateState::Available)
            ?: AppUpdateState.NoInstallableBuild(newer.first().first.tagName)
    }

    /** Fetch the chosen asset. No-op unless an update is the current state, so a double tap cannot
     * start two downloads of the same hundred megabytes. */
    fun download() {
        val update = (_state.value as? AppUpdateState.Available)?.update
            ?: (_state.value as? AppUpdateState.Failed)?.update
            ?: return
        if (_state.value.isBusy) return
        work = scope.launch {
            _state.value = AppUpdateState.Downloading(update, 0, update.sizeBytes)
            _state.value = try {
                withContext(Dispatchers.IO) { fetch(update) }
            } catch (cancellation: CancellationException) {
                throw cancellation
            } catch (failure: Throwable) {
                AppUpdateState.Failed(update, readableReason(failure))
            }
        }
    }

    /**
     * Stream the asset to disk, then refuse it unless it is provably the update it claims to be.
     *
     * **Three checks, because the release API offers no checksum and neither forge has one to
     * offer.** Declared size catches a truncated transfer; the archive's own package name catches a
     * file that is not this app; the archive's version code catches a stale or wrong artifact that
     * would install as a downgrade. A wrong-FLAVOR APK is caught earlier, by the asset name, and
     * cannot be caught here at all — both flavors share one application id — which is exactly why
     * the file-name scheme carries the flavor.
     *
     * The bytes land on a `.part` file that is renamed only once every check has passed, so a
     * transfer killed mid-flight cannot leave something a later run mistakes for a finished
     * download.
     */
    private suspend fun fetch(update: AvailableUpdate): AppUpdateState {
        downloadDir.mkdirs()
        // Nothing here is worth keeping between attempts: a partial is unusable and a previous
        // version's APK is a hundred megabytes of cache nobody will ever install.
        downloadDir.listFiles()?.forEach { it.delete() }
        val target = File(downloadDir, update.assetName)
        val partial = File(downloadDir, "${update.assetName}.part")

        releaseApi.download(update.downloadUrl).use { body ->
            body.byteStream().use { source ->
                partial.outputStream().use { sink ->
                    val buffer = ByteArray(DOWNLOAD_BUFFER_BYTES)
                    var total = 0L
                    var announced = 0L
                    while (true) {
                        val read = source.read(buffer)
                        if (read <= 0) break
                        sink.write(buffer, 0, read)
                        total += read
                        // Throttled: a state emission per 8 KB chunk would recompose the progress
                        // bar thousands of times a second and starve the very transfer it reports.
                        if (total - announced >= PROGRESS_STEP_BYTES) {
                            announced = total
                            _state.value = AppUpdateState.Downloading(update, total, update.sizeBytes)
                        }
                    }
                }
            }
        }

        if (update.sizeBytes > 0 && partial.length() != update.sizeBytes) {
            partial.delete()
            return AppUpdateState.Failed(
                update,
                "The download finished short — ${partial.length()} bytes of ${update.sizeBytes}. Try again.",
            )
        }
        val archive = packageFacts.archive(partial)
        val installed = packageFacts.installed()
        if (archive == null || archive.packageName != installed.packageName) {
            partial.delete()
            return AppUpdateState.Failed(update, "That file is not a Mewbo Aura package. Nothing was installed.")
        }
        if (archive.versionCode <= installed.versionCode) {
            partial.delete()
            return AppUpdateState.Failed(
                update,
                "That release carries build ${archive.versionCode}, which is not newer than the installed " +
                    "${installed.versionCode}. Nothing was installed.",
            )
        }
        if (!partial.renameTo(target)) {
            partial.delete()
            return AppUpdateState.Failed(update, "Couldn't finish writing the download to storage.")
        }
        return AppUpdateState.ReadyToInstall(update, target.absolutePath)
    }

    /** Hand the verified APK to the platform installer. No-op unless one is waiting. */
    fun install() {
        val ready = _state.value as? AppUpdateState.ReadyToInstall ?: return
        work = scope.launch {
            _state.value = AppUpdateState.Installing(ready.update)
            val outcome = installer.install(File(ready.apkPath))
            // A SUCCESS is never rendered: the process is replaced by the new build, and a state
            // saying "installed" would only ever be seen if it had not been. Anything else returns
            // the user to a file that is still on disk and still installable.
            _state.value = when (outcome) {
                is InstallOutcome.Succeeded -> AppUpdateState.Installing(ready.update)
                else -> AppUpdateState.Failed(ready.update, outcome.message)
            }
        }
    }

    /**
     * Abandon whatever is in flight and go back to offering the update.
     *
     * The partial file is left for [fetch] to clear on the next attempt rather than deleted here —
     * cancellation races the writer, and a delete landing between two writes recreates the file it
     * just removed.
     */
    fun cancel() {
        work?.cancel()
        work = null
        val update = _state.value.update ?: return
        _state.value = AppUpdateState.Available(update)
    }

    /**
     * A transport failure in the words of the person holding the device.
     *
     * The exception's own message is kept when it has one — `Unable to resolve host …` says more
     * than any sentence written here could — and only a message-less failure gets its class name.
     */
    private fun readableReason(failure: Throwable): String = when {
        !failure.message.isNullOrBlank() -> failure.message!!
        failure is IOException -> "Couldn't reach the release server."
        else -> failure::class.simpleName ?: "Something went wrong."
    }

    private companion object {
        const val DOWNLOAD_BUFFER_BYTES = 64 * 1024
        /** Roughly a percent of a hundred-megabyte APK — frequent enough to look live, rare enough
         * that the emissions cost nothing next to the transfer. */
        const val PROGRESS_STEP_BYTES = 512L * 1024
    }
}
