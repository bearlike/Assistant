package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.model.SessionSummary

/** Backend `SessionOrigin.MOBILE` value (see `data/CLAUDE.md` session-provenance contract). */
const val MOBILE_ORIGIN = "mobile"

/** Drawer/recents scope toggle: every session, or only ones that originated on this device. */
enum class RecentsFilter { MOBILE_ONLY, ALL }

/**
 * Pure predicate over [SessionSummary.origin] — a `null` origin (pre-provenance sessions, or any
 * surface that never tagged one) is NOT mobile, so [RecentsFilter.MOBILE_ONLY] excludes it.
 */
fun RecentsFilter.matches(session: SessionSummary): Boolean = when (this) {
    RecentsFilter.ALL -> true
    RecentsFilter.MOBILE_ONLY -> session.origin?.equals(MOBILE_ORIGIN, ignoreCase = true) == true
}
