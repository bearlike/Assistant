package com.mewbo.aura.data.device

import android.app.AlarmManager
import android.content.Context
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject

/** Production [NextAlarmReader]. `AlarmClockInfo.showIntent`'s creator package is read
 * defensively - the platform generally attaches one, but nothing guarantees every OEM does. */
class AlarmManagerNextAlarmReader @Inject constructor(
    @ApplicationContext private val context: Context,
) : NextAlarmReader {
    override fun nextAlarm(): NextAlarmInfo? {
        val alarmManager = context.getSystemService(AlarmManager::class.java) ?: return null
        val info = alarmManager.nextAlarmClock ?: return null
        return NextAlarmInfo(triggerAtEpochMillis = info.triggerTime, ownerPackage = info.showIntent?.creatorPackage)
    }
}
