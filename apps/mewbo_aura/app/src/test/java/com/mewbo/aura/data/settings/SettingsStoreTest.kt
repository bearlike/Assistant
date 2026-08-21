package com.mewbo.aura.data.settings

import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mockito
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

/**
 * [SettingsStore.speakResponses]'s absent-key default, and that a stored choice outranks it.
 *
 * Robolectric rather than plain JVM because `SettingsStore` needs a real `Context` for its
 * DataStore file. [KeystoreCipher] is mocked for the reason
 * [com.mewbo.aura.ui.control.DeviceControlOverlayTest]'s harness mocks it: its constructor opens
 * the `AndroidKeyStore` JCA provider, which Robolectric ships none of, and this path never reaches
 * the cipher.
 *
 * **Why this suite exists even though the default is a constant.** Making the default
 * device-conditional was tried and reverted, and the reason is worth keeping a test on: the read
 * is `it[KEY] ?: default`, which cannot distinguish "never touched this switch" from "explicitly
 * turned it off" — both are absent-or-false shaped at a glance. Anything that makes the fallback
 * cleverer has to keep a PRESENT `false` authoritative, and that is what the last two cases pin.
 * (The default itself was already `true` everywhere, so a shape-conditional version would have
 * changed nothing on a television while silently switching read-aloud OFF for every handheld that
 * had never touched it. What actually leaves a television silent is the synthesizer it resolves
 * to, not this flag.)
 */
@RunWith(RobolectricTestRunner::class)
class SettingsStoreTest {

    private fun store(): SettingsStore = SettingsStore(
        context = RuntimeEnvironment.getApplication(),
        keystoreCipher = Mockito.mock(KeystoreCipher::class.java),
    )

    @Test
    fun `an untouched key speaks by default`() = runBlocking {
        assertTrue(store().speakResponses.first())
    }

    @Test
    fun `an explicit false outranks the default`() = runBlocking {
        val settings = store()
        settings.setSpeakResponses(false)

        assertFalse(
            "a user who deliberately turned this off must not have it silently re-enabled",
            settings.speakResponses.first(),
        )
    }

    @Test
    fun `an explicit true round-trips`() = runBlocking {
        val settings = store()
        settings.setSpeakResponses(true)

        assertTrue(settings.speakResponses.first())
    }
}
