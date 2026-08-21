package com.mewbo.aura.di

import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import com.mewbo.aura.voice.VoiceBackends
import com.mewbo.aura.voice.VoiceFakesGate
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * Debug wiring: on-device means [VoiceBackends], which switches fake/platform at runtime. This
 * is the debug build-type source set's version of `com.mewbo.aura.di.VoiceModule` — see the
 * release counterpart for why it lives here rather than in `main`.
 *
 * **These bindings are [OnDeviceSpeech]-qualified, not the app-wide ones.** [SpeechModule] routes
 * the unqualified `Transcriber`/`Synthesizer` between this leg and the server-backed engines on the
 * user's Settings choice — so on redroid, where AOSP ships no recognizer and no TTS engine, "on
 * device" still resolves to the scripted fakes exactly as before, and picking a server engine in
 * Settings genuinely exercises the remote path instead.
 */
@Module
@InstallIn(SingletonComponent::class)
object VoiceModule {
    /**
     * The fake/platform switch, read through the ONE [SettingsStore] instance.
     *
     * A lambda over the store rather than a `SettingsStore` injection into `voice/`, matching
     * `SpeechModule.provideSpeechEngineGate`. It is also the fix for a launch crash on real
     * hardware: `VoiceBackends` used to open `aura_settings` itself with its own
     * `preferencesDataStore` delegate, and a second `DataStore` over a file that already has one
     * throws `IllegalStateException` the moment it is read. It survived every emulator because the
     * emulator branch short-circuits before that read — only a real device took the other arm.
     */
    @Provides
    @Singleton
    fun provideVoiceFakesGate(settingsStore: SettingsStore): VoiceFakesGate =
        VoiceFakesGate { settingsStore.voiceUseFakes }

    @Provides
    @Singleton
    @OnDeviceSpeech
    fun provideTranscriber(backends: VoiceBackends): Transcriber = backends.transcriber

    @Provides
    @Singleton
    @OnDeviceSpeech
    fun provideSynthesizer(backends: VoiceBackends): Synthesizer = backends.synthesizer
}
