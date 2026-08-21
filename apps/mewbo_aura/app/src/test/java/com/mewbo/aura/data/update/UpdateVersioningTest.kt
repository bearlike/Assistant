package com.mewbo.aura.data.update

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [AppVersion] ordering/parsing and [UpdateChannel]'s asset/tag matching, in isolation from the
 * repository that composes them. Each claim below is a distinct defect if it regressed — see
 * `AppVersion`'s own KDoc for why zero-padding and the flavor-suffix drop are both deliberate.
 */
class UpdateVersioningTest {

    // ---- AppVersion ----

    @Test
    fun `a shorter version zero-pads rather than truncates, so it compares equal to its padded form`() {
        val short = AppVersion.parse("0.0.20")!!
        val padded = AppVersion.parse("0.0.20.0")!!
        assertEquals(0, short.compareTo(padded))
        assertEquals(0, padded.compareTo(short))
    }

    @Test
    fun `a re-release counter makes the recut version newer, which is the point of padding over truncation`() {
        val recut = AppVersion.parse("0.0.20.1")!!
        val original = AppVersion.parse("0.0.20")!!
        assertTrue(recut > original)
    }

    @Test
    fun `versions compare numerically, not lexicographically`() {
        val v21 = AppVersion.parse("0.0.21")!!
        val v20 = AppVersion.parse("0.0.20")!!
        val v9 = AppVersion.parse("0.0.9")!!
        assertTrue(v21 > v20)
        // A string compare would read "9" > "20" on the last segment; numeric parsing must not.
        assertTrue(v9 < v20)
    }

    @Test
    fun `the enterprise flavor suffix parses to the same version as the bare tag`() {
        val enterprise = AppVersion.parse("0.0.20-enterprise")!!
        val bare = AppVersion.parse("0.0.20")!!
        assertEquals(0, enterprise.compareTo(bare))
    }

    @Test
    fun `a tag with no numeric part at all parses to null rather than throwing`() {
        assertNull(AppVersion.parse("enterprise"))
    }

    @Test
    fun `a segment too large for an Int parses to null, total, never a wrapped number`() {
        assertNull(AppVersion.parse("0.0.99999999999999999999"))
    }

    // ---- UpdateChannel ----

    private fun channel() = UpdateChannel(
        apiRoot = "https://example.invalid/",
        owner = "acme",
        repo = "aura",
        flavor = "enterprise",
        buildType = "debug",
    )

    private fun asset(name: String, url: String = "https://example.invalid/$name") =
        ReleaseAssetDto(name = name, size = 1024, browserDownloadUrl = url)

    @Test
    fun `fits accepts the new versioned asset naming scheme`() {
        assertTrue(channel().fits(asset("aura-0.0.21-enterprise-debug.apk")))
    }

    @Test
    fun `fits accepts the legacy asset naming scheme so already-published releases stay visible`() {
        assertTrue(channel().fits(asset("app-enterprise-debug.apk")))
    }

    @Test
    fun `fits rejects an asset built for the wrong flavor`() {
        // A public-flavor APK does not trust the deployment CA on an enterprise device.
        assertTrue(!channel().fits(asset("aura-0.0.21-public-debug.apk")))
    }

    @Test
    fun `fits rejects an asset built for the wrong build type`() {
        // A release-signed APK is a different key than a debug-signed one.
        assertTrue(!channel().fits(asset("aura-0.0.21-enterprise-release.apk")))
    }

    @Test
    fun `fits rejects an asset with a blank download url`() {
        assertTrue(!channel().fits(asset("aura-0.0.21-enterprise-debug.apk", url = "")))
    }

    @Test
    fun `releaseVersion parses an Aura-tagged release`() {
        val release = ReleaseDto(tagName = "aura-0.0.20.0")
        assertEquals(AppVersion.parse("0.0.20.0"), channel().releaseVersion(release))
    }

    @Test
    fun `releaseVersion returns null for the server's own release tags`() {
        // The tag namespace is shared with the server; "v0.0.12" must not read as an app version.
        val release = ReleaseDto(tagName = "v0.0.12")
        assertNull(channel().releaseVersion(release))
    }
}
