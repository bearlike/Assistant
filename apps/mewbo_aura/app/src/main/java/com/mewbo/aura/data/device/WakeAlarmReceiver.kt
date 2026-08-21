package com.mewbo.aura.data.device

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect

/**
 * Fired by the [AlarmManager.setAlarmClock][android.app.AlarmManager.setAlarmClock] scheduled in
 * [WakeDeviceHandler]. Plays the alarm-stream ringtone and vibrates, bounded to
 * [RING_DURATION_MS] (~10s, task brief) via [goAsync] - the app has no foreground service in this
 * task, so `goAsync`'s pending-result window is what keeps the process alive long enough to run
 * the bounded stop path instead of being reaped the instant [onReceive] returns. No Hilt
 * dependencies: everything here is a self-contained platform-API side effect, nothing to inject.
 */
class WakeAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pendingResult = goAsync()

        val ringtoneUri = RingtoneManager.getActualDefaultRingtoneUri(context, RingtoneManager.TYPE_ALARM)
        val ringtone = ringtoneUri?.let { RingtoneManager.getRingtone(context, it) }?.apply {
            audioAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            play()
        }

        val vibrator = VibratorResolver.resolve(context)
        vibrator?.vibrate(VibrationEffect.createWaveform(VIBRATE_PATTERN, 0))

        Handler(Looper.getMainLooper()).postDelayed({
            ringtone?.stop()
            vibrator?.cancel()
            pendingResult.finish()
        }, RING_DURATION_MS)
    }

    companion object {
        const val EXTRA_REASON = "reason"
        private const val RING_DURATION_MS = 10_000L
        private val VIBRATE_PATTERN = longArrayOf(0, 500, 500)
    }
}
