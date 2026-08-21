package com.mewbo.aura.data.device.shizuku

import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.IBinder
import com.mewbo.aura.IS_DEBUG_BUILD
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.resume
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import rikka.shizuku.Shizuku

/**
 * Owns the app's one connection to the shell-UID device service.
 *
 * A `@Singleton` because the binding is the expensive part and the whole
 * latency argument for this design is that it is paid ONCE per session rather
 * than per action. Every handler shares this instance.
 *
 * **Every call is gated on binder liveness.** Calling through a dead Shizuku
 * binder throws `IllegalStateException`, and the binder dies whenever the
 * Shizuku service stops — which on a non-rooted device is every reboot. So
 * "not available" is a normal, frequent state here, reported as a status
 * rather than raised as an error.
 */
@Singleton
class ShizukuDeviceControl @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val bindLock = Mutex()
    @Volatile private var service: IDeviceService? = null
    @Volatile private var geometry: DisplayGeometry? = null

    /**
     * The live status, pushed rather than polled.
     *
     * **The binder arrives ASYNCHRONOUSLY**, some time after the app process
     * starts. A one-shot read at screen-composition time therefore reports
     * `NotRunning` for a service that is running perfectly well, and nothing
     * ever corrects it — the user sees "Start Shizuku" for a service they
     * already started, which is exactly the wrong instruction. The sticky
     * listener replays a binder that arrived BEFORE we subscribed, so there is
     * no race between app start and this registration either.
     */
    private val _status = MutableStateFlow(readStatus())
    val status: StateFlow<DeviceControlStatus> = _status.asStateFlow()

    init {
        // Sticky: fires immediately if the binder is already here.
        runCatching { Shizuku.addBinderReceivedListenerSticky { refresh() } }
        runCatching {
            Shizuku.addBinderDeadListener {
                // The service died — drop the bound handles with it, or the
                // next call goes out on a dead binder and throws.
                service = null
                geometry = null
                refresh()
            }
        }
        runCatching { Shizuku.addRequestPermissionResultListener { _, _ -> refresh() } }
    }

    /** Re-read the status and publish it. Cheap; safe to call often. */
    fun refresh() {
        _status.value = readStatus()
    }

    /** Why device control is or is not usable right now — a four-way
     * diagnostic Settings renders, never a boolean. */
    fun readStatus(): DeviceControlStatus {
        if (!isShizukuInstalled()) return DeviceControlStatus.NotInstalled
        val alive = runCatching { Shizuku.pingBinder() }.getOrDefault(false)
        if (!alive) return DeviceControlStatus.NotRunning
        val granted = runCatching {
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        }.getOrDefault(false)
        return if (granted) DeviceControlStatus.Ready else DeviceControlStatus.PermissionDenied
    }

    /**
     * Ask Shizuku for access, and report whether the dialog can still appear.
     *
     * Returns `false` when Shizuku will no longer show its prompt — the user
     * denied it with "don't ask again", so the request is a silent no-op and
     * the caller must send them somewhere they CAN grant it. A tap that does
     * nothing and says nothing is the bug this return value exists to prevent.
     */
    fun requestPermission(requestCode: Int): Boolean {
        val canPrompt = runCatching {
            when {
                Shizuku.isPreV11() -> false
                Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED -> false
                Shizuku.shouldShowRequestPermissionRationale() -> false
                else -> {
                    Shizuku.requestPermission(requestCode)
                    true
                }
            }
        }.getOrDefault(false)
        refresh()
        return canPrompt
    }

    /**
     * The bound service, binding on first use, or `null` when unavailable.
     *
     * Bind failures degrade to `null` rather than throwing: the caller turns
     * that into a structured error the model can act on, and an unavailable
     * device must never take the run down.
     */
    suspend fun service(): IDeviceService? {
        service?.let { if (runCatching { it.asBinder().pingBinder() }.getOrDefault(false)) return it }
        if (!readStatus().isReady) return null
        return bindLock.withLock {
            service?.let { if (runCatching { it.asBinder().pingBinder() }.getOrDefault(false)) return it }
            bind().also { service = it }
        }
    }

    /** True screen geometry, read once per binding. The model never sees
     * pixels, but index→centre resolution needs the real resolution. */
    suspend fun geometry(): DisplayGeometry? {
        geometry?.let { return it }
        val info = runCatching { service()?.displayInfo() }.getOrNull() ?: return null
        val (size, density) = info.split(":").let {
            (it.getOrNull(0) ?: return null) to (it.getOrNull(1)?.toIntOrNull() ?: 0)
        }
        val parts = size.split("x")
        val width = parts.getOrNull(0)?.toIntOrNull() ?: return null
        val height = parts.getOrNull(1)?.toIntOrNull() ?: return null
        return DisplayGeometry(width, height, density).also { geometry = it }
    }

    private suspend fun bind(): IDeviceService? = withTimeoutOrNull(BIND_TIMEOUT_MS) {
        suspendCancellableCoroutine { continuation ->
            val connection = object : ServiceConnection {
                override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                    val bound = binder
                        ?.takeIf { it.pingBinder() }
                        ?.let { IDeviceService.Stub.asInterface(it) }
                    if (continuation.isActive) continuation.resume(bound)
                }

                override fun onServiceDisconnected(name: ComponentName?) {
                    service = null
                    geometry = null
                }
            }
            runCatching { Shizuku.bindUserService(userServiceArgs(), connection) }
                .onFailure { if (continuation.isActive) continuation.resume(null) }
        }
    }

    /**
     * `daemon(false)` is the load-bearing argument: it defaults to `true` for
     * backward compatibility, which leaves the shell-UID service alive after
     * the app dies until something explicitly removes it. A surface that can
     * drive the phone should stop when the app that owns it stops.
     *
     * `version` is the upgrade seam — bumping it destroys and restarts an old
     * service, so a shipped APK never talks to a stale one.
     */
    private fun userServiceArgs(): Shizuku.UserServiceArgs =
        Shizuku.UserServiceArgs(
            ComponentName(context.packageName, DeviceUserService::class.java.name),
        )
            .daemon(false)
            .processNameSuffix("device")
            .debuggable(IS_DEBUG_BUILD)
            .version(appVersionCode())

    /** The installed version code, read from the package manager — `BuildConfig`
     * is not generated in this project (see `IS_DEBUG_BUILD`, the per-variant
     * const that stands in for `BuildConfig.DEBUG`). */
    private fun appVersionCode(): Int =
        runCatching {
            context.packageManager.getPackageInfo(context.packageName, 0).longVersionCode.toInt()
        }.getOrDefault(1)

    private fun isShizukuInstalled(): Boolean =
        runCatching {
            context.packageManager.getPackageInfo(SHIZUKU_PACKAGE, 0)
            true
        }.getOrDefault(false)

    private companion object {
        const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"

        /** Well inside the 30s dispatch budget — a bind that has not landed by
         * now is a service that is not coming. */
        const val BIND_TIMEOUT_MS = 8_000L
    }
}
