"""FreshnessStrip — a compact "last refreshed" line from the run ledger.

Reads `app.system.runs()` and shows when the app's data was last updated plus the
latest run's status, so a viewer knows how fresh what they're looking at is.
"""

import mewbo_app
import streamlit as st


# ── FreshnessStrip ────────────────────────────────────────────────
class FreshnessStrip:
    """One caption line summarising the most recent pipeline run."""

    _STATUS_GLYPH = {"succeeded": "🟢", "failed": "🔴", "running": "🟡"}

    def __init__(self, runs):
        self.runs = runs

    def render(self):
        if not self.runs:
            st.caption("No pipeline runs yet.")
            return
        latest = self.runs[0]
        glyph = self._STATUS_GLYPH.get(latest.get("status", ""), "⚪")
        when = latest.get("ended_at") or latest.get("started_at") or "unknown"
        st.caption(f"{glyph} Last refreshed {when} ({latest.get('pipeline_name', 'pipeline')})")
# ── end FreshnessStrip ────────────────────────────────────────────


if __name__ == "__main__":
    app = mewbo_app.connect()
    FreshnessStrip(app.system.runs(limit=5)).render()
