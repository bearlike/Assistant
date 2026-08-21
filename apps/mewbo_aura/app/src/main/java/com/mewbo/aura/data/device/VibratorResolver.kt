package com.mewbo.aura.data.device

import android.content.Context
import android.os.Build
import android.os.Vibrator
import android.os.VibratorManager

/**
 * The one place [Vibrator] resolution branches on API level. [VibratorManager] is API 31+; at
 * minSdk 30 there is no manager and the lookup falls back to the deprecated `VIBRATOR_SERVICE`
 * key. Shared by `WakeAlarmReceiver` (this package, deliberately no Hilt) and `HapticsModule`
 * (`di/`, which may import `data/device/` but never the reverse) so the two call sites cannot
 * diverge on the branch.
 */
object VibratorResolver {
    fun resolve(context: Context): Vibrator? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
}
