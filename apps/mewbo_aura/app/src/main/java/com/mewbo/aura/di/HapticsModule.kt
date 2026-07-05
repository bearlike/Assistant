package com.mewbo.aura.di

import android.content.Context
import android.os.Vibrator
import android.os.VibratorManager
import com.mewbo.aura.voice.AuraHaptics
import com.mewbo.aura.voice.VibratorAuraHaptics
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * The one place [android.os.Vibrator] is resolved (minSdk 33 always has [VibratorManager], API
 * 31+) - was previously hand-constructed inline in `AuraSession` on every session create;
 * centralizing it here lets [AssistEntryPoint][com.mewbo.aura.voice.AssistEntryPoint] and
 * `@AndroidEntryPoint` hosts (`AssistOverlayPreviewActivity`) share the exact same singleton
 * instead of two separate resolution call sites.
 */
@Module
@InstallIn(SingletonComponent::class)
object HapticsModule {
    @Provides
    @Singleton
    fun provideVibrator(@ApplicationContext context: Context) = runCatching {
        (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
    }.getOrNull()

    @Provides
    @Singleton
    fun provideAuraHaptics(vibrator: Vibrator?): AuraHaptics = VibratorAuraHaptics(vibrator)
}
