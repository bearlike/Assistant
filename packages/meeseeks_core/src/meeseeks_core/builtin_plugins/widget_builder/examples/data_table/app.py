"""Sales report widget — tabular data with column totals."""

import json

import streamlit as st

with open("data.json") as f:
    data = json.load(f)

st.title(data["title"])

header = data["columns"]
rows = data["rows"]

st.dataframe(
    {col: [row[i] for row in rows] for i, col in enumerate(header)},
    use_container_width=True,
)

totals = {header[i]: sum(row[i] for row in rows) for i in range(1, len(header))}
st.markdown("**Totals:** " + " | ".join(f"{k}: ${v:,}" for k, v in totals.items()))
