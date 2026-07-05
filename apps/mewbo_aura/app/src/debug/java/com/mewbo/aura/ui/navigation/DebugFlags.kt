package com.mewbo.aura.ui.navigation

/**
 * True only in the debug variant (release counterpart in `src/release` sets it false). Stands in
 * for `BuildConfig.DEBUG`: `buildFeatures.buildConfig` isn't enabled in `app/build.gradle.kts` and
 * that file is off-limits for this task, so this follows the same per-variant source-set swap
 * already used by `di/VoiceModule.kt`.
 */
const val IS_DEBUG_BUILD = true
