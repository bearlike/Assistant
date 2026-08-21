package com.mewbo.aura.ui.common

import androidx.compose.runtime.staticCompositionLocalOf
import com.mewbo.aura.data.device.DeviceShape

/**
 * The [DeviceShape] this composition is rendering for, provided once by `MainActivity` from the
 * injected `TelevisionChecker`.
 *
 * `staticCompositionLocalOf` rather than `compositionLocalOf` because a device does not stop being
 * a television: the value is resolved once per process and never changes, so the cheaper local that
 * re-composes its whole subtree on a write is free here and the write never happens.
 *
 * **Defaults to [DeviceShape.Handheld], and that direction is deliberate.** A preview, a test, or a
 * future host that forgets to provide it renders the touch design — which is merely wrong-looking
 * on a television, where the reverse (a remote-shaped tree on a handheld) would hide affordances a
 * finger needs. Tests that assert television behaviour must provide it explicitly, which also makes
 * the assumption visible in the test rather than ambient.
 */
val LocalDeviceShape = staticCompositionLocalOf<DeviceShape> { DeviceShape.Handheld }
