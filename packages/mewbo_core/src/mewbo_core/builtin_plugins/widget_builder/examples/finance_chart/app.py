"""Portfolio summary widget — KPI metrics and holdings list."""

import json

import streamlit as st

with open("data.json") as f:
    data = json.load(f)

st.title(data["title"])

col1, col2 = st.columns(2)
col1.metric("Total Value", f"${data['total_value']:,.2f}")
col2.metric("Day Gain", f"${data['day_gain']:,.2f}")

st.subheader("Holdings")
for h in data["holdings"]:
    color = "green" if h["change_pct"] >= 0 else "red"
    st.markdown(f"**{h['ticker']}** — {h['shares']} shares @ ${h['price']:.2f} :{color}[({h['change_pct']:+.1f}%)]")
