package com.mewbo.aura.data.device

/** Plain-Kotlin projection of `AlarmManager.AlarmClockInfo` - deliberately NOT the raw platform
 * type, whose `showIntent: PendingIntent` field can't be hand-doubled in a plain-JVM test any more
 * than `android.net.Uri` can (apps/mewbo_aura/CLAUDE.md). */
data class NextAlarmInfo(val triggerAtEpochMillis: Long, val ownerPackage: String?)

/** Seam over `AlarmManager.getNextAlarmClock()` so [GetNextAlarmHandler]'s exists/none formatting
 * is unit-testable without a real `AlarmManager`. */
fun interface NextAlarmReader {
    /** `null` when there is no alarm scheduled system-wide. */
    fun nextAlarm(): NextAlarmInfo?
}
