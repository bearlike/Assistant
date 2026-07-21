package com.mewbo.aura.data.device

import javax.inject.Inject
import javax.inject.Singleton

/**
 * Whether the assist overlay's window (`voice/AuraSession`, a `TYPE_VOICE_INTERACTION` system
 * window) is currently on screen. A Hilt singleton holding exactly one flag: `AuraSession` sets it
 * in `onShow`, clears it in `onHide` AND `onDestroy`, and `di/DeviceModule`'s [AppForegroundChecker]
 * ORs it with the process-importance check ([canStartActivityNow], whose KDoc carries the
 * AOSP-source evidence for why a showing session both fails that check and is nevertheless allowed
 * to launch an activity).
 *
 * Lives in `data/device/` rather than `voice/` because its ONE consumer is this package's
 * activity-launch guard - `voice/` only writes to it. [visible] is `@Volatile`: the write happens on
 * the session's main thread, the read on whatever coroutine dispatcher a `device_*` handler was
 * dispatched onto (`@ApplicationScope` is `Dispatchers.IO`).
 *
 * **Bias every ambiguous teardown path toward `false`, and clear it on EVERY one.** The two failure
 * directions are not symmetric:
 *
 * - Stale-`true` (window gone, flag still set) is the dangerous one, because a blocked activity
 *   launch is a SILENT no-op: `ActivityStarter` returns `START_ABORTED` internally, but
 *   `getExternalResult()` maps that to `START_SUCCESS` before the caller ever sees it (the only
 *   trace is a platform-side `Slog.wtf`). No exception, no error return - so the handler would
 *   cheerfully report `handed_to_clock_app: true` for an alarm the user never got. That is the exact
 *   lie the guard exists to prevent (review finding F4).
 * - Stale-`false` merely surfaces the honest `app_not_foreground` error the user can see and retry.
 *
 * Hence `onDestroy` clears it as well as `onHide`: a session torn down without a preceding hide (the
 * service killed/unbound) would otherwise leave the flag stuck true forever.
 */
@Singleton
class AssistOverlayPresence @Inject constructor() {
    @Volatile
    var visible: Boolean = false
}
