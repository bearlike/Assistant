"""Stock ticker card — Streamlit-native, KISS.

Assembled around `st.metric(chart_data=..., chart_type="area")` — a
single Streamlit primitive that ships the big bold price, colored delta,
and an embedded sparkline together.  No altair, no pandas, no HTML.

Copy-paste contract
-------------------
Everything between ``# ── StockTickerCard ─`` and
``# ── end StockTickerCard ─`` is the reusable block.  Extract with::

    awk '/^# ── StockTickerCard ─/,/^# ── end StockTickerCard ─/' \\
        stock_ticker_card.py >> app.py

Call as ``StockTickerCard(tick_dict).render()``.  One call = one atomic
bordered card; stack several calls for a watchlist.

Preview standalone: ``streamlit run stock_ticker_card.py``.
"""

from __future__ import annotations

from typing import TypedDict

import streamlit as st

# ── StockTickerCard ───────────────────────────────────────────────────────


class StockTick(TypedDict, total=False):
    """State for :class:`StockTickerCard`.

    ``symbol``/``name``/``price``/``previous_close`` are required for a
    meaningful card; everything else is optional and the card hides
    fields that aren't provided.
    """

    symbol: str
    name: str
    price: float
    previous_close: float
    currency: str  # default "USD"
    history: list[float]  # close prices, oldest first, >= 2 for sparkline
    day_high: float
    day_low: float
    volume: int
    market_cap: float


class StockTickerCard:
    """One atomic ticker card: symbol/name, big price+delta metric with
    embedded sparkline, and a small stats row — all inside one border."""

    def __init__(self, tick: StockTick) -> None:
        self.tick = tick

    @staticmethod
    def _fmt(n: float, decimals: int = 2) -> str:
        """1.23K / 4.56M / 7.89B / 1.23T."""
        a = abs(n)
        for suf, thr in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if a >= thr:
                return f"{n / thr:.{decimals}f}{suf}"
        return f"{n:,.0f}" if decimals == 0 else f"{n:,.{decimals}f}"

    def render(self) -> None:
        t = self.tick
        symbol = t.get("symbol", "—")
        name = t.get("name", "")
        price = float(t.get("price", 0.0))
        prev = float(t.get("previous_close", price))
        delta_pct = ((price - prev) / prev * 100) if prev else 0.0
        currency = t.get("currency", "USD")
        history = t.get("history") or []

        with st.container(border=True):
            # Compact header: bold symbol + muted name on one line.
            st.markdown(f"**{symbol}** &nbsp;·&nbsp; {name}" if name else f"**{symbol}**")

            # The headline metric — st.metric handles bold price, colored
            # delta, arrow glyph, AND the inline sparkline via chart_data.
            st.metric(
                label=currency,
                value=f"{price:,.2f}",
                delta=f"{delta_pct:+.2f}%" if prev else None,
                chart_data=history if len(history) >= 2 else None,
                chart_type="area",
                border=False,
            )

            # Stats row — only metrics that have data show up.
            stats = [
                ("Day High", t.get("day_high"), lambda v: f"{v:,.2f}"),
                ("Day Low", t.get("day_low"), lambda v: f"{v:,.2f}"),
                ("Volume", t.get("volume"), lambda v: self._fmt(v, 1)),
                ("Mkt Cap", t.get("market_cap"), lambda v: f"${self._fmt(v, 1)}"),
            ]
            present = [(label, fmt(v)) for label, v, fmt in stats if v is not None]
            if present:
                for col, (label, value) in zip(st.columns(len(present)), present, strict=True):
                    col.metric(label, value)


# ── end StockTickerCard ───────────────────────────────────────────────────


# Canonical demo payloads — outside the copy region; the agent brings its own.
AAPL_TICK: StockTick = {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "currency": "USD",
    "price": 184.25,
    "previous_close": 181.40,
    "history": [178.4, 179.9, 181.1, 180.3, 182.7, 181.4, 184.25],
    "day_high": 185.9,
    "day_low": 180.8,
    "volume": 52_300_000,
    "market_cap": 2_900_000_000_000,
}
NFLX_TICK: StockTick = {
    "symbol": "NFLX",
    "name": "Netflix, Inc.",
    "currency": "USD",
    "price": 402.11,
    "previous_close": 414.00,
    "history": [420.0, 415.3, 411.2, 409.8, 406.5, 414.0, 402.11],
    "day_high": 412.3,
    "day_low": 400.2,
    "volume": 3_200_000,
    "market_cap": 185_000_000_000,
}


if __name__ == "__main__":
    st.set_page_config(page_title="Stock ticker card", layout="centered")
    st.title("Stock ticker card — demo")
    StockTickerCard(AAPL_TICK).render()
    st.write("")
    StockTickerCard(NFLX_TICK).render()
