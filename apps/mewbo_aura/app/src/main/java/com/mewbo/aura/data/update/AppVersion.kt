package com.mewbo.aura.data.update

/**
 * A dotted numeric version, ordered.
 *
 * This exists because the two sides being compared are not written the same way and never will be.
 * A release tag is `aura-0.0.20.0` — a product prefix and FOUR segments, the last a re-release
 * counter for the same app version. The installed `versionName` is `0.0.20`, and on the enterprise
 * flavor `0.0.20-enterprise`, because that flavor carries a `versionNameSuffix`. Compared as
 * strings, or as three-segment semver, or with the suffix left on, an enterprise build reads as
 * perpetually out of date against a release it is already running.
 *
 * Two rules settle it, and both are deliberate:
 *
 * - **Everything from the first non-numeric character onward is dropped.** That is what makes the
 *   flavor suffix invisible here rather than a special case at each call site.
 * - **The shorter side is zero-padded, never truncated.** `0.0.20` and `0.0.20.0` are the same
 *   version, so a release re-cut as `0.0.20.1` is correctly NEWER than the `0.0.20` installed from
 *   `0.0.20.0`. Truncating to the shorter length would make that re-release invisible, which is
 *   precisely the case a re-release counter exists for.
 */
data class AppVersion(val segments: List<Int>) : Comparable<AppVersion> {

    override fun compareTo(other: AppVersion): Int {
        val width = maxOf(segments.size, other.segments.size)
        for (index in 0 until width) {
            val mine = segments.getOrElse(index) { 0 }
            val theirs = other.segments.getOrElse(index) { 0 }
            if (mine != theirs) return mine.compareTo(theirs)
        }
        return 0
    }

    override fun toString(): String = segments.joinToString(".")

    companion object {
        /**
         * Parse the leading numeric run of [raw], or `null` when there is none.
         *
         * TOTAL by design: the input is a release tag from a server and a `versionName` from the
         * package manager, neither of which this app controls. A tag nobody planned for must make
         * the check report "unreadable" through a `null`, never throw on a screen the user is
         * looking at. A segment too large for an `Int` is treated the same way — unreadable beats
         * a wrapped number that compares wrong.
         */
        fun parse(raw: String): AppVersion? {
            val numeric = raw.trim().takeWhile { it.isDigit() || it == '.' }
            val segments = numeric.split('.')
                .filter { it.isNotEmpty() }
                .map { it.toIntOrNull() ?: return null }
            return if (segments.isEmpty()) null else AppVersion(segments)
        }
    }
}
