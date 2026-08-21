package com.mewbo.aura.di

import android.content.Context
import android.content.Intent
import com.mewbo.aura.debugtools.DebugTool
import com.mewbo.aura.debugtools.DebugTools
import com.mewbo.aura.ui.speech.SpeechBenchActivity
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * Debug build-type wiring for [DebugTools]: the ONE place a `debug/` Activity class is named, so
 * `ui/settings/` can offer the tool without importing it.
 *
 * The release counterpart binds a no-op that references no `src/debug` class at all — mirroring
 * `MockBackendModule`'s two halves exactly (see [DebugTools] for why the seam exists rather than
 * the screen simply branching on `IS_DEBUG_BUILD`).
 */
@Module
@InstallIn(SingletonComponent::class)
object DebugToolsModule {

    @Provides
    @Singleton
    fun provideDebugTools(): DebugTools = DebugToolLauncher()
}

/**
 * Launches a debug tool's host Activity.
 *
 * `NEW_TASK` is deliberately NOT set: the bench belongs to the task the user is already in, so
 * Back returns them to Settings rather than to the launcher.
 */
class DebugToolLauncher : DebugTools {

    override fun isAvailable(tool: DebugTool): Boolean = when (tool) {
        DebugTool.SpeechBench -> true
    }

    override fun launch(context: Context, tool: DebugTool) {
        val target = when (tool) {
            DebugTool.SpeechBench -> SpeechBenchActivity::class.java
        }
        context.startActivity(Intent(context, target))
    }
}
