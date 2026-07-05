package com.mewbo.aura.ui.chat

import com.mewbo.aura.data.model.ComposerScope

/**
 * [ChatViewModel.bind]'s rebind-vs-no-op decision, extracted into its own dependency-free class
 * (module CLAUDE.md's "one atomic class per feature" paradigm - same shape as
 * [com.mewbo.aura.data.model.TranscriptReducer]) purely so it's directly unit-testable: the other
 * three things [ChatViewModel] constructor-injects (`SessionRepository`/`RunRepository`, and
 * transitively `SettingsStore`/`KeystoreCipher`/Android Keystore) can't be constructed in a plain
 * JVM `app/src/test` unit test (no Robolectric in this module), so a full `ChatViewModel`
 * instantiation test isn't feasible today - this class isolates the one piece of `bind()`'s logic
 * that actually needs a regression test.
 *
 * ONE [ChatViewModel] instance outlives many different session ids: `AuraNavHost`'s "New chat" /
 * "open session from drawer" navigations both reuse the same `NavBackStackEntry` (`launchSingleTop`
 * + `popUpTo(inclusive = true)` on the same `chat` route - `ui/navigation/AuraNavHost.kt`'s "one
 * destination, never a back-stack push" design), and with it this same Hilt-scoped view model,
 * rather than creating a fresh one. A one-shot "bind once ever" guard silently no-ops every call
 * after the first, leaving the view model stuck on whatever session bound first - a send from a
 * "fresh" greeting would steer into that STALE session instead of creating a new one (task integ
 * bug A).
 */
internal class SessionBinding {
    private var isBound = false

    /** The session id [ChatViewModel] is currently bound to - `null` for a fresh/unsaved chat.
     * Also reassigned by [ChatViewModel.send] once a fresh chat's first send creates a real
     * session, which is why this is a plain mutable property rather than [rebindTo]-only. */
    var currentId: String? = null

    /**
     * `true` when [id] is a genuine session switch the caller must reset per-session state and
     * rebind for (including switching TO or FROM a null/fresh-greeting id); `false` only for a
     * redundant re-invocation with the SAME id already bound (e.g. a recomposition retriggering
     * the same `LaunchedEffect(sessionId)` without the key actually changing). Updates [currentId]
     * and the bound flag as a side effect whenever it returns `true`.
     */
    fun rebindTo(id: String?): Boolean {
        if (isBound && id == currentId) return false
        isBound = true
        currentId = id
        return true
    }
}

/**
 * [ChatViewModel.bind]'s composer-scope reseed decision (Gitea #178 W1-A): a new/fresh chat
 * (`id == null`) reseeds [ComposerScope.selectedProjectKey] from the app-wide default project; an
 * existing session's scope is left untouched, so a revisited session keeps the project/tools it was
 * actually created with (commit 3add535) rather than being silently re-scoped to the app default. As
 * of Gitea #185 P5 the user CAN re-pick scope on an idle existing session for the next turn - so this
 * reseed must preserve, never overwrite, that restored scope. Extracted for the same reason
 * [SessionBinding] is its own class - directly unit-testable without a full [ChatViewModel].
 */
internal fun reseedProjectForBind(id: String?, current: ComposerScope, defaultProjectKey: String?): ComposerScope =
    if (id == null) current.copy(selectedProjectKey = defaultProjectKey) else current
