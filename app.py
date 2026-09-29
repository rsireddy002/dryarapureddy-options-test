"""
dryarapureddy-options
----------------------
Standalone Streamlit app: pick any NSE F&O symbol (stock or index), pick an
expiry and a strike, and see that strike's CE (Call) and PE (Put) candlestick
charts side by side.

Auth: same pattern as your other apps -- a plain Upstox access token stored
in Streamlit secrets (UPSTOX_ACCESS_TOKEN), refreshed manually by you each
day. No OAuth login flow here, same as your other dashboards.

Password gate: same _check_password() pattern used on all your other apps,
reading APP_PASSWORD from secrets.
"""

import os
import time
import gzip
import io
import json
import concurrent.futures
from datetime import datetime, timedelta

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

st.set_page_config(page_title="Dr Yarapu Reddy Options CE/PE", layout="wide")

# Refresh model: same three-tier pattern as dryarapureddy-live (Run
# Precompute / Refresh Zones / Refresh Quotes -- see the UI section near
# the bottom of this file). A full-page st_autorefresh across all 200+
# symbols was tried earlier and abandoned (any interval short enough to
# feel "live" re-triggered a full cold prefetch before the previous one
# finished, so the page never stopped restarting). The optional
# auto-refresh checkbox below only re-runs the cheap "Refresh Quotes"
# tier (one batched LTP call), which is safe to run every 30s.

# ---------------------------------------------------------------------------
# Password gate (same pattern as your other apps -- do not delete the
# _pw_input session_state key inside the callback, that reintroduces the
# re-fire bug we fixed earlier).
# ---------------------------------------------------------------------------
def _check_password():
    def _password_entered():
        _correct = None
        try:
            _correct = st.secrets.get("APP_PASSWORD")
        except Exception:
            _correct = None
        if _correct and st.session_state.get("_pw_input") == _correct:
            st.session_state["_pw_ok"] = True
        else:
            st.session_state["_pw_ok"] = False

    if st.session_state.get("_pw_ok"):
        return True

    st.text_input(
        "Password", type="password", key="_pw_input", on_change=_password_entered,
    )
    if st.session_state.get("_pw_ok") is False:
        st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()

# ---------------------------------------------------------------------------
# Upstox access token
# ---------------------------------------------------------------------------
try:
    TOKEN = st.secrets.get("UPSTOX_ACCESS_TOKEN")
except Exception:
    TOKEN = None

if not TOKEN:
    st.error("UPSTOX_ACCESS_TOKEN is not set in Secrets. Add it in Settings -> Secrets.")
    st.stop()

HEADERS = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}

# ---------------------------------------------------------------------------
# Small 429-aware GET wrapper (same fix pattern used on dryarapureddy-ML)
# ---------------------------------------------------------------------------
def _get_with_backoff(url, params=None, timeout=20, max_retries=4, base_delay=1.5):
    resp = None
    for attempt in range(max_retries):
        resp = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        if resp.status_code != 429:
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
        time.sleep(delay)
    return resp


# ---------------------------------------------------------------------------
# Underlying universe: major indices + full NSE F&O stock list, resolved to
# Upstox instrument_keys via the published NSE instrument master (cached).
# ---------------------------------------------------------------------------
INDEX_SYMBOLS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
}

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"


@st.cache_data(ttl=6 * 60 * 60, show_spinner="Loading instrument master...")
def load_fno_stock_universe():
    """Loads Upstox's instrument master ONCE (cached 6h) and returns:
      - symbols: sorted list of NSE F&O stock underlyings, using each
        stock's own TRADING SYMBOL (e.g. "RELIANCE", "HDFCBANK") -- same
        convention as SECTOR_MAP below and your other apps' EQUITY_SYMBOLS
        list, not the long company name.
      - symbol_to_key: dict mapping that trading symbol -> its NSE_EQ
        instrument_key, needed to call the option-chain API.
    The "name" (company name) column is the one field shared between a
    stock's NSE_EQ (equity) row and its NSE_FO (options) rows, so it's used
    internally to link the two -- but tradingsymbol is what's shown/used.
    """
    try:
        resp = requests.get(INSTRUMENTS_URL, timeout=60)
        resp.raise_for_status()
        raw = gzip.decompress(resp.content)
        df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    except Exception as exc:
        st.warning(f"Couldn't load the full F&O stock list from Upstox ({exc}); "
                   f"showing indices only for now.")
        return [], {}

    required_cols = {"exchange", "instrument_type", "instrument_key", "name", "tradingsymbol"}
    if not required_cols.issubset(df.columns):
        st.warning("Instrument master is missing expected columns; showing indices only for now.")
        return [], {}

    eq = df[df["exchange"] == "NSE_EQ"].dropna(subset=["name"])
    name_to_key = dict(zip(eq["name"], eq["instrument_key"]))
    name_to_tradingsymbol = dict(zip(eq["name"], eq["tradingsymbol"]))

    fo = df[(df["exchange"] == "NSE_FO") & (df["instrument_type"] == "OPTSTK")]
    fo_names = set(fo["name"].dropna().unique())

    # Only keep F&O names we could actually resolve to an equity instrument_key.
    symbol_to_key = {}
    for name in fo_names:
        key = name_to_key.get(name)
        tsym = name_to_tradingsymbol.get(name)
        if key and tsym:
            symbol_to_key[tsym] = key

    symbols = sorted(symbol_to_key.keys())
    return symbols, symbol_to_key


# ---------------------------------------------------------------------------
# Sector grouping -- copied verbatim from dryarapureddy-sectors' app.py
# (SECTOR_MAP), so this app groups stocks the same way your other apps do.
# Best-effort NSE-style categorization, not a formal index classification.
# ---------------------------------------------------------------------------
SECTOR_MAP = {
    # Banks
    "HDFCBANK": "Banks", "ICICIBANK": "Banks", "SBIN": "Banks",
    "KOTAKBANK": "Banks", "AXISBANK": "Banks", "INDUSINDBK": "Banks",
    "BANDHANBNK": "Banks", "BANKBARODA": "Banks", "PNB": "Banks",
    "CANBK": "Banks", "IDFCFIRSTB": "Banks", "FEDERALBNK": "Banks",
    "RBLBANK": "Banks", "AUBANK": "Banks", "BANKINDIA": "Banks",
    "UNIONBANK": "Banks", "CUB": "Banks", "YESBANK": "Banks",

    # NBFC / Financial Services
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "CHOLAFIN": "NBFC",
    "MANAPPURAM": "NBFC", "MUTHOOTFIN": "NBFC", "LICHSGFIN": "NBFC",
    "MFSL": "NBFC", "PFC": "NBFC", "RECLTD": "NBFC", "SBICARD": "NBFC",
    "ABCAPITAL": "NBFC", "CANFINHOME": "NBFC", "SAMMAANCAP": "NBFC",
    "L&TFH": "NBFC", "M&MFIN": "NBFC", "LTF": "NBFC", "HUDCO": "NBFC",
    "IIFL": "NBFC", "MOTILALOFS": "NBFC", "ANGELONE": "NBFC",
    "JIOFIN": "NBFC", "SHRIRAMFIN": "NBFC", "HDFCAMC": "NBFC",
    "PAYTM": "NBFC", "POLICYBZR": "NBFC", "PIRAMALFIN": "NBFC",

    # Insurance
    "SBILIFE": "Insurance", "HDFCLIFE": "Insurance", "ICICIGI": "Insurance",
    "ICICIPRULI": "Insurance", "STARHEALTH": "Insurance", "GICRE": "Insurance",

    # Financial Infra / Exchanges
    "BSE": "Financial Infra", "CDSL": "Financial Infra", "IEX": "Financial Infra",

    # IT
    "TCS": "IT", "INFY": "IT", "HCLTECH": "IT", "WIPRO": "IT", "TECHM": "IT",
    "LTM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "OFSS": "IT", "NAUKRI": "IT", "BSOFT": "IT", "TATAELXSI": "IT",
    "INDIAMART": "IT",

    # Auto & Ancillaries
    "MARUTI": "Auto", "M&M": "Auto", "TMPV": "Auto", "EICHERMOT": "Auto",
    "HEROMOTOCO": "Auto", "BAJAJ-AUTO": "Auto", "TVSMOTOR": "Auto",
    "ASHOKLEY": "Auto", "MOTHERSON": "Auto", "BHARATFORG": "Auto",
    "BALKRISIND": "Auto", "MRF": "Auto", "APOLLOTYRE": "Auto",
    "EXIDEIND": "Auto", "ESCORTS": "Auto", "SONACOMS": "Auto",
    "TIINDIA": "Auto", "ZFCVINDIA": "Auto",

    # Pharma & Healthcare
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "AUROPHARMA": "Pharma", "BIOCON": "Pharma",
    "LUPIN": "Pharma", "TORNTPHARM": "Pharma", "ALKEM": "Pharma",
    "GLENMARK": "Pharma", "GRANULES": "Pharma", "LAURUSLABS": "Pharma",
    "IPCALAB": "Pharma", "ZYDUSLIFE": "Pharma", "MANKIND": "Pharma",
    "SYNGENE": "Pharma", "PFIZER": "Pharma",
    "APOLLOHOSP": "Healthcare", "FORTIS": "Healthcare", "MAXHEALTH": "Healthcare",
    "LALPATHLAB": "Healthcare", "METROPOLIS": "Healthcare",

    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "TATACONSUM": "FMCG",
    "BRITANNIA": "FMCG", "NESTLEIND": "FMCG", "DABUR": "FMCG",
    "MARICO": "FMCG", "COLPAL": "FMCG", "GODREJCP": "FMCG",
    "MCDOWELL-N": "FMCG", "UBL": "FMCG", "VBL": "FMCG", "PATANJALI": "FMCG",
    "JUBLFOOD": "FMCG", "GODFRYPHLP": "FMCG",

    # Metals & Mining
    "TATASTEEL": "Metals & Mining", "JSWSTEEL": "Metals & Mining",
    "HINDALCO": "Metals & Mining", "ADANIENT": "Metals & Mining",
    "VEDANTA": "Metals & Mining", "VEDL": "Metals & Mining",
    "NMDC": "Metals & Mining", "NATIONALUM": "Metals & Mining",
    "HINDCOPPER": "Metals & Mining", "SAIL": "Metals & Mining",
    "JINDALSTEL": "Metals & Mining", "APLAPOLLO": "Metals & Mining",

    # Oil & Gas / Energy
    "RELIANCE": "Oil & Gas", "ONGC": "Oil & Gas", "COALINDIA": "Oil & Gas",
    "GAIL": "Oil & Gas", "BPCL": "Oil & Gas", "IOC": "Oil & Gas",
    "HINDPETRO": "Oil & Gas", "PETRONET": "Oil & Gas", "OIL": "Oil & Gas",
    "IGL": "Oil & Gas", "MGL": "Oil & Gas",

    # Power
    "NTPC": "Power", "POWERGRID": "Power", "TATAPOWER": "Power",
    "TORNTPOWER": "Power", "NHPC": "Power", "SJVN": "Power",
    "CGPOWER": "Power",

    # Capital Goods & Defence
    "LT": "Capital Goods", "SIEMENS": "Capital Goods", "CUMMINSIND": "Capital Goods",
    "BHEL": "Capital Goods", "BEL": "Capital Goods", "HAL": "Capital Goods",
    "POLYCAB": "Capital Goods", "KEI": "Capital Goods", "SOLARINDS": "Capital Goods",
    "POWERINDIA": "Capital Goods", "GRAPHITE": "Capital Goods",
    "SUZLON": "Capital Goods", "ITI": "Capital Goods",

    # Cement & Construction Materials
    "ULTRACEMCO": "Cement", "GRASIM": "Cement", "SHREECEM": "Cement",
    "AMBUJACEM": "Cement", "DALBHARAT": "Cement", "JKCEMENT": "Cement",
    "INDIACEM": "Cement",

    # Chemicals
    "PIDILITIND": "Chemicals", "UPL": "Chemicals", "SRF": "Chemicals",
    "DEEPAKNTR": "Chemicals", "ATUL": "Chemicals", "GNFC": "Chemicals",
    "NAVINFLUOR": "Chemicals", "AARTIIND": "Chemicals", "TATACHEM": "Chemicals",
    "PIIND": "Chemicals", "ASTRAL": "Chemicals", "RAIN": "Chemicals",
    "SUPREMEIND": "Chemicals",

    # Consumer Durables
    "TITAN": "Consumer Durables", "ASIANPAINT": "Consumer Durables",
    "HAVELLS": "Consumer Durables", "VOLTAS": "Consumer Durables",
    "CROMPTON": "Consumer Durables", "DIXON": "Consumer Durables",
    "WHIRLPOOL": "Consumer Durables", "BATAINDIA": "Consumer Durables",
    "PGEL": "Consumer Durables",

    # Telecom
    "BHARTIARTL": "Telecom", "INDUSTOWER": "Telecom", "IDEA": "Telecom",
    "HFCL": "Telecom", "TATACOMM": "Telecom",

    # Realty
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty",
    "PRESTIGE": "Realty", "LODHA": "Realty",

    # Media & Entertainment
    "ZEEL": "Media", "SUNTV": "Media", "PVRINOX": "Media",

    # Retail / Consumer Services
    "TRENT": "Retail", "ETERNAL": "Retail", "DMART": "Retail",
    "NYKAA": "Retail", "PAGEIND": "Retail", "ABFRL": "Retail",
    "KALYANKJIL": "Retail",

    # Aviation / Logistics
    "INDIGO": "Aviation & Logistics", "CONCOR": "Aviation & Logistics",
    "GMRAIRPORT": "Aviation & Logistics", "DELHIVERY": "Aviation & Logistics",

    # Hotels & Travel
    "IRCTC": "Hotels & Travel", "INDHOTEL": "Hotels & Travel",

    # Construction & Infra
    "IRB": "Construction & Infra", "NBCC": "Construction & Infra",
    "NCC": "Construction & Infra", "RVNL": "Construction & Infra",
    "TITAGARH": "Construction & Infra",

    # Agri & Fertilizers
    "CHAMBLFERT": "Agri & Fertilizers", "COROMANDEL": "Agri & Fertilizers",

    # PSU Financial (rail/infra financing, distinct enough from private NBFC)
    "IRFC": "PSU Financial",

    # Diversified / Services
    "ADANIPORTS": "Diversified / Services",
}

OTHER_SECTOR = "Other / Unclassified"


# ---------------------------------------------------------------------------
# Option contracts / chain
# ---------------------------------------------------------------------------
@st.cache_data(ttl=15 * 60, show_spinner=False)
def get_expiries(underlying_key):
    """Upstox's option/contract endpoint returns every live contract for the
    underlying, each carrying its expiry -- collect the distinct dates."""
    url = "https://api.upstox.com/v2/option/contract"
    resp = _get_with_backoff(url, params={"instrument_key": underlying_key})
    if resp is None or resp.status_code != 200:
        return []
    data = resp.json().get("data", [])
    expiries = sorted({c["expiry"] for c in data if c.get("expiry")})
    return expiries


@st.cache_data(ttl=2 * 60, show_spinner=False)
def get_option_chain(underlying_key, expiry_date):
    url = "https://api.upstox.com/v2/option/chain"
    resp = _get_with_backoff(url, params={"instrument_key": underlying_key, "expiry_date": expiry_date})
    if resp is None or resp.status_code != 200:
        return []
    return resp.json().get("data", [])


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
@st.cache_data(ttl=45, show_spinner=False)
def fetch_intraday_candles(instrument_key, unit="minutes", interval="5"):
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
    resp = _get_with_backoff(url)
    if resp is None:
        return pd.DataFrame()
    if resp.status_code != 200:
        return pd.DataFrame()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp")


@st.cache_data(ttl=15 * 60, show_spinner=False)
def fetch_recent_candles(instrument_key, unit="minutes", interval="5", lookback_days=5):
    """Historical (not intraday) candles for the last few days -- used as a
    fallback when today has no intraday candles yet (before market open,
    or on a weekend/holiday), so the chart still shows something useful."""
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    resp = _get_with_backoff(url)
    if resp is None or resp.status_code != 200:
        return pd.DataFrame()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp")


def candles_with_fallback(instrument_key, interval):
    """Today's intraday candles, or the last trading session's candles if
    today has none (weekend/holiday/pre-market). Returns (df, is_fallback)."""
    df = fetch_intraday_candles(instrument_key, interval=interval)
    if not df.empty:
        return df, False
    df = fetch_recent_candles(instrument_key, interval=interval)
    if not df.empty:
        # keep only the most recent trading day present in the data
        last_day = df["timestamp"].dt.date.max()
        df = df[df["timestamp"].dt.date == last_day]
    return df, True


def _clean_candles(df):
    """Drops candle rows with impossible OHLC values -- a stray 0 or NaN,
    seen on illiquid option strikes before their first real trade, or from
    a bad/partial API response. Left uncleaned, a single ~0 price forces
    Plotly's y-axis to span from ~0 up to the real price range, making that
    one candle's wick look like it swallows the entire chart and squashing
    every other candle into an unreadable sliver at the top."""
    if df.empty:
        return df
    mask = (
        (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)
        & df["open"].notna() & df["high"].notna() & df["low"].notna() & df["close"].notna()
        & (df["high"] >= df["low"])
    )
    return df[mask]


def _lock_price_range(fig, df, axis="yaxis"):
    """Explicitly pins the y-axis to the candle data's own high/low (with a
    little padding), so a support/resistance line far outside today's actual
    range (common with an 18-day high/low composite vs. a much tighter
    intraday range) can no longer force Plotly to auto-expand the axis and
    squash the real candles into a thin band. A line that falls outside this
    range simply won't be drawn -- which is the right call, since a level
    price never approached today isn't worth losing chart resolution over."""
    if df.empty:
        return
    y_low, y_high = df["low"].min(), df["high"].max()
    pad = (y_high - y_low) * 0.08 or max(y_high * 0.01, 1)
    fig.update_layout(**{axis: dict(range=[y_low - pad, y_high + pad])})


def candlestick_fig(df, title, underlying_support=None, underlying_resistance=None, underlying_spot=None):
    """Candlestick of the option's own premium (left axis). The underlying's
    18-day support/resistance (and current spot) are drawn as dashed lines
    against an invisible secondary right-hand axis, since the underlying's
    price scale (e.g. 1300s) has nothing to do with the premium's (e.g. 10s-100s)."""
    df = _clean_candles(df)
    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=title,
        ))

    has_underlying_levels = underlying_support is not None and underlying_resistance is not None
    if has_underlying_levels:
        # Invisible secondary-axis trace purely so the axis (and its hover
        # values) exist -- the lines themselves are added via add_hline below.
        x_anchor = [df["timestamp"].iloc[0]] if not df.empty else [datetime.now()]
        fig.add_trace(go.Scatter(
            x=x_anchor, y=[underlying_spot if underlying_spot is not None else underlying_resistance],
            mode="markers", marker=dict(size=0.1, color="rgba(0,0,0,0)"),
            yaxis="y2", showlegend=False, hoverinfo="skip",
        ))
        fig.add_hline(y=underlying_resistance, yref="y2", line_dash="dash", line_color="crimson",
                       annotation_text=f"Underlying R {round(underlying_resistance, 2)}", annotation_position="top right")
        fig.add_hline(y=underlying_support, yref="y2", line_dash="dash", line_color="seagreen",
                       annotation_text=f"Underlying S {round(underlying_support, 2)}", annotation_position="bottom right")
        if underlying_spot is not None:
            fig.add_hline(y=underlying_spot, yref="y2", line_dash="dot", line_color="gray",
                          annotation_text=f"Spot {round(underlying_spot, 2)}", annotation_position="top left")

    layout_kwargs = dict(title=title, xaxis_rangeslider_visible=False, height=420,
                          margin=dict(l=10, r=10, t=40, b=10))
    if has_underlying_levels:
        layout_kwargs["yaxis2"] = dict(overlaying="y", side="right", title="Underlying", showgrid=False)
    fig.update_layout(**layout_kwargs)
    # The underlying's S/R lines live on yaxis2 (secondary), so they can't
    # stretch THIS premium axis -- but a bad candle (pre-cleaning) or a
    # naturally huge intraday premium swing still could, so lock it to the
    # option's own range for the same readability reason as underlying_fig.
    _lock_price_range(fig, df, axis="yaxis")
    return fig


def underlying_fig(df, title, support=None, resistance=None, spot=None):
    """Candlestick of the underlying itself (its own price scale, so the
    18-day S/R lines sit directly on the same primary axis -- no secondary-
    axis trick needed here, unlike candlestick_fig above where the option's
    premium and the underlying's price are on totally different scales).
    The axis is explicitly locked to the candle data's own range (see
    _lock_price_range) so a support/resistance level far outside today's
    actual price action doesn't force the whole chart to zoom out and
    squash today's candles into a thin, unreadable band."""
    df = _clean_candles(df)
    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=title,
        ))
    if resistance is not None:
        fig.add_hline(y=resistance, line_dash="dash", line_color="crimson",
                      annotation_text=f"R {round(resistance, 2)}", annotation_position="top right")
    if support is not None:
        fig.add_hline(y=support, line_dash="dash", line_color="seagreen",
                      annotation_text=f"S {round(support, 2)}", annotation_position="bottom right")
    if spot is not None:
        fig.add_hline(y=spot, line_dash="dot", line_color="gray",
                      annotation_text=f"Spot {round(spot, 2)}", annotation_position="top left")
    fig.update_layout(title=title, xaxis_rangeslider_visible=False, height=420,
                       margin=dict(l=10, r=10, t=40, b=10))
    _lock_price_range(fig, df, axis="yaxis")
    return fig


def combined_symbol_fig(symbol, atm_strike, underlying_df, ce_df, pe_df,
                         support=None, resistance=None, spot=None):
    """The idea behind this view: one figure, three stacked panels (underlying
    / CE / PE) that all share the same time axis, so a move in the underlying
    and the corresponding CE/PE reaction line up vertically -- instead of two
    separate CE/PE charts side by side with no underlying candles at all
    (candlestick_fig above only drew the underlying's S/R as reference lines
    on the option panels, never the underlying's own candles). Zooming or
    panning any one panel (drag on the x-axis) moves all three together,
    since shared_xaxes=True links them.

    Row order top-to-bottom: underlying spot, CE, PE -- so the top panel is
    always "what actually happened to the stock/index", and the two below it
    show how that strike's Call and Put reacted."""
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        row_heights=[0.4, 0.3, 0.3],
        subplot_titles=(
            f"{symbol} (underlying)",
            f"{atm_strike} CE",
            f"{atm_strike} PE",
        ),
    )

    def _add_candles(df, row):
        if df.empty:
            return
        fig.add_trace(
            go.Candlestick(
                x=df["timestamp"], open=df["open"], high=df["high"],
                low=df["low"], close=df["close"], showlegend=False,
            ),
            row=row, col=1,
        )

    _add_candles(underlying_df, 1)
    _add_candles(ce_df, 2)
    _add_candles(pe_df, 3)

    # Support/resistance/spot lines belong on the underlying's own panel now
    # (row 1) -- they're on the underlying's price scale, which only that
    # panel uses, unlike candlestick_fig's old secondary-axis workaround.
    if resistance is not None:
        fig.add_hline(y=resistance, line_dash="dash", line_color="crimson",
                      annotation_text=f"R {round(resistance, 2)}", annotation_position="top right",
                      row=1, col=1)
    if support is not None:
        fig.add_hline(y=support, line_dash="dash", line_color="seagreen",
                      annotation_text=f"S {round(support, 2)}", annotation_position="bottom right",
                      row=1, col=1)
    if spot is not None:
        fig.add_hline(y=spot, line_dash="dot", line_color="gray",
                      annotation_text=f"Spot {round(spot, 2)}", annotation_position="top left",
                      row=1, col=1)

    fig.update_layout(
        height=760, margin=dict(l=10, r=10, t=40, b=10), showlegend=False,
    )
    # rangeslider defaults to on for every candlestick row -- only the
    # bottom-most one needs it (it's shared across all three via
    # shared_xaxes), and even that stays off here since a 3-panel synced
    # view is already busy enough without one.
    fig.update_xaxes(rangeslider_visible=False)
    return fig


# ---------------------------------------------------------------------------
# LTP (for ATM strike selection) -- ONE batched call for every underlying at
# once, not one call per symbol. This is the same trick dryarapureddy-live's
# fast "Refresh Quotes" tier uses (fetch_batch_quotes there) -- it's why that
# app's refresh feels instant while a per-symbol version doesn't: 210+
# separate HTTP round trips vs. 1-2 batched ones is the whole difference.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def get_batch_ltp(instrument_keys_tuple):
    """instrument_keys_tuple must be a tuple (not list) so st.cache_data can
    hash it as a cache key. Upstox's LTP endpoint accepts a comma-separated
    instrument_key list in one call; chunked defensively since very large
    universes may exceed a practical URL/response size in one request."""
    result = {}
    keys = list(instrument_keys_tuple)
    chunk_size = 200
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        url = "https://api.upstox.com/v2/market-quote/ltp"
        resp = _get_with_backoff(url, params={"instrument_key": ",".join(chunk)})
        if resp is None or resp.status_code != 200:
            continue
        data = resp.json().get("data", {})
        for _, v in data.items():
            key = v.get("instrument_token")
            if key:
                result[key] = v.get("last_price")
    return result


def pick_atm_strike(strikes, ltp):
    if not strikes or ltp is None:
        return strikes[len(strikes) // 2] if strikes else None
    return min(strikes, key=lambda s: abs(s - ltp))


def pick_nearest_expiry(expiries):
    """Nearest (soonest) expiry -- the most-traded/liquid one, used since
    there's no expiry dropdown in the auto view."""
    today = datetime.now().date()
    future = [e for e in expiries if datetime.strptime(e, "%Y-%m-%d").date() >= today]
    return sorted(future)[0] if future else sorted(expiries)[0]


# ---------------------------------------------------------------------------
# 18-day composite support/resistance, computed on the UNDERLYING's own
# daily candles (support/resistance are price levels of the stock/index
# itself, not of an option's premium). Shown as reference info alongside
# each symbol's ATM CE/PE charts.
#
# NOTE: this is a standard 18-trading-day high/low + pivot composite, not
# necessarily byte-for-byte identical to whatever exact formula your other
# apps use -- if you want it to match exactly, share that function and I'll
# swap this out for it.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=15 * 60, show_spinner=False)
def fetch_daily_candles(instrument_key, lookback_days=40):
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}"
    resp = _get_with_backoff(url)
    if resp is None or resp.status_code != 200:
        return pd.DataFrame()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp")


def compute_18day_sr(daily_df, lookback=18):
    """Composite support/resistance from the last `lookback` trading days:
      - resistance = highest high, support = lowest low over that window
      - pivot = average of (that resistance, that support, latest close)
    Returns (support, resistance, pivot) or (None, None, None)."""
    if daily_df.empty:
        return None, None, None
    window = daily_df.tail(lookback)
    resistance = window["high"].max()
    support = window["low"].min()
    last_close = daily_df["close"].iloc[-1]
    pivot = (resistance + support + last_close) / 3
    return support, resistance, pivot


# ---------------------------------------------------------------------------
# Three-tier refresh -- same pattern as dryarapureddy-live:
#
#   Run Precompute (slow, once/day)   resolve expiry/chain/strikes/ATM/CE-PE
#                                      instrument keys + 18-day underlying S/R
#                                      for EVERY symbol, then fetch each one's
#                                      first CE/PE candle set. Stored into
#                                      st.session_state so it survives reruns.
#   Refresh Zones (medium)            re-fetch CE/PE candles only, reusing the
#                                      keys Precompute already resolved -- no
#                                      expiry/chain network calls at all.
#   Refresh Quotes (fast)             ONE batched LTP call; just updates the
#                                      displayed spot/ATM strike, candles
#                                      untouched. This is the tier that should
#                                      feel instant.
#
# Nothing in the render section below makes a network call -- it only reads
# whatever these three actions last stored in st.session_state. That's what
# makes switching sector tabs / the interval radio instant instead of
# re-triggering a 200+ symbol fetch every time.
# ---------------------------------------------------------------------------
def resolve_symbol_static(symbol, underlying_key):
    """The SLOW, rarely-changing part of one symbol: expiry, chain, strikes,
    18-day underlying S/R. Only ever called from Run Precompute."""
    expiries = get_expiries(underlying_key)
    if not expiries:
        return None
    expiry = pick_nearest_expiry(expiries)
    chain = get_option_chain(underlying_key, expiry)
    if not chain:
        return None
    strikes = sorted({r["strike_price"] for r in chain if r.get("strike_price") is not None})
    if not strikes:
        return None
    daily_df = fetch_daily_candles(underlying_key)
    support, resistance, _ = compute_18day_sr(daily_df)
    return {
        "underlying_key": underlying_key,
        "expiry": expiry,
        "chain": chain,
        "strikes": strikes,
        "support": support,
        "resistance": resistance,
    }


def fetch_symbol_candles(entry, ltp, interval):
    """Re-picks the ATM strike/CE-PE keys from an already-resolved chain
    (no network call) and fetches those two candle series, PLUS the
    underlying's own candles (three network calls total now instead of two)
    -- the underlying series is what makes the combined three-panel chart
    possible, since candlestick_fig previously only drew the underlying's
    S/R as reference lines, never its actual candles. Used by both Run
    Precompute (first fill) and Refresh Zones (re-fill)."""
    atm_strike = pick_atm_strike(entry["strikes"], ltp)
    row = next((r for r in entry["chain"] if r.get("strike_price") == atm_strike), None)
    ce_key = (row.get("call_options") or {}).get("instrument_key") if row else None
    pe_key = (row.get("put_options") or {}).get("instrument_key") if row else None
    ce_df, ce_fallback = candles_with_fallback(ce_key, interval) if ce_key else (pd.DataFrame(), False)
    pe_df, pe_fallback = candles_with_fallback(pe_key, interval) if pe_key else (pd.DataFrame(), False)
    underlying_df, underlying_fallback = candles_with_fallback(entry["underlying_key"], interval)
    return {
        "atm_strike": atm_strike, "ce_key": ce_key, "pe_key": pe_key,
        "ce_df": ce_df, "ce_fallback": ce_fallback,
        "pe_df": pe_df, "pe_fallback": pe_fallback,
        "underlying_df": underlying_df, "underlying_fallback": underlying_fallback,
    }


def render_symbol_block(symbol, entry, ltp):
    """Pure render -- reads only what's already in `entry` (one symbol's
    slot in st.session_state['options_precomputed']). No network calls."""
    st.markdown(f"### {symbol}")
    if entry is None:
        st.info(f"{symbol}: not loaded yet -- click **Run Precompute** above.")
        st.divider()
        return

    expiry = entry.get("expiry")
    atm_strike = entry.get("atm_strike")
    support = entry.get("support")
    resistance = entry.get("resistance")

    info_bits = [f"Expiry: {expiry}", f"ATM strike: {atm_strike}"]
    if ltp is not None:
        info_bits.insert(0, f"Spot: {ltp}")
    if support is not None:
        info_bits.append(f"Underlying 18d S/R: {round(support, 2)} / {round(resistance, 2)}")
    st.caption(" · ".join(info_bits))
    st.caption("Three separate views, side by side: underlying, CE, and PE -- each with its own "
               "support/resistance/spot lines.")

    underlying_df = entry.get("underlying_df", pd.DataFrame())
    ce_df = entry.get("ce_df", pd.DataFrame())
    pe_df = entry.get("pe_df", pd.DataFrame())

    col_u, col_ce, col_pe = st.columns(3)
    with col_u:
        st.plotly_chart(
            underlying_fig(underlying_df, f"{symbol} (underlying)", support, resistance, ltp),
            width="stretch",
        )
        if underlying_df.empty:
            st.caption("No underlying candle data available yet -- try Refresh Zones.")
        elif entry.get("underlying_fallback"):
            st.caption(f"Last session ({underlying_df['timestamp'].dt.date.max()}) -- no candles today yet.")
    with col_ce:
        if entry.get("ce_key"):
            st.plotly_chart(
                candlestick_fig(ce_df, f"{atm_strike} CE", support, resistance, ltp),
                width="stretch",
            )
            if ce_df.empty:
                st.caption("No CE candle data available yet -- try Refresh Zones.")
            elif entry.get("ce_fallback"):
                st.caption(f"Last session ({ce_df['timestamp'].dt.date.max()}) -- no candles today yet.")
        else:
            st.warning("No CE contract at ATM strike.")
    with col_pe:
        if entry.get("pe_key"):
            st.plotly_chart(
                candlestick_fig(pe_df, f"{atm_strike} PE", support, resistance, ltp),
                width="stretch",
            )
            if pe_df.empty:
                st.caption("No PE candle data available yet -- try Refresh Zones.")
            elif entry.get("pe_fallback"):
                st.caption(f"Last session ({pe_df['timestamp'].dt.date.max()}) -- no candles today yet.")
        else:
            st.warning("No PE contract at ATM strike.")
    st.divider()


# ---------------------------------------------------------------------------
# Load the committed cache written by github_precompute.py's scheduled
# GitHub Action, if present -- same pattern as dryarapureddy-live's
# sahi_zones_cache.json. Lets the app open with today's data already
# loaded, without anyone clicking Run Precompute. Only tried once per
# session, and only if nothing's been loaded into session_state yet (a
# button click always wins over the on-disk cache).
# ---------------------------------------------------------------------------
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_zones_cache.json")


def _records_to_df(records):
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records)
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    return out


def load_cache_from_disk():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r") as f:
            raw = json.load(f)
    except Exception:
        return None
    precomputed = {}
    for symbol, entry in raw.get("precomputed", {}).items():
        entry = dict(entry)
        entry["ce_df"] = _records_to_df(entry.get("ce_df", []))
        entry["pe_df"] = _records_to_df(entry.get("pe_df", []))
        # underlying_df/underlying_fallback are only present in caches written
        # by a github_precompute.py new enough to fetch the underlying's own
        # candles -- older cached JSON (or a cache from before that fetch was
        # added) simply won't have this key, so .get(..., []) degrades to an
        # empty underlying chart instead of crashing on a missing key.
        entry["underlying_df"] = _records_to_df(entry.get("underlying_df", []))
        precomputed[symbol] = entry
    return {
        "precomputed": precomputed,
        "ltp_map": raw.get("ltp_map", {}),
        "generated_at": raw.get("generated_at"),
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("CE / PE Charts -- ATM, auto")
st.caption("Same three-tier refresh as Dr Yarapu Reddy Levels: Precompute once, "
           "Refresh Zones every few minutes, Refresh Quotes for an instant spot-price update.")

fo_stocks, stock_name_to_key = load_fno_stock_universe()
interval = st.radio("Candle interval", ["1", "5", "15", "30"], index=1, horizontal=True, format_func=lambda v: f"{v} min")

all_symbol_keys = [("NIFTY", INDEX_SYMBOLS["NIFTY"]), ("BANKNIFTY", INDEX_SYMBOLS["BANKNIFTY"])]
all_symbol_keys += [(s, stock_name_to_key[s]) for s in fo_stocks]

st.session_state.setdefault("options_precomputed", {})
st.session_state.setdefault("options_ltp_map", {})
st.session_state.setdefault("options_last_precompute", None)
st.session_state.setdefault("options_last_zones", None)
st.session_state.setdefault("options_last_quotes", None)
st.session_state.setdefault("options_tried_disk_cache", False)

if not st.session_state["options_tried_disk_cache"] and not st.session_state["options_precomputed"]:
    disk_cache = load_cache_from_disk()
    if disk_cache:
        st.session_state["options_precomputed"] = disk_cache["precomputed"]
        st.session_state["options_ltp_map"] = disk_cache["ltp_map"]
        generated_at = disk_cache["generated_at"]
        st.session_state["options_last_precompute"] = f"{generated_at} (scheduled)"
        st.session_state["options_last_zones"] = f"{generated_at} (scheduled)"
        st.session_state["options_last_quotes"] = f"{generated_at} (scheduled)"
    st.session_state["options_tried_disk_cache"] = True

col1, col2, col3 = st.columns(3)
with col1:
    do_precompute = st.button("Run Precompute (slow, once/day)", width="stretch")
with col2:
    do_zones = st.button("Refresh Zones (medium, every few min)", width="stretch")
with col3:
    do_quotes = st.button("Refresh Quotes (fast)", width="stretch")

status_bits = []
if st.session_state["options_last_precompute"]:
    status_bits.append(f"Precompute: {st.session_state['options_last_precompute']}")
if st.session_state["options_last_zones"]:
    status_bits.append(f"Zones: {st.session_state['options_last_zones']}")
if st.session_state["options_last_quotes"]:
    status_bits.append(f"Quotes: {st.session_state['options_last_quotes']}")
st.caption(" · ".join(status_bits) if status_bits else "Not loaded yet -- click Run Precompute.")

if do_precompute:
    with st.spinner("Precompute: fetching spot prices, expiries, chains, ATM strikes, 18-day S/R and CE/PE candles for every symbol..."):
        ltp_map = get_batch_ltp(tuple(k for _, k in all_symbol_keys))
        st.session_state["options_ltp_map"] = ltp_map

        precomputed = {}
        progress = st.progress(0.0)
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(resolve_symbol_static, s, k): s for s, k in all_symbol_keys}
            for fut in concurrent.futures.as_completed(futures):
                s = futures[fut]
                try:
                    entry = fut.result()
                except Exception:
                    entry = None
                if entry:
                    precomputed[s] = entry
                done += 1
                progress.progress(done / len(futures))
        progress.empty()

        progress = st.progress(0.0)
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_symbol_candles, entry, ltp_map.get(entry["underlying_key"]), interval): s
                for s, entry in precomputed.items()
            }
            for fut in concurrent.futures.as_completed(futures):
                s = futures[fut]
                try:
                    precomputed[s].update(fut.result())
                except Exception:
                    pass
                done += 1
                progress.progress(done / len(futures))
        progress.empty()

        st.session_state["options_precomputed"] = precomputed
        st.session_state["options_last_precompute"] = datetime.now().strftime("%H:%M:%S")
        st.session_state["options_last_zones"] = datetime.now().strftime("%H:%M:%S")
        st.session_state["options_last_quotes"] = datetime.now().strftime("%H:%M:%S")
    st.rerun()

elif do_zones:
    precomputed = st.session_state.get("options_precomputed", {})
    if not precomputed:
        st.warning("Nothing to refresh yet -- click Run Precompute first.")
    else:
        with st.spinner(f"Refresh Zones: re-fetching CE/PE candles for {len(precomputed)} symbols..."):
            ltp_map = st.session_state.get("options_ltp_map", {})
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(fetch_symbol_candles, entry, ltp_map.get(entry["underlying_key"]), interval): s
                    for s, entry in precomputed.items()
                }
                for fut in concurrent.futures.as_completed(futures):
                    s = futures[fut]
                    try:
                        precomputed[s].update(fut.result())
                    except Exception:
                        pass
            st.session_state["options_precomputed"] = precomputed
            st.session_state["options_last_zones"] = datetime.now().strftime("%H:%M:%S")
    st.rerun()

elif do_quotes:
    precomputed = st.session_state.get("options_precomputed", {})
    if not precomputed:
        st.warning("Nothing to refresh yet -- click Run Precompute first.")
    else:
        with st.spinner("Refresh Quotes: one batched LTP call..."):
            get_batch_ltp.clear()
            ltp_map = get_batch_ltp(tuple(k for _, k in all_symbol_keys))
            st.session_state["options_ltp_map"] = ltp_map
            for s, entry in precomputed.items():
                entry["atm_strike"] = pick_atm_strike(entry["strikes"], ltp_map.get(entry["underlying_key"]))
            st.session_state["options_precomputed"] = precomputed
            st.session_state["options_last_quotes"] = datetime.now().strftime("%H:%M:%S")
    st.rerun()

auto_refresh_quotes = st.checkbox(
    "Auto-refresh quotes (every 30s) -- only while this tab stays open",
    value=False,
)
if auto_refresh_quotes and st.session_state.get("options_precomputed"):
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=30_000, key="options_quotes_autorefresh")
        get_batch_ltp.clear()
        ltp_map = get_batch_ltp(tuple(k for _, k in all_symbol_keys))
        st.session_state["options_ltp_map"] = ltp_map
        precomputed = st.session_state.get("options_precomputed", {})
        for s, entry in precomputed.items():
            entry["atm_strike"] = pick_atm_strike(entry["strikes"], ltp_map.get(entry["underlying_key"]))
        st.session_state["options_precomputed"] = precomputed
        st.session_state["options_last_quotes"] = datetime.now().strftime("%H:%M:%S")
    except ImportError:
        st.caption("Auto-refresh needs the `streamlit-autorefresh` package -- add it to requirements.txt to enable this checkbox.")

# ---------------------------------------------------------------------------
# Render -- pure, reads only st.session_state. No network calls below here.
# ---------------------------------------------------------------------------
precomputed = st.session_state.get("options_precomputed", {})
ltp_map = st.session_state.get("options_ltp_map", {})

st.header("Indices")
for idx_symbol in ["NIFTY", "BANKNIFTY"]:
    entry = precomputed.get(idx_symbol)
    ltp = ltp_map.get(INDEX_SYMBOLS[idx_symbol])
    render_symbol_block(idx_symbol, entry, ltp)

st.header("F&O Stocks -- by sector")
st.caption(f"{len(fo_stocks)} F&O stocks, grouped the same way as your dryarapureddy-sectors app.")

# Group by sector; anything not in SECTOR_MAP (new/renamed listings the
# mapping hasn't caught up with yet) goes into its own catch-all tab rather
# than silently disappearing.
stocks_by_sector = {}
for stock_symbol in fo_stocks:
    sector = SECTOR_MAP.get(stock_symbol, OTHER_SECTOR)
    stocks_by_sector.setdefault(sector, []).append(stock_symbol)

# Order: sectors with the most F&O stocks first (Banks/NBFC/Pharma etc.
# tend to matter most for options flow), "Other" last.
sector_names = sorted(
    (s for s in stocks_by_sector if s != OTHER_SECTOR),
    key=lambda s: -len(stocks_by_sector[s]),
)
if OTHER_SECTOR in stocks_by_sector:
    sector_names.append(OTHER_SECTOR)

sector_tabs = st.tabs([f"{s} ({len(stocks_by_sector[s])})" for s in sector_names])

for sector_name, tab in zip(sector_names, sector_tabs):
    with tab:
        for stock_symbol in stocks_by_sector[sector_name]:
            entry = precomputed.get(stock_symbol)
            ltp = ltp_map.get(stock_name_to_key[stock_symbol])
            render_symbol_block(stock_symbol, entry, ltp)

st.caption("Data via Upstox. Support/Resistance is a standard 18-trading-day high/low composite "
           "computed on the underlying's own daily candles. Sector groupings copied from "
           "dryarapureddy-sectors.")
