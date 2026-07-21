"""DataTablePage — render any list of documents as a titled, full-width table.

Feed it documents from `app.data.query(...)`. Column order follows the first
document's keys unless you pass an explicit `columns` list.
"""

import mewbo_app
import streamlit as st


# ── DataTablePage ─────────────────────────────────────────────────
class DataTablePage:
    """A titled table over a list of document dicts."""

    def __init__(self, documents, title="Data", columns=None):
        self.documents = documents
        self.title = title
        self.columns = columns

    def render(self):
        st.header(self.title)
        if not self.documents:
            st.info("No data yet.")
            return
        columns = self.columns or list(self.documents[0].keys())
        st.dataframe(
            [{col: doc.get(col, "") for col in columns} for doc in self.documents],
            use_container_width=True,
            hide_index=True,
        )
# ── end DataTablePage ─────────────────────────────────────────────


if __name__ == "__main__":
    app = mewbo_app.connect()
    DataTablePage(app.data.query("emails", limit=200), title="All mail").render()
