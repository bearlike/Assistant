package com.mewbo.aura

/**
 * True only in the debug variant (release counterpart in `src/release` sets it false). Stands in
 * for `BuildConfig.DEBUG`: `buildFeatures.buildConfig` isn't enabled in `app/build.gradle.kts` and
 * that file is off-limits for this task, so this follows the same per-variant source-set swap
 * already used by `di/VoiceModule.kt`.
 *
 * Lives at the app root, above both `data/` and `ui/`: a build-variant fact is neither, and its
 * previous home in `ui/navigation` made `data/device/shizuku` import UP into `ui/`
 * ([`di/CLAUDE.md`](di/CLAUDE.md)'s seam law forbids exactly that).
 */
const val IS_DEBUG_BUILD = true
