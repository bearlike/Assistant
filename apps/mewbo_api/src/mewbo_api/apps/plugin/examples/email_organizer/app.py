"""Email Organizer — dashboard page (the app entrypoint).

Reads the display-ready `task_groups` collection through the injected SDK and
renders one bordered card per group. All grouping/labelling was done by the
`morning-organize` pipeline, so this page only reads and renders.
"""

import mewbo_app
import streamlit as st

st.title("📥 Email Organizer")

try:
    app = mewbo_app.connect()
    groups = app.data.query("task_groups", limit=50)
    runs = app.system.runs(limit=1)
except mewbo_app.AppTokenExpired:
    st.warning("This app's session expired. Refresh the app to reconnect.")
    st.stop()

if runs:
    st.caption(f"Last refreshed: {runs[0].get('ended_at', 'never')}")

if not groups:
    st.info("No task groups yet. The organizer pipeline runs every morning.")

for group in groups:
    with st.container(border=True):
        st.subheader(f"{group['title']}  ·  {group['count']} emails")
        for subject in group.get("subjects", []):
            st.markdown(f"- {subject}")
