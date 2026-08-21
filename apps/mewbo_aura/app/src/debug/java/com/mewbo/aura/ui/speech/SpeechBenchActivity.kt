package com.mewbo.aura.ui.speech

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import com.mewbo.aura.data.repo.SpeechRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.di.ApplicationScope
import com.mewbo.aura.di.OnDeviceSpeech
import com.mewbo.aura.ui.theme.AuraTheme
import com.mewbo.aura.voice.RemoteSynthesizer
import com.mewbo.aura.voice.SelectedSynthesizer
import com.mewbo.aura.voice.SpeechEngineGate
import com.mewbo.aura.voice.SpeechVolumeBoost
import com.mewbo.aura.voice.Synthesizer
import dagger.hilt.android.AndroidEntryPoint
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow

/**
 * Debug-only host for the TTS bench: type text, pick any engine the gateway advertises OR the
 * on-device one, speak it, and read the latency, the byte count and the failure reason.
 *
 * ```
 * adb -s <serial> shell am start -n com.mewbo.aura/.ui.speech.SpeechBenchActivity
 * ```
 *
 * **What it reuses, and the one thing it substitutes.** The stack under the button is production:
 * [SelectedSynthesizer] routing to either the [OnDeviceSpeech] leg or a [RemoteSynthesizer] over
 * [SpeechRepository]. The single substitution is the [SpeechEngineGate] — a bench-owned
 * [MutableStateFlow] instead of `SettingsStore`, which is what lets the screen pick a model
 * explicitly without moving the user's real setting. That gate is exactly the seam the production
 * code already reads its selection through, so nothing had to be widened to make this possible.
 *
 * **Both readers of the gate had to move together, and missing the second is the trap.**
 * `SelectedSynthesizer` reads it to choose a delegate; `RemoteSynthesizer.pump` reads it AGAIN for
 * the model id it puts on the wire. So a bench that swapped only the router's gate would route to
 * the server leg and then synthesize with whatever Settings held — reporting one engine's name
 * over another engine's latency. Both are constructed here against the same flow.
 *
 * **The instances are bench-local, not the injected singletons**, because a `@Singleton`
 * `RemoteSynthesizer` is already bound to the real gate and cannot be re-pointed. That is also why
 * the on-device leg IS the injected singleton: it holds a `TextToSpeech` engine and an audio-focus
 * request, and a second copy would contend with the app's own read-aloud for both.
 */
@AndroidEntryPoint
class SpeechBenchActivity : ComponentActivity() {

    @Inject @OnDeviceSpeech lateinit var onDeviceSynthesizer: Synthesizer

    @Inject lateinit var speechRepository: SpeechRepository

    @Inject lateinit var settingsStore: SettingsStore

    /** The app-process scope, so a synthesis pump outlives a configuration change the same way it
     * does in production. The bench's own coroutines use `lifecycleScope` instead — they should
     * die with the screen. */
    @Inject @ApplicationScope lateinit var applicationScope: CoroutineScope

    /** The REAL boost singleton, not a bench-local one: it owns a live audio effect, and a second
     * copy would attach a second one alongside read-aloud's. Substituting it would also break this
     * bench's own charter — it substitutes exactly one thing, the [SpeechEngineGate]. */
    @Inject lateinit var speechVolumeBoost: SpeechVolumeBoost

    /** Held so [onDestroy] can barge in on BOTH legs. `SelectedSynthesizer.stop` reaches each
     * delegate, which is the only thing that stops a remote clip already decoding. */
    private lateinit var synthesizer: Synthesizer

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.isNavigationBarContrastEnforced = false

        val selection = MutableStateFlow("")
        val gate = SpeechEngineGate { selection }
        val probe = RecordingSpeechGateway(speechRepository)
        val remote = RemoteSynthesizer(
            context = applicationContext,
            gateway = probe,
            engineGate = gate,
            boost = speechVolumeBoost,
            scope = applicationScope,
        )
        synthesizer = SelectedSynthesizer(
            onDevice = onDeviceSynthesizer,
            remote = remote,
            engineGate = gate,
            scope = applicationScope,
        )
        val bench = SpeechBench(
            synthesizer = synthesizer,
            selection = selection,
            probe = probe,
            loadCatalog = { speechRepository.catalog() },
            // The CLASS, not a description: on AOSP the on-device leg is `FakeSynthesizer`, which
            // logs instead of speaking, and a result of "spoke in 40ms" is unreadable without it.
            onDeviceEngineName = onDeviceSynthesizer::class.simpleName.orEmpty(),
            scope = lifecycleScope,
        )

        setContent {
            // Threaded, never a bare AuraTheme{} — the defaulted parameter silently drops the
            // in-app reduced-motion toggle, which has bitten two hosts here already.
            val reducedMotion by settingsStore.reducedMotion.collectAsState(initial = false)
            AuraTheme(reducedMotion = reducedMotion) {
                val state by bench.state.collectAsState()
                SpeechBenchScreen(
                    state = state,
                    onTextChange = bench::setText,
                    onSelectEngine = bench::selectEngine,
                    onManualEngineChange = bench::setManualEngine,
                    onSpeak = bench::speak,
                    onStop = bench::stop,
                    onReloadCatalog = bench::reloadCatalog,
                )
            }
        }
    }

    /** Barge-in on the way out. The synthesis pump runs on the APPLICATION scope by design, so
     * leaving the screen mid-clip would otherwise keep talking over whatever the user opened next
     * — and the on-device leg is the app-wide singleton, so it would hold audio focus too. */
    override fun onDestroy() {
        super.onDestroy()
        synthesizer.stop()
    }
}
