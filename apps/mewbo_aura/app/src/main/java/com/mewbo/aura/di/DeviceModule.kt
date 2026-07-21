package com.mewbo.aura.di

import android.app.ActivityManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Process
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.device.AlarmManagerNextAlarmReader
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.AssistOverlayPresence
import com.mewbo.aura.data.device.BatteryStatusHandler
import com.mewbo.aura.data.device.ContentResolverSmsInboxReader
import com.mewbo.aura.data.device.DeviceClock
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceTimeHandler
import com.mewbo.aura.data.device.DeviceToolCallHistory
import com.mewbo.aura.data.device.DeviceToolCallLedger
import com.mewbo.aura.data.device.DeviceToolDispatch
import com.mewbo.aura.data.device.DeviceToolExecutor
import com.mewbo.aura.data.device.DeviceToolGate
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
import com.mewbo.aura.data.device.canStartActivityNow
import com.mewbo.aura.data.settings.SettingsStore
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.flow.first

/** Hilt bindings for `data/device/`. [Context.checkSelfPermission] is the
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

    /** The ONE place [DeviceToolExecutor] is wired to anything: `RunRepository` depends on the
     * narrow [DeviceToolDispatch] and attaches it inside `live()`, so every path that follows a run
     * services device tool calls by construction (that interface's KDoc has the regression this
     * came out of). Nothing else may inject the executor. */
    @Binds
    abstract fun bindDeviceToolDispatch(impl: DeviceToolExecutor): DeviceToolDispatch

    companion object {
        @Provides
        fun provideDevicePermissionChecker(@ApplicationContext context: Context): DevicePermissionChecker =
            DevicePermissionChecker { permission ->
                context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
            }

        @Provides
        fun provideDeviceClock(): DeviceClock = DeviceClock { System.currentTimeMillis() / 1000.0 }

        /** the per-tool Settings toggle read behind the narrow [DeviceToolGate]
         * seam (the reason it exists: keeps `DeviceToolCatalog`/`DeviceToolExecutor` plain-JVM
         * unit-testable, same as [DevicePermissionChecker]). `first()` is the current set - DataStore
         * caches it in memory after the first read, so this is a cheap suspend on every `/query`
         * advertisement and every dispatch. */
        @Provides
        fun provideDeviceToolGate(settingsStore: SettingsStore): DeviceToolGate =
            DeviceToolGate { settingsStore.disabledDeviceToolIds.first() }

        /** `ActivityManager.runningAppProcesses` importance (not `ProcessLifecycleOwner`, not an
         * extra dependency) is the same signal Android's own background-activity-launch restriction
         * (API 29+) checks (review finding F4) - but it is only HALF the question. The decision
         * itself, and why the assist overlay counts as launch-permitted even though its process
         * importance says otherwise, lives in [canStartActivityNow]; this provider only reads the
         * two platform-side inputs it needs. */
        @Provides
        fun provideAppForegroundChecker(
            @ApplicationContext context: Context,
            overlayPresence: AssistOverlayPresence,
        ): AppForegroundChecker =
            AppForegroundChecker {
                val activityManager = context.getSystemService(ActivityManager::class.java)
                val myPid = Process.myPid()
                val importance = activityManager?.runningAppProcesses?.firstOrNull { it.pid == myPid }?.importance
                canStartActivityNow(processImportance = importance, assistOverlayVisible = overlayPresence.visible)
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
