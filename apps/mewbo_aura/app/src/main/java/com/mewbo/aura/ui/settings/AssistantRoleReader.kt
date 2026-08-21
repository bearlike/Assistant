package com.mewbo.aura.ui.settings

import android.app.role.RoleManager
import android.content.Context
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Reads whether Mewbo currently holds Android's assistant role.
 *
 * **`RoleManager` is the only public answer to this question**, and it is the answer the platform
 * itself uses — the shell's own role state names the same holder the secure `assistant` setting
 * does. Everything else on offer is a hidden settings key, which apps targeting recent Android
 * versions may be refused outright, silently, at read time.
 *
 * So the read is guarded on both ends. A device with no `RoleManager` at all, or one whose role
 * lookup throws, yields [AssistantRole.Unknown] rather than a default of "not set" — the row then
 * says it does not know, which is the only honest thing it can say. `isRoleAvailable` is checked
 * first for the same reason: a device that does not carry the assistant role cannot be reasoned
 * about as though it simply has not been set.
 *
 * Constructor-injected, so Hilt supplies it with no module of its own.
 */
@Singleton
class AssistantRoleReader @Inject constructor(@ApplicationContext private val context: Context) {

    /** O(1) — one binder call to the role service. Safe to call on every resume. */
    fun read(): AssistantRole {
        val roles = context.getSystemService(RoleManager::class.java) ?: return AssistantRole.Unknown
        return runCatching {
            when {
                !roles.isRoleAvailable(RoleManager.ROLE_ASSISTANT) -> AssistantRole.Unknown
                roles.isRoleHeld(RoleManager.ROLE_ASSISTANT) -> AssistantRole.Active
                else -> AssistantRole.Inactive
            }
        }.getOrDefault(AssistantRole.Unknown)
    }
}
