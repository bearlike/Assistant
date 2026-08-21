package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import com.mewbo.aura.di.ApplicationScope
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * The answer `device_control_start` returns — never a bare boolean.
 *
 * A boolean has nowhere to put a reason, which is the whole defect this
 * replaces: "off" had four causes, each needing a different action from the
 * person holding the phone, and all four rendered as silence. Every arm carries
 * a [message] written for that person, relayed by the model.
 *
 * [Refused] is a closed sub-union rather than a flat list of arms so a future
 * in-app prompt (`declined_by_user`) lands as one more `data object` and every
 * `when` over refusals fails to compile until it handles it. Nothing about the
 * shape has to change to admit it.
 */
sealed interface DeviceControlGrant {
    /** The discriminator the model reads; snake_case, matching the tool wire. */
    val code: String

    /** Whether the three control tools are callable after this call. */
    val active: Boolean

    /** What the model says to the user — the only signal they get. */
    val message: String

    data object Granted : DeviceControlGrant {
        override val code = "granted"
        override val active = true
        override val message =
            "Device control is active. The screen tools are callable until you call " +
                "device_control_stop, and the user can end it from the notification at any time."
    }

    /** A grant already held — starting again is a no-op, not an error. */
    data object AlreadyActive : DeviceControlGrant {
        override val code = "already_active"
        override val active = true
        override val message = "Device control was already active. Carry on."
    }

    /** A refusal, always naming what would fix it. */
    sealed interface Refused : DeviceControlGrant {
        override val active: Boolean get() = false
    }

    /**
     * No grant is held. **Never returned by [DeviceControlSession.start] — it is
     * the answer to "may this control tool run", not to "may I take control".**
     * It lives in the same union because both questions have the same shape:
     * control is unavailable, and here is what would fix it. The difference is
     * only who acts — this one the model can fix unaided, the rest need the
     * person holding the phone.
     */
    data object NotStarted : Refused {
        override val code = "device_control_not_started"
        override val message =
            "Device control is not active. Call device_control_start first, then retry — it " +
                "returns 'granted', or names what the user must do."
    }

    data object ShizukuNotInstalled : Refused {
        override val code = "shizuku_not_installed"
        override val message =
            "Device control needs the Shizuku app, which is not installed on this phone. " +
                "Tell the user to install Shizuku and start it, then try again."
    }

    /**
     * Installed, service down. The after-every-reboot state on a non-rooted
     * device, so this is ordinary rather than exceptional — say so, or the user
     * reads a recurring normal state as a broken app.
     */
    data object ShizukuNotRunning : Refused {
        override val code = "shizuku_not_running"
        override val message =
            "Shizuku is installed but its service is not running — this is normal after a " +
                "restart. Tell the user to open Shizuku and start it, then try again."
    }

    /**
     * Running, Aura not authorised IN SHIZUKU.
     *
     * Worth naming precisely because the obvious workaround does not work:
     * granting the Android permission with `pm grant` reads back as granted
     * while Shizuku's own server still refuses, since `checkSelfPermission()`
     * asks the SERVER. The OS flag and the authorisation are different facts and
     * only the second one is the gate — so the remedy has to name Shizuku's own
     * UI, not a permission screen.
     */
    data object PermissionDenied : Refused {
        override val code = "permission_denied"
        override val message =
            "Shizuku is running but has not authorised Aura. Tell the user to open Aura's " +
                "Settings and tap the screen-control row, or authorise Mewbo Aura inside " +
                "Shizuku, then try again."
    }
}

/**
 * Whether an agent may drive this phone right now, and everything that fact
 * arms.
 *
 * **Device control is a session, not a property.** It used to be a boolean
 * derived independently in four places at four different moments, so nothing
 * owned it and nothing could explain it: the foreground-service hold was armed
 * from one derivation, the tool list from another, the capability header from a
 * third. Each could be false for a different reason and none could say which.
 * This class is the one home for the fact — the catalog asks it whether control
 * is POSSIBLE, the executor asks it whether control is HELD, and the run seam
 * asks it whether the channel must be held open.
 *
 * Every collaborator is a narrow seam ([DeviceControlStatusSource],
 * [DeviceControlBinder]) for the reason the rest of this package uses them: the
 * state machine is exercised on a plain JVM with no Shizuku binder, no Android
 * framework and no `Context`.
 *
 * **The grant is app-wide, deliberately not per-session.** There is one screen
 * and one shell-UID service; two sessions cannot each hold it. Session-scoping
 * it would push a session id through [DeviceToolCatalog.availableTools], which
 * has no session to hand it, and buys nothing a single owner does not already
 * give.
 */
@Singleton
class DeviceControlSession @Inject constructor(
    private val statusSource: DeviceControlStatusSource,
    private val binder: DeviceControlBinder,
    @ApplicationScope scope: CoroutineScope,
) {
    /** Serialises [start]: it binds, which suspends, and two concurrent tool
     * calls must not both pay a bind or both flip the flag. */
    private val startLock = Mutex()

    /**
     * The INTENT, which needs THREE states because two lose a fact that has a
     * different remedy.
     *
     * A plain held/not-held boolean forces a choice between two wrong
     * behaviours, and both were written and caught by their own tests:
     * derive `held && ready` on each read and a returning binder RESURRECTS a
     * grant whose agent is gone; latch `held` to false on the loss and the
     * REASON is erased, so a control tool refused because Shizuku died reports
     * "not started", sending the model to `start`, which refuses identically —
     * a loop neither party can break.
     *
     * [LOST] keeps the grant dead while remembering that it was alive, which is
     * what lets the refusal name the substrate for as long as the substrate is
     * the problem, and fall back to "start again" once it is not.
     */
    private enum class Intent { NONE, HELD, LOST }

    private val _intent = MutableStateFlow(Intent.NONE)

    init {
        // **Losing the substrate is an EVENT, and only a collector sees an
        // event.** The Shizuku user service is `daemon(false)`, so it dies with
        // its client process; measured on-device, a server restart took the
        // capability away mid-session with a quietly changing tool count as the
        // only symptom.
        //
        // `compareAndSet` so this can only ever demote a LIVE grant: it must not
        // resurrect one [stop] already ended, and it must not race [stop] into
        // reporting a release that did not happen.
        //
        // The synchronous readers do not depend on this having run — they read
        // the intent and the status together, so they are already right in the
        // window before it lands.
        scope.launch {
            statusSource.status().collect {
                if (!it.isReady) _intent.compareAndSet(Intent.HELD, Intent.LOST)
            }
        }
    }

    /**
     * Whether an agent currently holds control, substrate included.
     *
     * A `Flow` rather than a getter because two surfaces outside this package
     * have to REACT to it, not poll it: the foreground-service hold (raised for
     * as long as the grant lives, released with it) and any on-screen
     * indication that the phone is being driven. A grant nothing can observe is
     * a phone under an agent's control with nothing saying so.
     *
     * **A binder death drops this to false without anyone calling [stop].**
     * Measured: restarting the API container took the Shizuku server with it and
     * the capability vanished mid-session, with a quietly changing tool count as
     * the only symptom. A grant that outlives its binder is a toggle that lies,
     * which is the failure this whole seam exists to remove — so it is
     * invalidated, and every reader learns at once.
     */
    val active: Flow<Boolean> =
        combine(statusSource.status(), _intent) { status, intent ->
            intent == Intent.HELD && status.isReady
        }.distinctUntilChanged()

    /**
     * Emits whenever the advertised device-tool set could have changed.
     *
     * Exists because that set is DERIVED and was only ever re-derived on a
     * fetch: after authorising Shizuku, Settings read "Ready" while the composer
     * still read the pre-authorisation tool count until the app was
     * force-stopped. The current value is dropped so a collector gets changes,
     * not an immediate redundant refresh.
     *
     * **Keyed on the STATUS alone, deliberately not on the grant.** A collector
     * re-runs a real fetch — two HTTP calls — so an emission that cannot change
     * the answer is pure cost, and the advertised set does not depend on the
     * grant: [DeviceToolCatalog] gates the control tools on [canTakeControl] and
     * the lifecycle pair on the user's toggles, neither of which moves when a
     * grant starts or ends. **If advertisement ever starts reading the grant —
     * the one-line flip this design leaves open — this must gain `_intent` back
     * in the same change, or the composer silently goes stale again.**
     */
    val changes: Flow<Unit> =
        statusSource.status().drop(1).map { }

    /** Whether control could be taken right now — Shizuku live and authorised.
     * The ADVERTISE-side question; [controlRefusal] is the ANSWER-side one. */
    fun canTakeControl(): Boolean = statusSource.status().value.isReady

    fun isActive(): Boolean = controlRefusal() == null

    /**
     * Why a control tool must be refused right now, or `null` while control is
     * genuinely held.
     *
     * **The two ways it can be unavailable are different facts and must not
     * collapse.** Never started is the model's to fix and it can do so in the
     * same run; the substrate having gone away is the user's, and telling the
     * model to "start first" there sends them both round a loop that cannot
     * terminate — start would refuse for the very same reason.
     *
     * Reusing [DeviceControlGrant.Refused] rather than minting a second
     * vocabulary is what keeps the remedy identical on both seams: a control
     * tool refused because Shizuku died says exactly what `device_control_start`
     * would say about it.
     */
    fun controlRefusal(): DeviceControlGrant.Refused? = when (_intent.value) {
        Intent.NONE -> DeviceControlGrant.NotStarted
        Intent.HELD -> refusalFor(statusSource.status().value)
        // Armed, then the substrate went away. Name the cause for as long as it
        // IS the cause; once Shizuku is back the honest advice is simply to
        // start again, because the reason has stopped being true.
        Intent.LOST -> refusalFor(statusSource.status().value) ?: DeviceControlGrant.NotStarted
    }

    /**
     * Take control, or refuse with what would fix it.
     *
     * Binds the shell-UID service before reporting success, so [Granted] means
     * the channel is live rather than merely permitted — the status says
     * Shizuku WOULD allow a bind, which is not the same fact, and a grant that
     * is discovered to be hollow on the first `device_ui` call is exactly the
     * silent failure this tool exists to remove.
     */
    suspend fun start(): DeviceControlGrant = startLock.withLock {
        if (isActive()) return@withLock DeviceControlGrant.AlreadyActive
        // A grant whose substrate went away leaves the intent set but nothing
        // behind it. Drop it before re-deriving, so a refusal below can never
        // be reported against a state that still claims to hold control.
        _intent.value = Intent.NONE
        val refusal = refusalFor(statusSource.status().value)
        if (refusal != null) return@withLock refusal
        // Ready, but the bind can still fail (the service is a separate
        // app_process Shizuku starts for us). The remedy is the same one the
        // user already has for a down service, so it reports as that rather
        // than inventing an arm nothing can act on differently.
        if (!binder.bind()) return@withLock DeviceControlGrant.ShizukuNotRunning
        _intent.value = Intent.HELD
        DeviceControlGrant.Granted
    }

    /**
     * Release control. Idempotent — [stop] on an inactive grant is a normal
     * call, not an error, because every automatic release path (run terminal,
     * the notification's own Stop, an explicit tool call) can legitimately race
     * the others.
     *
     * @return true if this call is what ended a grant. Keyed on the INTENT, not
     *   on [isActive]: a grant whose binder already died still has a hold and a
     *   notification behind it, and releasing those is exactly what this did.
     */
    fun stop(): Boolean {
        val wasEngaged = _intent.value != Intent.NONE
        _intent.value = Intent.NONE
        return wasEngaged
    }

    private companion object {
        /** Pure status → refusal mapping, so the four-way diagnostic and the
         * four-way refusal can never drift apart. */
        fun refusalFor(status: DeviceControlStatus): DeviceControlGrant.Refused? = when (status) {
            DeviceControlStatus.NotInstalled -> DeviceControlGrant.ShizukuNotInstalled
            DeviceControlStatus.NotRunning -> DeviceControlGrant.ShizukuNotRunning
            DeviceControlStatus.PermissionDenied -> DeviceControlGrant.PermissionDenied
            DeviceControlStatus.Ready -> null
        }
    }
}
