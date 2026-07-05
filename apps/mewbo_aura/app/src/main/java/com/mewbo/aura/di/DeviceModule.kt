package com.mewbo.aura.di

import android.app.ActivityManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Process
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.device.AlarmManagerNextAlarmReader
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.BatteryStatusHandler
import com.mewbo.aura.data.device.ContentResolverSmsInboxReader
import com.mewbo.aura.data.device.DeviceClock
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceTimeHandler
import com.mewbo.aura.data.device.DeviceToolCallHistory
import com.mewbo.aura.data.device.DeviceToolCallLedger
import com.mewbo.aura.data.device.DeviceToolHandler
import com.mewbo.aura.data.device.DeviceToolResultReporter
import com.mewbo.aura.data.device.DismissAlarmHandler
import com.mewbo.aura.data.device.GetNextAlarmHandler
import com.mewbo.aura.data.device.NextAlarmReader
import com.mewbo.aura.data.device.ReadLatestSmsHandler
import com.mewbo.aura.data.device.SendSmsHandler
import com.mewbo.aura.data.device.SetAlarmHandler
import com.mewbo.aura.data.device.SetTimerHandler
import com.mewbo.aura.data.device.SmsInboxReader
import com.mewbo.aura.data.device.WakeDeviceHandler
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent

/** Hilt bindings for `data/device/` (Gitea #179 Phase 2-4). [Context.checkSelfPermission] is the
 * platform API directly (API 23+, well under `minSdk 33`) - no `androidx.core` dependency needed
 * just for this one call. */
@Module
@InstallIn(SingletonComponent::class)
abstract class DeviceModule {

    @Binds
    abstract fun bindDeviceToolCallLedger(impl: DeviceToolCallHistory): DeviceToolCallLedger

    @Binds
    abstract fun bindSmsInboxReader(impl: ContentResolverSmsInboxReader): SmsInboxReader

    @Binds
    abstract fun bindNextAlarmReader(impl: AlarmManagerNextAlarmReader): NextAlarmReader

    companion object {
        @Provides
        fun provideDevicePermissionChecker(@ApplicationContext context: Context): DevicePermissionChecker =
            DevicePermissionChecker { permission ->
                context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
            }

        @Provides
        fun provideDeviceClock(): DeviceClock = DeviceClock { System.currentTimeMillis() / 1000.0 }

        /** `ActivityManager.runningAppProcesses` importance (not `ProcessLifecycleOwner`, not an
         * extra dependency) - the app's OWN process's importance at or above
         * `IMPORTANCE_FOREGROUND` is the same signal Android's own background-activity-launch
         * restriction (API 29+) checks (review finding F4). */
        @Provides
        fun provideAppForegroundChecker(@ApplicationContext context: Context): AppForegroundChecker =
            AppForegroundChecker {
                val activityManager = context.getSystemService(ActivityManager::class.java)
                val myPid = Process.myPid()
                val importance = activityManager?.runningAppProcesses?.firstOrNull { it.pid == myPid }?.importance
                importance != null && importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
            }

        /** Assembles the concrete `device_*` handlers into the plain list
         * [com.mewbo.aura.data.device.DeviceToolExecutor] actually depends on - kept out of the
         * executor's own constructor so its unit tests don't need real (Context-backed) handler
         * instances, only this list shape. */
        @Provides
        fun provideDeviceToolHandlers(
            time: DeviceTimeHandler,
            battery: BatteryStatusHandler,
            setAlarm: SetAlarmHandler,
            setTimer: SetTimerHandler,
            wake: WakeDeviceHandler,
            readLatestSms: ReadLatestSmsHandler,
            sendSms: SendSmsHandler,
            getNextAlarm: GetNextAlarmHandler,
            dismissAlarm: DismissAlarmHandler,
        ): List<@JvmSuppressWildcards DeviceToolHandler> =
            listOf(time, battery, setAlarm, setTimer, wake, readLatestSms, sendSms, getNextAlarm, dismissAlarm)

        /** Never surfaces a thrown exception - the wire contract forbids retrying a result POST
         * regardless of outcome (200/403/404/409/network failure alike are all terminal from the
         * executor's point of view), so any failure here is swallowed rather than propagated. Both
         * response body variants are explicitly closed to avoid leaking the OkHttp connection -
         * `Response<ResponseBody>` (not a converted DTO) is used precisely so no JSON conversion is
         * attempted against a response whose body may legitimately be empty. */
        @Provides
        fun provideDeviceToolResultReporter(api: AuraApi): DeviceToolResultReporter =
            DeviceToolResultReporter { sessionId, callId, request ->
                runCatching {
                    val response = api.postDeviceToolResult(sessionId, callId, request)
                    response.body()?.close()
                    response.errorBody()?.close()
                }
            }
    }
}
