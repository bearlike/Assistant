package com.mewbo.aura.data.settings

import com.mewbo.aura.voice.SpeechVolumeBoost
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mockito
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

/**
 * [SettingsStore.speechVolumeBoostDecibels]: that OFF is what an untouched install gets, and that a
 * chosen level actually survives a write/read.
 *
 * Robolectric for [SettingsStoreTest]'s reason exactly — `SettingsStore` needs a real `Context` for
 * its DataStore file, and [KeystoreCipher] is mocked because its constructor opens the
 * `AndroidKeyStore` JCA provider Robolectric ships none of. **A separate FILE, not a store of its
 * own**: `data/settings/CLAUDE.md`'s one-DataStore-per-file law is about the production delegate,
 * and this suite constructs the same [SettingsStore] class the app does.
 *
 * ## ⚠️ The absent-key case gets ONE method, and it is deliberate
 *
 * `preferencesDataStore` caches the store it constructs, so the FIRST `Context` to touch it fixes
 * the file for the rest of the JVM — Robolectric handing each test a fresh application directory
 * changes nothing. Measured here rather than assumed: this suite's default-value assertion, written
 * as its own `@Test` against a freshly built [SettingsStore], read back the `10` a SIBLING method
 * had persisted (`expected:<0> but was:<10>`). A per-method "untouched install" test is therefore
 * not testing an untouched install; it is testing whichever sibling JUnit happened to run first.
 *
 * The cure is sequence, not isolation: the absent-key read happens FIRST, inside the one method
 * that then does the writing. What still has to hold outside this file is that no other suite in
 * the module writes this key — true by construction, since nothing else knows it exists.
 */
@RunWith(RobolectricTestRunner::class)
class SpeechVolumeBoostSettingsTest {

    private fun store(): SettingsStore = SettingsStore(
        context = RuntimeEnvironment.getApplication(),
        keystoreCipher = Mockito.mock(KeystoreCipher::class.java),
    )

    @Test
    fun `off by default, and every chosen level round-trips`() = runBlocking {
        val settings = store()

        // The absent key. A boost amplifies past what the platform itself will do, so anything
        // other than OFF here would make an untouched install louder than the user ever set it.
        assertEquals(
            "an untouched install must not amplify",
            SpeechVolumeBoost.OFF_DECIBELS,
            settings.speechVolumeBoostDecibels.first(),
        )

        settings.setSpeechVolumeBoostDecibels(10)
        assertEquals(10, settings.speechVolumeBoostDecibels.first())

        settings.setSpeechVolumeBoostDecibels(15)
        assertEquals("a second choice must replace the first, not merge with it", 15, settings.speechVolumeBoostDecibels.first())

        // The one write that looks exactly like the absent key, and is only safe because both mean
        // the same thing — a user who deliberately switched the boost back off.
        settings.setSpeechVolumeBoostDecibels(SpeechVolumeBoost.OFF_DECIBELS)
        assertEquals(SpeechVolumeBoost.OFF_DECIBELS, settings.speechVolumeBoostDecibels.first())

        // Out of range, and persisted verbatim. Deliberate division of labour: `voice/` owns the
        // range, `data/` owns the bytes — duplicating the clamp here is how two ranges drift, and
        // `data/` may not import `voice/` to share the constant anyway. The read side is guarded by
        // SpeechVolumeBoostTest's clamping cases. Last in the sequence because it writes, and every
        // write in this JVM is visible to any later absent-key read.
        settings.setSpeechVolumeBoostDecibels(400)
        assertEquals("the store must not silently rewrite what it was handed", 400, settings.speechVolumeBoostDecibels.first())
        assertEquals(SpeechVolumeBoost.MAX_DECIBELS, SpeechVolumeBoost.clampDecibels(400))
    }
}
