package com.mewbo.aura.di

import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import com.mewbo.aura.voice.VoiceBackends
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * Debug wiring: routes through [VoiceBackends], which switches fake/platform at runtime. This
 * is the debug build-type source set's version of `com.mewbo.aura.di.VoiceModule` — see the
 * release counterpart for why it lives here rather than in `main`.
 */
@Module
@InstallIn(SingletonComponent::class)
object VoiceModule {
    @Provides
    @Singleton
    fun provideTranscriber(backends: VoiceBackends): Transcriber = backends.transcriber

    @Provides
    @Singleton
    fun provideSynthesizer(backends: VoiceBackends): Synthesizer = backends.synthesizer
}
