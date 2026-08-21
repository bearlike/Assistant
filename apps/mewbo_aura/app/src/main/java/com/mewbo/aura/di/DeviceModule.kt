package com.mewbo.aura.di

import android.app.ActivityManager
import android.app.UiModeManager
import android.content.Context
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.os.Process
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.device.AlarmManagerNextAlarmReader
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.AssistOverlayPresence
import com.mewbo.aura.data.device.BatteryStatusHandler
import com.mewbo.aura.data.device.ContentResolverSmsInboxReader
import com.mewbo.aura.data.device.DeviceActionHandler
import com.mewbo.aura.data.device.DeviceClock
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.DeviceControlStartHandler
import com.mewbo.aura.data.device.DeviceControlStopHandler
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.data.device.DeviceShellHandler
import com.mewbo.aura.data.device.DeviceUiHandler
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
import com.mewbo.aura.data.device.ScreenCaptureVeil
import com.mewbo.aura.ui.control.DeviceControlOverlay
import com.mewbo.aura.data.device.SendSmsHandler
import com.mewbo.aura.data.device.SetAlarmHandler
import com.mewbo.aura.data.device.SetTimerHandler
import com.mewbo.aura.data.device.SmsInboxReader
import com.mewbo.aura.data.device.TelevisionChecker
import com.mewbo.aura.data.device.WakeDeviceHandler
import com.mewbo.aura.data.device.canStartActivityNow
import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import com.mewbo.aura.data.device.shizuku.DeviceShellRunner
import com.mewbo.aura.data.device.shizuku.OverlayPermissionState
import com.mewbo.aura.data.device.shizuku.ShizukuDeviceControl
import com.mewbo.aura.data.device.shizuku.ShizukuOverlayGrant
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.ui.settings.OverlayPermissionReader
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton
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

        /**
         * The app-wide [DeviceShape], resolved ONCE from [TelevisionChecker].
         *
         * `@Singleton` because a device does not stop being a television — the same reason
         * `LocalDeviceShape` is a `staticCompositionLocalOf`. Injected by every reader that is NOT
         * under `MainActivity`'s composition and therefore cannot read that local: the
         * device-control overlay is a raw `WindowManager` view, and `ChatViewModel` is not a
         * composable at all. Reading the local from either would silently return the `Handheld`
         * default with nothing reporting it.
         *
         * **`MainActivity`'s debug `deviceShape` intent override does NOT reach this binding**, and
         * cannot: the override is scoped to one Activity's intent, while this is resolved for the
         * process. So the override still re-shapes the Compose tree for layout and focus work, but
         * anything reading THIS — read-aloud on text turns, the control overlay's narration and its
         * aura rise — follows the real platform feature only.
         */
        @Provides
        @Singleton
        fun provideDeviceShape(checker: TelevisionChecker): DeviceShape = DeviceShape.of(checker)

        /**
         * The two platform reads behind [TelevisionChecker], OR-ed — behind the same narrow seam as
         * [DevicePermissionChecker], so the consumer needs no `PackageManager` in a test.
         *
         * **OR, not either alone, because a false NEGATIVE is the costly direction.**
         * `FEATURE_LEANBACK` is what the leanback storefront filters on; `UI_MODE_TYPE_TELEVISION`
         * is what the device says it currently IS, catching a TV-shaped build that ships the ui
         * mode without the feature. A handheld reports neither, so the OR costs nothing there.
         * Which one Fire TV reports is untested — the OR is what makes that gap survivable.
         */
        @Provides
        fun provideTelevisionChecker(@ApplicationContext context: Context): TelevisionChecker =
            TelevisionChecker {
                val uiMode = context.getSystemService(UiModeManager::class.java)?.currentModeType
                context.packageManager.hasSystemFeature(PackageManager.FEATURE_LEANBACK) ||
                    uiMode == Configuration.UI_MODE_TYPE_TELEVISION
            }

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
        /** The PUSHED status, not a one-shot read — [DeviceControlSession] both
         * gates on it and republishes changes from it, and a snapshot taken at
         * app start is stale the moment the user authorises Shizuku (which
         * happens in another app entirely). */
        @Provides
        fun provideDeviceControlStatusSource(control: ShizukuDeviceControl): DeviceControlStatusSource =
            DeviceControlStatusSource { control.status }

        /** Binding IS the check — `service()` returns null rather than throwing
         * when the bind fails, which is what lets a grant refuse instead of
         * reporting a success the first tool call disproves. */
        @Provides
        fun provideDeviceControlBinder(control: ShizukuDeviceControl): DeviceControlBinder =
            DeviceControlBinder { control.service() != null }

        /** The SAME `IDeviceService.shell` channel `device_shell` reaches, behind
         * a narrow seam so a caller with one fixed internal command need not
         * inject [ShizukuDeviceControl]. A bind failure and a thrown binder call
         * both degrade to `null`, which the caller turns into a refusal naming a
         * remedy — an unreachable service must never take a caller down. */
        @Provides
        fun provideDeviceShellRunner(control: ShizukuDeviceControl): DeviceShellRunner =
            DeviceShellRunner { command, timeoutMs ->
                runCatching { control.service()?.shell(command, timeoutMs) }.getOrNull()
            }

        /** Declared DOWN in `data/device/shizuku/` and implemented in
         * `ui/settings/`, so `data/` never imports `ui/` — the same shape as
         * [ScreenCaptureVeil] ← [DeviceControlOverlay], and this module is the one
         * layer allowed to see both. It is deliberately the same
         * `Settings.canDrawOverlays` read the settings row and
         * `DeviceControlOverlay.raise()` use; a second way to ask could report a
         * grant the window manager still refuses. */
        @Provides
        fun provideOverlayPermissionState(reader: OverlayPermissionReader): OverlayPermissionState =
            OverlayPermissionState { reader.isGranted() }

        /** The package name is read here rather than injected as a qualified
         * `String`, which is the only reason this is a `@Provides` and not an
         * `@Inject constructor`: the class itself must stay constructible on a
         * plain JVM with no `Context`. */
        @Provides
        fun provideShizukuOverlayGrant(
            @ApplicationContext context: Context,
            statusSource: DeviceControlStatusSource,
            shell: DeviceShellRunner,
            overlayState: OverlayPermissionState,
        ): ShizukuOverlayGrant =
            ShizukuOverlayGrant(context.packageName, statusSource, shell, overlayState)

        /** Device control is available only while the Shizuku service is
         * running — which on a non-rooted device means "not after a reboot,
         * until the user re-arms it". Behind the same narrow-seam treatment as
         * [DevicePermissionChecker]/[DeviceToolGate] so `DeviceToolCatalog`
         * stays plain-JVM testable with no Shizuku binder.
         *
         * **Asked of [DeviceControlSession], not of Shizuku directly.** The
         * grant is the one home for "may an agent drive this phone"; the
         * catalog reading the binder itself would be a second, independently
         * derived answer to a question that already has an owner. */
        @Provides
        fun provideDeviceControlGate(session: DeviceControlSession): DeviceControlGate =
            DeviceControlGate { session.canTakeControl() }

        /**
         * The overlay IS the veil — it owns the windows, so it is the only thing that can take
         * them out of a capture and put them back.
         *
         * A pass-through binding rather than `@Binds` on the class itself, because `data/` may not
         * import `ui/`: the seam is declared DOWN
         * ([com.mewbo.aura.data.device.ScreenCaptureVeil]) and implemented UP, and the DI module is
         * the one layer allowed to see both. Same shape as
         * [com.mewbo.aura.data.repo.RunNotifications] ← `notify/RunNotificationLauncher`.
         *
         * Nothing is hidden when no window is up: the implementation returns before touching a
         * window or a dispatcher, which matters because a capture is already the expensive
         * observation and most happen with no overlay on screen at all.
         */
        @Provides
        fun provideScreenCaptureVeil(overlay: DeviceControlOverlay): ScreenCaptureVeil = overlay

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
            deviceUi: DeviceUiHandler,
            deviceAction: DeviceActionHandler,
            deviceShell: DeviceShellHandler,
            controlStart: DeviceControlStartHandler,
            controlStop: DeviceControlStopHandler,
        ): List<@JvmSuppressWildcards DeviceToolHandler> =
            listOf(
                time, battery, setAlarm, setTimer, wake, readLatestSms, sendSms, getNextAlarm, dismissAlarm,
                deviceUi, deviceAction, deviceShell, controlStart, controlStop,
            )

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
