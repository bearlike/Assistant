package com.mewbo.aura.di

import android.content.Context
import com.mewbo.aura.debugtools.DebugTool
import com.mewbo.aura.debugtools.DebugTools
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * Release build-type wiring: [DebugTools] resolves to a permanently-unavailable no-op.
 *
 * **Nothing under `app/src/debug` is referenced here, which is the point** — the bench Activity is
 * not on the release classpath at all, so it is absent from the APK rather than merely unreachable
 * from the UI. Mirrors `MockBackendModule`'s release half exactly; the two `DebugToolsModule`s
 * live in different source sets and never compile together.
 */
@Module
@InstallIn(SingletonComponent::class)
object DebugToolsModule {

    @Provides
    @Singleton
    fun provideDebugTools(): DebugTools = NoOpDebugTools
}

/** Offers nothing and launches nothing. `Settings` renders no row for an unavailable tool, so
 * [launch] is unreachable in practice; it is a no-op rather than a throw because a debug affordance
 * must never be the thing that crashes a release build. */
object NoOpDebugTools : DebugTools {
    override fun isAvailable(tool: DebugTool): Boolean = false
    override fun launch(context: Context, tool: DebugTool) = Unit
}
