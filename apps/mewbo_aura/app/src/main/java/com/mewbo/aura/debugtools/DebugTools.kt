package com.mewbo.aura.debugtools

import android.content.Context
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

/**
 * The debug/release-swapped seam for LAUNCHING a debug-only tool from a `main/` surface.
 *
 * It exists for the same reason [com.mewbo.aura.mock.MockBackendFlags] does, and solves the same
 * shape of problem one step further along. The Settings screen lives in `main/` and the tools it
 * offers live in `app/src/debug/`, so the screen cannot name their classes — a release build would
 * not compile, and moving a tool into `main/` to dodge that would ship it in the public APK, which
 * is the one outcome this must prevent.
 *
 * **The row is gated twice, and the two gates are different facts.** `IS_DEBUG_BUILD` hides the
 * Debug section, which is presentation; this seam decides whether the Activity CLASS is on the
 * classpath at all, which is packaging. The release implementation
 * ([com.mewbo.aura.di.DebugToolsModule]'s no-op) references nothing under `src/debug`, so R8 has
 * nothing to strip — the bench is absent rather than merely unreachable.
 *
 * A tool is offered only if [isAvailable] says so, so a variant that does not ship one renders no
 * row rather than a row that does nothing when tapped.
 */
interface DebugTools {

    /** Whether this build carries the tool named by [tool]. Always `false` in release. */
    fun isAvailable(tool: DebugTool): Boolean

    /**
     * Starts [tool]'s host.
     *
     * Takes a [Context] rather than holding one: the launcher is a `@Singleton` and the caller has
     * an Activity, which is the correct context for a start that should belong to the user's
     * current task rather than to the process.
     */
    fun launch(context: Context, tool: DebugTool)
}

/**
 * The debug tools reachable from Settings.
 *
 * An enum rather than a class reference, precisely because a `main/` caller must not be able to
 * name a `debug/` class — the enum is the whole vocabulary the two source sets share.
 */
enum class DebugTool(val label: String, val caption: String) {
    /** The TTS bench: type text, pick a gateway or on-device engine, speak, read the timings. */
    SpeechBench(
        label = "Speech bench",
        caption = "Test text-to-speech against any engine, with timings",
    ),
}

/**
 * Reaches the [DebugTools] binding from a composable.
 *
 * `SettingsScreen` is not an injection site of its own, and a launcher does not belong on
 * `SettingsViewModel` (it is not state, and the ViewModel would have to hold an Activity context to
 * use it). This is the same sanctioned Hilt escape hatch `voice/AssistEntryPoint` uses, and for the
 * same reason: exposing an accessor for a binding the variant modules already provide, never a
 * second `@Provides`.
 */
@EntryPoint
@InstallIn(SingletonComponent::class)
interface DebugToolsEntryPoint {
    fun debugTools(): DebugTools
}
