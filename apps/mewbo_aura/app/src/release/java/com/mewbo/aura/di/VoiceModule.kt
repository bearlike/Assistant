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
 * Release wiring: always the platform speech stack. This file lives ONLY in the release build-
 * type source set (its debug counterpart lives at
 * `app/src/debug/java/com/mewbo/aura/di/VoiceModule.kt` and routes through `VoiceBackends`
 * instead) — the two never compile together, so release never sees `FakeTranscriber`/
 * `FakeSynthesizer` on its classpath.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class VoiceModule {
    @Binds
    abstract fun bindTranscriber(impl: SpeechRecognizerTranscriber): Transcriber

    @Binds
    abstract fun bindSynthesizer(impl: PlatformSynthesizer): Synthesizer
}
