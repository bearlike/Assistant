package com.mewbo.aura.data.device.shizuku

/**
 * Which app the screen is actually showing, parsed from `dumpsys window`.
 *
 * **This is a FIELD on the observation, never a tool.** "What am I looking at"
 * is the first thing every screen-driving turn needs and the element list does
 * not answer it: a pruned node carries a package, but repeating one fact on
 * every node is per-node cost for a per-screen truth, and a whole tool schema
 * for it is re-sent at full price on every model call. One pair of keys at the
 * head of the elements result costs neither.
 *
 * **Why the focused window rather than the nodes' own `package`.** The dump
 * mixes the app with whatever else holds a window — the status bar, the
 * navigation bar, an IME, a system dialog — so picking a package out of the
 * node list is a popularity contest that a full-screen system overlay wins.
 * `mCurrentFocus` is the window manager's own answer to the same question.
 *
 * Measured on the dev device: `dumpsys window | grep mCurrentFocus` costs
 * ~0.01s against a ~2.0s element read, so the second command is ~0.5% of an
 * observation. It is worth re-measuring before adding a THIRD.
 */
data class FocusedWindow(val packageName: String, val activity: String?) {

    companion object {
        /** Exposed so a test can assert the TARGET, not just the parse — the
         * same reason [UiSnapshot.DUMP_COMMAND] is public. A suite fed good
         * fixture text stays green forever while the command producing it is
         * wrong. */
        const val DUMP_COMMAND: String = "dumpsys window | grep mCurrentFocus"

        /** ~0.01s measured; the budget is generous because a wedged `dumpsys`
         * must not eat the element read's own share of the dispatch budget. */
        const val TIMEOUT_MS: Long = 3_000L

        /** `mCurrentFocus=Window{a1b2c3 u0 com.example/com.example.MainActivity}`.
         * The user id is matched rather than skipped so the component is read
         * from the right field on a multi-user device. */
        private val FOCUS_RE = Regex("""mCurrentFocus=Window\{\S+\s+u\d+\s+([^\s}]+)\}""")

        /**
         * Parse `dumpsys window | grep mCurrentFocus` output, or `null`.
         *
         * `null` for every case that does not NAME an app: `mCurrentFocus=null`
         * (nothing focused), a bare system window (`StatusBar`,
         * `NavigationBar0` — a window name, not a package), or output the
         * regex does not match. A guess here would be worse than absence: the
         * model treats this as ground truth for which app it is driving, and
         * would launch, tap and type against the wrong one.
         */
        fun parse(output: String): FocusedWindow? {
            val component = FOCUS_RE.find(output)?.groupValues?.get(1) ?: return null
            val slash = component.indexOf('/')
            if (slash <= 0 || slash == component.length - 1) return null
            val packageName = component.substring(0, slash)
            val activity = component.substring(slash + 1)
            // A relative class name (`.Settings`) is expanded so the value can
            // be handed straight back to `am start -n <component>` — which is
            // what the model does with it.
            val qualified = if (activity.startsWith(".")) packageName + activity else activity
            return FocusedWindow(packageName, qualified.takeIf { it.isNotBlank() })
        }
    }
}
