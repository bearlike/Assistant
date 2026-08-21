package com.mewbo.aura.di

import com.mewbo.aura.voice.PlatformSynthesizer
import com.mewbo.aura.voice.SpeechRecognizerTranscriber
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

/**
 * Release wiring: on-device means the platform speech stack. This file lives ONLY in the release
 * build-type source set (its debug counterpart lives at
 * `app/src/debug/java/com/mewbo/aura/di/VoiceModule.kt` and routes through `VoiceBackends`
 * instead) — the two never compile together, so release never sees `FakeTranscriber`/
 * `FakeSynthesizer` on its classpath.
 *
 * **These bindings are [OnDeviceSpeech]-qualified, not the app-wide ones.** The unqualified
 * `Transcriber`/`Synthesizer` every consumer injects come from [SpeechModule], which routes between
 * this leg and the server-backed engines on the user's Settings choice. This module answers only
 * "what does on-device mean in this variant", which is the one question a build type can answer.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class VoiceModule {
    @Binds
    @OnDeviceSpeech
    abstract fun bindTranscriber(impl: SpeechRecognizerTranscriber): Transcriber

    @Binds
    @OnDeviceSpeech
    abstract fun bindSynthesizer(impl: PlatformSynthesizer): Synthesizer
}
