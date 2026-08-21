package com.mewbo.aura.data.update

import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * [AppUpdateRepository] end to end, over hand-rolled fakes for [ReleaseApi], [PackageFacts] and
 * [PlatformInstaller] — the same class-under-test the class's own KDoc names as the point of the
 * interface seams: no `Context`, no real forge, no real package manager.
 *
 * **[fetch] genuinely hops onto `Dispatchers.IO` — a real thread, not the test scheduler — so
 * `advanceUntilIdle()` alone cannot prove a download has finished.** The injected
 * [UnconfinedTestDispatcher] scope makes `check()`/`install()` (which never leave it) complete
 * synchronously inside the triggering call; [awaitState] is what actually witnesses the IO-thread
 * hop finishing for `download()`, by polling wall-clock time rather than virtual time. Each test
 * still asserts the terminal state's own fields, so a helper that returned too early would fail the
 * very next assertion rather than passing silently.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class AppUpdateRepositoryTest {

    @get:Rule val tempFolder = TemporaryFolder()

    private val installed = ApkIdentity(packageName = "com.mewbo.aura", versionName = "0.0.20", versionCode = 20)

    // ---- 1-8: check() ----

    @Test
    fun `a newer release with a fitting asset surfaces as Available`() = runTest {
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.21.0")) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertTrue("expected Available, got $state", state is AppUpdateState.Available)
        val update = (state as AppUpdateState.Available).update
        assertEquals("0.0.21.0", update.versionLabel)
        assertEquals("aura-0.0.21.0-enterprise-debug.apk", update.assetName)
        assertEquals("https://example.invalid/0.0.21.0.apk", update.downloadUrl)
    }

    @Test
    fun `the installed version being the newest reads as UpToDate`() = runTest {
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.20.0")) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertEquals(AppUpdateState.UpToDate("0.0.20"), state)
    }

    @Test
    fun `a newer release whose assets do not fit this device is NoInstallableBuild, not UpToDate`() = runTest {
        // The real shape of the public GitHub mirror: a release whose assets list is empty.
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.21.0", assets = emptyList())) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertTrue("expected NoInstallableBuild, got $state", state is AppUpdateState.NoInstallableBuild)
        assertEquals("aura-0.0.21.0", (state as AppUpdateState.NoInstallableBuild).tagName)
    }

    @Test
    fun `a release list of only the server's own tags reads as UpToDate`() = runTest {
        val api = FakeReleaseApi().apply { releases = listOf(ReleaseDto(tagName = "v0.0.12")) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertEquals(AppUpdateState.UpToDate("0.0.20"), state)
    }

    @Test
    fun `a newer prerelease is offered as Available, prerelease flag intact`() = runTest {
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.21.0", prerelease = true)) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertTrue("expected Available, got $state", state is AppUpdateState.Available)
        assertTrue((state as AppUpdateState.Available).update.prerelease)
    }

    @Test
    fun `a draft release is never offered`() = runTest {
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.21.0", draft = true)) }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertEquals(AppUpdateState.UpToDate("0.0.20"), state)
    }

    @Test
    fun `a failed forge call reports CheckFailed with the exception's message, never UpToDate`() = runTest {
        val api = FakeReleaseApi().apply { releasesFailure = IllegalStateException("connection reset") }
        val repo = repository(releaseApi = api, downloadDir = tempFolder.newFolder())

        repo.check()
        val state = awaitState(repo) { it !is AppUpdateState.Checking && it != AppUpdateState.NotChecked }

        assertTrue("expected CheckFailed, got $state", state is AppUpdateState.CheckFailed)
        assertEquals("connection reset", (state as AppUpdateState.CheckFailed).reason)
    }

    @Test
    fun `an unconfigured channel starts and stays Unsupported and issues no request`() = runTest {
        val channel = UpdateChannel(apiRoot = "", owner = "acme", repo = "aura", flavor = "enterprise", buildType = "debug")
        val api = FakeReleaseApi().apply { releases = listOf(release("aura-0.0.21.0")) }
        val repo = repository(channel = channel, releaseApi = api, downloadDir = tempFolder.newFolder())

        assertEquals(AppUpdateState.Unsupported, repo.state.value)

        repo.check()

        assertEquals(AppUpdateState.Unsupported, repo.state.value)
        assertEquals("an unconfigured channel must never dial the forge", 0, api.releasesCallCount)
    }

    // ---- 9-13: download() / install() ----

    @Test
    fun `a happy download lands ReadyToInstall with the exact bytes and no dangling part file`() = runTest {
        val bytes = "the whole update, byte for byte".toByteArray()
        val api = FakeReleaseApi().apply {
            releases = listOf(release("aura-0.0.21.0", assetSize = bytes.size.toLong()))
            downloadBytes = bytes
        }
        val downloadDir = tempFolder.newFolder()
        val repo = repository(releaseApi = api, downloadDir = downloadDir)
        repo.check()
        awaitState(repo) { it is AppUpdateState.Available }

        repo.download()
        val state = awaitState(repo) { it is AppUpdateState.ReadyToInstall || it is AppUpdateState.Failed }

        assertTrue("expected ReadyToInstall, got $state", state is AppUpdateState.ReadyToInstall)
        val apk = File((state as AppUpdateState.ReadyToInstall).apkPath)
        assertTrue("the installable file must exist", apk.exists())
        assertTrue("the installable file's bytes must match the download", apk.readBytes().contentEquals(bytes))
        assertTrue(
            "a .part file was left behind for a later run to mistake as finished",
            downloadDir.listFiles().orEmpty().none { it.name.endsWith(".part") },
        )
    }

    @Test
    fun `a short download fails and leaves nothing in the download directory`() = runTest {
        val bytes = "truncated".toByteArray()
        val api = FakeReleaseApi().apply {
            // Declares more than it actually sends - the transfer is cut short.
            releases = listOf(release("aura-0.0.21.0", assetSize = bytes.size + 100L))
            downloadBytes = bytes
        }
        val downloadDir = tempFolder.newFolder()
        val repo = repository(releaseApi = api, downloadDir = downloadDir)
        repo.check()
        awaitState(repo) { it is AppUpdateState.Available }

        repo.download()
        val state = awaitState(repo) { it is AppUpdateState.Failed || it is AppUpdateState.ReadyToInstall }

        assertTrue("expected Failed, got $state", state is AppUpdateState.Failed)
        assertTrue(
            "reason must explain the short transfer, got ${(state as AppUpdateState.Failed).reason}",
            state.reason.contains("short"),
        )
        assertTrue(
            "a partial file was left for a later run to mistake as finished",
            downloadDir.listFiles().isNullOrEmpty(),
        )
    }

    @Test
    fun `an archive with the wrong package name is refused and never reaches the installer`() = runTest {
        val bytes = "not aura".toByteArray()
        val api = FakeReleaseApi().apply {
            releases = listOf(release("aura-0.0.21.0", assetSize = bytes.size.toLong()))
            downloadBytes = bytes
        }
        val packageFacts = FakePackageFacts(installed).apply {
            archiveResult = ApkIdentity(packageName = "com.example.other", versionName = "0.0.21", versionCode = 21)
        }
        val installer = FakePlatformInstaller()
        val repo = repository(releaseApi = api, packageFacts = packageFacts, installer = installer, downloadDir = tempFolder.newFolder())
        repo.check()
        awaitState(repo) { it is AppUpdateState.Available }

        repo.download()
        val state = awaitState(repo) { it is AppUpdateState.Failed || it is AppUpdateState.ReadyToInstall }

        assertTrue("expected Failed, got $state", state is AppUpdateState.Failed)
        assertEquals(0, installer.installCount)
    }

    @Test
    fun `an archive whose versionCode is not newer is refused as a downgrade`() = runTest {
        val bytes = "stale build".toByteArray()
        val api = FakeReleaseApi().apply {
            releases = listOf(release("aura-0.0.21.0", assetSize = bytes.size.toLong()))
            downloadBytes = bytes
        }
        val packageFacts = FakePackageFacts(installed).apply {
            // Same versionCode as installed - not greater, so it must be refused.
            archiveResult = installed.copy()
        }
        val repo = repository(releaseApi = api, packageFacts = packageFacts, downloadDir = tempFolder.newFolder())
        repo.check()
        awaitState(repo) { it is AppUpdateState.Available }

        repo.download()
        val state = awaitState(repo) { it is AppUpdateState.Failed || it is AppUpdateState.ReadyToInstall }

        assertTrue("expected Failed, got $state", state is AppUpdateState.Failed)
        assertTrue(
            "reason must explain the downgrade, got ${(state as AppUpdateState.Failed).reason}",
            state.reason.contains("not newer"),
        )
    }

    @Test
    fun `a signature mismatch from the installer reaches the user as its own sentence`() = runTest {
        val bytes = "a fine build".toByteArray()
        val api = FakeReleaseApi().apply {
            releases = listOf(release("aura-0.0.21.0", assetSize = bytes.size.toLong()))
            downloadBytes = bytes
        }
        val installer = FakePlatformInstaller().apply { outcome = InstallOutcome.SignatureMismatch }
        val repo = repository(releaseApi = api, installer = installer, downloadDir = tempFolder.newFolder())
        repo.check()
        awaitState(repo) { it is AppUpdateState.Available }
        repo.download()
        awaitState(repo) { it is AppUpdateState.ReadyToInstall }

        repo.install()
        val state = awaitState(repo) { it is AppUpdateState.Failed || it is AppUpdateState.Installing }

        assertTrue("expected Failed, got $state", state is AppUpdateState.Failed)
        assertEquals(InstallOutcome.SignatureMismatch.message, (state as AppUpdateState.Failed).reason)
        assertEquals(1, installer.installCount)
    }

    // ---- fixtures ----

    private fun defaultChannel() = UpdateChannel(
        apiRoot = "https://example.invalid/",
        owner = "acme",
        repo = "aura",
        flavor = "enterprise",
        buildType = "debug",
    )

    private fun TestScope.repository(
        channel: UpdateChannel = defaultChannel(),
        releaseApi: FakeReleaseApi = FakeReleaseApi(),
        packageFacts: FakePackageFacts = FakePackageFacts(installed),
        installer: FakePlatformInstaller = FakePlatformInstaller(),
        downloadDir: File,
    ): AppUpdateRepository = AppUpdateRepository(
        channel = channel,
        releaseApi = releaseApi,
        packageFacts = packageFacts,
        installer = installer,
        downloadDir = downloadDir,
        scope = CoroutineScope(UnconfinedTestDispatcher(testScheduler) + SupervisorJob()),
    )

    private fun release(
        tag: String,
        assetSize: Long = 1024,
        assets: List<ReleaseAssetDto> = listOf(fittingAsset(tag, assetSize)),
        prerelease: Boolean = false,
        draft: Boolean = false,
    ): ReleaseDto = ReleaseDto(tagName = tag, draft = draft, prerelease = prerelease, assets = assets)

    private fun fittingAsset(tag: String, size: Long = 1024): ReleaseAssetDto {
        val version = tag.removePrefix(UpdateChannel.TAG_PREFIX)
        return ReleaseAssetDto(
            name = "aura-$version-enterprise-debug.apk",
            size = size,
            browserDownloadUrl = "https://example.invalid/$version.apk",
        )
    }

    /**
     * Real-time bounded wait for [repo]'s state to satisfy [predicate]. `download()` genuinely hops
     * onto `Dispatchers.IO`, a real thread outside the test scheduler, so this polls wall-clock time
     * rather than driving virtual time - see the class KDoc.
     */
    private suspend fun awaitState(
        repo: AppUpdateRepository,
        timeoutMs: Long = 5_000,
        predicate: (AppUpdateState) -> Boolean,
    ): AppUpdateState = withContext(Dispatchers.Default) {
        val deadline = System.currentTimeMillis() + timeoutMs
        var state = repo.state.value
        while (!predicate(state) && System.currentTimeMillis() < deadline) {
            delay(5)
            state = repo.state.value
        }
        state
    }

    private class FakeReleaseApi : ReleaseApi {
        var releases: List<ReleaseDto> = emptyList()
        var releasesFailure: Throwable? = null
        var releasesCallCount = 0
        var downloadBytes: ByteArray = ByteArray(0)

        override suspend fun releases(owner: String, repo: String, perPage: Int, limit: Int): List<ReleaseDto> {
            releasesCallCount++
            releasesFailure?.let { throw it }
            return releases
        }

        override suspend fun download(url: String): ResponseBody =
            downloadBytes.toResponseBody("application/octet-stream".toMediaType())
    }

    private class FakePackageFacts(private val installedIdentity: ApkIdentity) : PackageFacts {
        /** A plausible newer build of the SAME package, so the happy-path tests need no override -
         * the defect tests each replace this with the specific wrong shape they're pinning. */
        var archiveResult: ApkIdentity? = installedIdentity.copy(versionCode = installedIdentity.versionCode + 1)

        override fun installed(): ApkIdentity = installedIdentity

        override fun archive(file: File): ApkIdentity? = archiveResult
    }

    private class FakePlatformInstaller : PlatformInstaller {
        var installCount = 0
        var outcome: InstallOutcome = InstallOutcome.Succeeded

        override fun canInstallPackages(): Boolean = true

        override fun openInstallPermissionScreen(): Boolean = true

        override suspend fun install(apk: File): InstallOutcome {
            installCount++
            return outcome
        }
    }
}
