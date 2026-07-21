"""Email Organizer — all-mail page.

A second page (Streamlit auto-discovers files under `pages/`), showing the raw
`emails` collection as a sortable table. Demonstrates that an app is a full,
multi-page surface, not a single card.
"""

import mewbo_app
import streamlit as st

st.header("All mail")

try:
    app = mewbo_app.connect()
    emails = app.data.query("emails", limit=200)
except mewbo_app.AppTokenExpired:
    st.warning("This app's session expired. Refresh the app to reconnect.")
    st.stop()

if not emails:
    st.info("No mail fetched yet.")
    st.stop()

st.dataframe(
    [
        {
            "Received": email.get("received_at", ""),
            "From": email.get("sender", ""),
            "Subject": email.get("subject", ""),
        }
        for email in emails
    ],
    use_container_width=True,
    hide_index=True,
)
