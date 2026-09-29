"""
Standalone precompute script for GitHub Actions -- dryarapureddy-options.

Runs the SAME precompute logic as app.py's "Run Precompute" button
(resolve_symbol_static + fetch_symbol_candles), but with no Streamlit
dependency, so it can run headless on a schedule. Writes
options_zones_cache.json, which app.py reads on startup -- when this
script's output is committed back to the repo, Streamlit Cloud's
auto-redeploy-on-push picks up the fresh cache immediately, so opening
the app later shows current CE/PE charts and ATM strikes without ever
clicking "Run Precompute".

Reads the Upstox token from the UPSTOX_ANALYTICAL_TOKEN environment
variable (set as a GitHub Actions secret) -- the SAME long-lived (~1
year) analytical token already used for dryarapureddy-live's scheduled
precompute, not the daily-refreshed UPSTOX_ACCESS_TOKEN the Streamlit
app itself uses. That's what lets this run unattended every trading day
without you updating any secret.

NOTE: this deliberately DUPLICATES several functions/constants from
app.py (universe loading, option chain/candle fetch, 18-day S/R) rather
than importing them, since app.py has Streamlit-specific code
(st.cache_data, st.session_state, etc.) that can't run headless. If
those functions change in app.py, update this file to match.

Usage:
    UPSTOX_ANALYTICAL_TOKEN=xxx python github_precompute.py
"""
import gzip
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


# ---------------------------------------------------------------------------
# Constants (copied from app.py)
# ---------------------------------------------------------------------------
CACHE_PATH = "options_zones_cache.json"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"

INDEX_SYMBOLS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
}

CANDLE_INTERVAL = "5"  # minutes -- matches app.py's default radio selection


def get_token():
    token = os.environ.get("UPSTOX_ANALYTICAL_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ANALYTICAL_TOKEN environment variable not set.")
    return token.strip()


# ---------------------------------------------------------------------------
# Retry-with-backoff (copied from app.py)
# ---------------------------------------------------------------------------
def _get_with_backoff(url, headers, params=None, timeout=20, max_retries=5, base_delay=1.5):
    last_exc = None
    resp = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            time.sleep(base_delay * (2 ** attempt))
            continue
        if resp.status_code != 429:
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
        time.sleep(delay)
    if resp is None and last_exc is not None:
        raise last_exc
    return resp


# ---------------------------------------------------------------------------
# F&O stock universe (copied from app.py's load_fno_stock_universe, minus
# st.cache_data / st.warning)
# ---------------------------------------------------------------------------
def load_fno_stock_universe():
    resp = requests.get(INSTRUMENTS_URL, timeout=60)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    df = pd.read_csv(io.BytesIO(raw), low_memory=False)

    eq = df[df["exchange"] == "NSE_EQ"].dropna(subset=["name"])
    name_to_key = dict(zip(eq["name"], eq["instrument_key"]))
    name_to_tradingsymbol = dict(zip(eq["name"], eq["tradingsymbol"]))

    fo = df[(df["exchange"] == "NSE_FO") & (df["instrument_type"] == "OPTSTK")]
    fo_names = set(fo["name"].dropna().unique())

    symbol_to_key = {}
    for name in fo_names:
        key = name_to_key.get(name)
        tsym = name_to_tradingsymbol.get(name)
        if key and tsym:
            symbol_to_key[tsym] = key

    return sorted(symbol_to_key.keys()), symbol_to_key


# ---------------------------------------------------------------------------
# Option contracts / chain / candles (copied from app.py, minus
# st.cache_data)
# ---------------------------------------------------------------------------
def get_expiries(underlying_key, headers):
    url = "https://api.upstox.com/v2/option/contract"
    resp = _get_with_backoff(url, headers, params={"instrument_key": underlying_key})
    if resp is None or resp.status_code != 200:
        return []
    data = resp.json().get("data", [])
    return sorted({c["expiry"] for c in data if c.get("expiry")})


def get_option_chain(underlying_key, expiry_date, headers):
    url = "https://api.upstox.com/v2/option/chain"
    resp = _get_with_backoff(url, headers, params={"instrument_key": underlying_key, "expiry_date": expiry_date})
    if resp is None or resp.status_code != 200:
        return []
    return resp.json().get("data", [])


def _candles_df(candles):
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp")


def fetch_intraday_candles(instrument_key, headers, unit="minutes", interval="5"):
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
    resp = _get_with_backoff(url, headers)
    if resp is None or resp.status_code != 200:
        return pd.DataFrame()
    return _candles_df(resp.json().get("data", {}).get("candles", []))


def fetch_recent_candles(instrument_key, headers, unit="minutes", interval="5", lookback_days=5):
    to_date = now_ist().strftime("%Y-%m-%d")
    from_date = (now_ist() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    resp = _get_with_backoff(url, headers)
    if resp is None or resp.status_code != 200:
        return pd.DataFrame()
    return _candles_df(resp.json().get("data", {}).get("candles", []))


def candles_with_fallback(instrument_key, headers, interval):
    df = fetch_intraday_candles(instrument_key, headers, interval=interval)
    if not df.empty:
        return df, False
    df = fetch_recent_candles(instrument_key, headers, interval=interval)
    if not df.empty:
        last_day = df["timestamp"].dt.date.max()
        df = df[df["timestamp"].dt.date == last_day]
    return df, True


def fetch_daily_candles(instrument_key, headers, lookback_days=40):
    to_date = now_ist().strftime("%Y-%m-%d")
    from_date = (now_ist() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}"
    resp = _get_with_backoff(url, headers)
    if resp is None or resp.status_code != 200:
        return pd.DataFrame()
    return _candles_df(resp.json().get("data", {}).get("candles", []))


def compute_18day_sr(daily_df, lookback=18):
    if daily_df.empty:
        return None, None
    window = daily_df.tail(lookback)
    resistance = float(window["high"].max())
    support = float(window["low"].min())
    return support, resistance


def pick_atm_strike(strikes, ltp):
    if not strikes or ltp is None:
        return strikes[len(strikes) // 2] if strikes else None
    return min(strikes, key=lambda s: abs(s - ltp))


def pick_nearest_expiry(expiries):
    today = now_ist().date()
    future = [e for e in expiries if datetime.strptime(e, "%Y-%m-%d").date() >= today]
    return sorted(future)[0] if future else sorted(expiries)[0]


def get_batch_ltp(instrument_keys, headers):
    result = {}
    chunk_size = 200
    for i in range(0, len(instrument_keys), chunk_size):
        chunk = instrument_keys[i:i + chunk_size]
        url = "https://api.upstox.com/v2/market-quote/ltp"
        resp = _get_with_backoff(url, headers, params={"instrument_key": ",".join(chunk)})
        if resp is None or resp.status_code != 200:
            continue
        data = resp.json().get("data", {})
        for _, v in data.items():
            key = v.get("instrument_token")
            if key:
                result[key] = v.get("last_price")
    return result


def _df_to_records(df):
    if df.empty:
        return []
    out = df.copy()
    out["timestamp"] = out["timestamp"].astype(str)
    return out.to_dict("records")


# ---------------------------------------------------------------------------
# Per-symbol resolve + candle fetch (mirrors app.py's resolve_symbol_static
# and fetch_symbol_candles)
# ---------------------------------------------------------------------------
def resolve_and_fetch(symbol, underlying_key, ltp, headers):
    expiries = get_expiries(underlying_key, headers)
    if not expiries:
        return None
    expiry = pick_nearest_expiry(expiries)

    chain = get_option_chain(underlying_key, expiry, headers)
    if not chain:
        return None
    strikes = sorted({r["strike_price"] for r in chain if r.get("strike_price") is not None})
    if not strikes:
        return None

    daily_df = fetch_daily_candles(underlying_key, headers)
    support, resistance = compute_18day_sr(daily_df)

    atm_strike = pick_atm_strike(strikes, ltp)
    row = next((r for r in chain if r.get("strike_price") == atm_strike), None)
    ce_key = (row.get("call_options") or {}).get("instrument_key") if row else None
    pe_key = (row.get("put_options") or {}).get("instrument_key") if row else None

    ce_df, ce_fallback = candles_with_fallback(ce_key, headers, CANDLE_INTERVAL) if ce_key else (pd.DataFrame(), False)
    pe_df, pe_fallback = candles_with_fallback(pe_key, headers, CANDLE_INTERVAL) if pe_key else (pd.DataFrame(), False)
    # Underlying's own candles -- added so the scheduled cache matches
    # app.py's "Run Precompute" button, which fetches this same third series
    # (see fetch_symbol_candles in app.py). Without this, every cache this
    # script writes silently omits underlying_df, so the underlying panel in
    # the three-panel view is empty for every symbol until someone clicks
    # "Run Precompute" by hand -- and the next scheduled run overwrites that
    # with the same gap again.
    underlying_df, underlying_fallback = candles_with_fallback(underlying_key, headers, CANDLE_INTERVAL)

    return {
        "underlying_key": underlying_key,
        "expiry": expiry,
        "chain": chain,
        "strikes": strikes,
        "support": support,
        "resistance": resistance,
        "atm_strike": atm_strike,
        "ce_key": ce_key,
        "pe_key": pe_key,
        "ce_df": _df_to_records(ce_df),
        "ce_fallback": ce_fallback,
        "pe_df": _df_to_records(pe_df),
        "pe_fallback": pe_fallback,
        "underlying_df": _df_to_records(underlying_df),
        "underlying_fallback": underlying_fallback,
    }


# ---------------------------------------------------------------------------
# Main precompute loop
# ---------------------------------------------------------------------------
def run_precompute(token):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    fo_stocks, stock_name_to_key = load_fno_stock_universe()
    all_symbol_keys = [("NIFTY", INDEX_SYMBOLS["NIFTY"]), ("BANKNIFTY", INDEX_SYMBOLS["BANKNIFTY"])]
    all_symbol_keys += [(s, stock_name_to_key[s]) for s in fo_stocks]
    total = len(all_symbol_keys)

    ltp_map = get_batch_ltp([k for _, k in all_symbol_keys], headers)

    precomputed = {}
    for i, (symbol, key) in enumerate(all_symbol_keys):
        try:
            entry = resolve_and_fetch(symbol, key, ltp_map.get(key), headers)
            if entry:
                precomputed[symbol] = entry
                print(f"  [{i + 1}/{total}] {symbol}: ok (ATM {entry['atm_strike']})")
            else:
                print(f"  [{i + 1}/{total}] {symbol}: no expiry/chain/strikes, skipping.")
        except Exception as e:
            print(f"  [{i + 1}/{total}] {symbol}: precompute failed ({e}), skipping.")
        time.sleep(0.1)

    out = {
        "generated_at": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "interval": CANDLE_INTERVAL,
        "ltp_map": ltp_map,
        "precomputed": precomputed,
    }
    with open(CACHE_PATH, "w") as f:
        json.dump(out, f)
    print(f"\nPrecompute done. {len(precomputed)}/{total} symbols cached -> {CACHE_PATH}")
    return out


if __name__ == "__main__":
    tok = get_token()
    run_precompute(tok)
