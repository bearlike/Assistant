package com.mewbo.aura.ui.settings

import androidx.lifecycle.ViewModel
import com.mewbo.aura.data.update.AppUpdateRepository
import com.mewbo.aura.data.update.AppUpdateState
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.StateFlow

/**
 * The About-app section's view of the updater — a pass-through, and deliberately nothing more.
 *
 * **The lifecycle lives in the `@Singleton` [AppUpdateRepository], not here.** The artifact is
 * around a hundred megabytes: a download owned by this ViewModel would be cancelled the moment the
 * user left Settings and would restart from zero on the way back in. So the state, the job and the
 * install grant all belong above every screen, and this class only forwards.
 *
 * **Do not add state here.** A field on this ViewModel is a field that dies with the screen, which
 * is exactly the failure the singleton was chosen to avoid — and a second copy of "what the updater
 * is doing" would be free to disagree with the first.
 *
 * Cost: every member is `O(1)` dispatch. What the repository does behind [check] and [download] is
 * declared on those methods.
 */
@HiltViewModel
class AppUpdateViewModel @Inject constructor(
    private val repository: AppUpdateRepository,
) : ViewModel() {

    val state: StateFlow<AppUpdateState> = repository.state

    /** What is running right now, as the package manager reports it. Read once: an install
     * replaces the process, so this cannot change under a composition. */
    val installedVersion: String = repository.installedVersionLabel

    fun check() = repository.check()

    fun download() = repository.download()

    fun cancel() = repository.cancel()

    fun install() = repository.install()

    /** Re-read on every ask rather than held: "Install unknown apps" is a special grant changed on
     * a system screen, so there is no callback and a remembered answer goes stale silently. */
    fun canInstallPackages(): Boolean = repository.canInstallPackages()

    /** Send the user to the system screen that grants it.
     *
     * @return `false` when this device exposes no screen for the grant, so the caller can say so
     *   instead of leaving a tap that does nothing.
     */
    fun openInstallPermissionScreen(): Boolean = repository.openInstallPermissionScreen()
}
