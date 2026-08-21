package com.mewbo.aura.di

import android.content.Context
import android.os.Vibrator
import com.mewbo.aura.data.device.VibratorResolver
import com.mewbo.aura.voice.AuraHaptics
import com.mewbo.aura.voice.VibratorAuraHaptics
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * The one place [android.os.Vibrator] is resolved ([VibratorResolver] branches API 31+
 * [android.os.VibratorManager] vs the minSdk-30 fallback) - was previously hand-constructed
 * inline in `AuraSession` on every session create; centralizing it here lets
 * [AssistEntryPoint][com.mewbo.aura.voice.AssistEntryPoint] and `@AndroidEntryPoint` hosts
 * (`AssistOverlayPreviewActivity`) share the exact same singleton instead of two separate
 * resolution call sites.
 */
@Module
@InstallIn(SingletonComponent::class)
object HapticsModule {
    @Provides
    @Singleton
    fun provideVibrator(@ApplicationContext context: Context) = runCatching {
        VibratorResolver.resolve(context)
    }.getOrNull()

    @Provides
    @Singleton
    fun provideAuraHaptics(vibrator: Vibrator?): AuraHaptics = VibratorAuraHaptics(vibrator)
}
