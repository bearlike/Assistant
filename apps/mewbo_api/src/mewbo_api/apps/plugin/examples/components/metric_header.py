"""MetricHeader — a row of summary numbers at the top of a page.

Feed it a list of (label, value, delta) tuples — usually derived from a single
display-ready summary document your pipeline maintains, not computed here.
"""

import mewbo_app
import streamlit as st


# ── MetricHeader ──────────────────────────────────────────────────
class MetricHeader:
    """A single row of `st.metric` cells."""

    def __init__(self, metrics):
        # metrics: list of (label, value, delta_or_None)
        self.metrics = metrics

    def render(self):
        if not self.metrics:
            return
        columns = st.columns(len(self.metrics))
        for column, (label, value, delta) in zip(columns, self.metrics, strict=True):
            column.metric(label, value, delta)
# ── end MetricHeader ──────────────────────────────────────────────


if __name__ == "__main__":
    app = mewbo_app.connect()
    summary = app.data.query("summary", limit=1)
    row = summary[0] if summary else {}
    MetricHeader(
        [
            ("Unread", row.get("unread", 0), None),
            ("Task groups", row.get("groups", 0), None),
        ]
    ).render()
