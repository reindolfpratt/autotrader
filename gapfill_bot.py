import os, math, base64, random, time
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BASE = os.getenv("T212_BASE_URL", "https://demo.trading212.com/api/v0").rstrip("/")
API_KEY = os.getenv("T212_API_KEY")
API_SECRET = os.getenv("T212_API_SECRET")
ACCOUNT_CCY = os.getenv("ACCOUNT_CURRENCY", "GBP")

UNIVERSE = [t.strip().upper() for t in os.getenv("TICKERS", "VUSA,VUAG,ISF,VUKE,VMID").split(",")]
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET_GBP", "2000"))
PER_TRADE_RISK = float(os.getenv("PER_TRADE_RISK_PCT", "0.005"))

# Gap + filters
MIN_GAP = float(os.getenv("MIN_GAP_DOWN", "-0.003"))  # e.g., -0.3%
MAX_GAP = float(os.getenv("MAX_GAP_DOWN", "-0.010"))  # e.g., -1.0%
RSI_MAX = float(os.getenv("RSI_MAX", "40"))
SLIPPAGE_BP = float(os.getenv("SLIPPAGE_BP", "5")) / 10000.0

# Market clock (LSE)
TZ = os.getenv("TIMEZONE", "Europe/London")
OPEN_HHMM = os.getenv("LSE_OPEN_HHMM", "08:00")
CLOSE_HHMM = os.getenv("LSE_CLOSE_HHMM", "16:30")

def parse_hhmm(s: str) -> dt.time:
    hh, mm = s.split(":")
    return dt.time(int(hh), int(mm))

OPEN_T = parse_hhmm(OPEN_HHMM)
CLOSE_T = parse_hhmm(CLOSE_HHMM)

# Cache of positions we opened today: ticker -> qty
positions: dict[str, int] = {}

# ──────────────────────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────────────────────
def zdt_now():
    return dt.datetime.now(pytz.timezone(TZ))

def is_market_open() -> bool:
    n = zdt_now()
    return (n.weekday() < 5) and (OPEN_T <= n.time() <= CLOSE_T)

def wait_until_open():
    while True:
        n = zdt_now()
        if n.weekday() >= 5:
            print("[WAIT] Weekend. Sleeping 30m...")
            time.sleep(1800)
            continue
        if n.time() >= OPEN_T:
            return
        next_open = dt.datetime.combine(n.date(), OPEN_T, n.tzinfo)
        secs = (next_open - n).total_seconds()
        sleep_s = max(5, min(secs, 300))
        print(f"[WAIT] Market closed. Sleeping {sleep_s:.0f}s…")
        time.sleep(sleep_s)

# ──────────────────────────────────────────────────────────────────────────────
# Trading 212 API wrapper with backoff
# ──────────────────────────────────────────────────────────────────────────────
def auth_header() -> dict:
    assert API_KEY and API_SECRET, "Missing T212 API creds in .env"
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def t212_request(method: str, path: str, **kwargs):
    url = f"{BASE}{path}"
    headers = kwargs.pop("headers", {})
    headers.update(auth_header())

    for attempt in range(6):  # exponential backoff
        resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        if resp.status_code not in (429, 500, 502, 503, 504):
            resp.raise_for_status()
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = int(retry_after)
        else:
            sleep_s = (2 ** attempt) + random.uniform(0, 0.5)

        print(f"[RATE-LIMIT] {resp.status_code} {path} → retry {attempt+1}/6 in {sleep_s:.1f}s")
        time.sleep(sleep_s)

    resp.raise_for_status()
    return resp  # not reached

# Thin API helpers (use the backoff wrapper)
def get_cash_gbp() -> float:
    r = t212_request("GET", "/equity/account/cash")
    return float(r.json().get("cash", 0.0))

def post_market_order(ticker: str, qty: float):
    payload = {"ticker": ticker, "quantity": round(qty, 6), "timeValidity": "DAY"}
    r = t212_request("POST", "/equity/orders/market", json=payload)
    return r.json()

def post_stop_order(ticker: str, qty_to_sell: float, stop_price: float):
    payload = {"ticker": ticker, "quantity": -abs(round(qty_to_sell, 6)),
               "stopPrice": round(stop_price, 4), "timeValidity": "DAY"}
    r = t212_request("POST", "/equity/orders/stop", json=payload)
    return r.json()

def market_sell_all(ticker: str, qty: float):
    payload = {"ticker": ticker, "quantity": -abs(round(qty, 6)), "timeValidity": "DAY"}
    r = t212_request("POST", "/equity/orders/market", json=payload)
    return r.json()

# ──────────────────────────────────────────────────────────────────────────────
# Market data + signals
# ──────────────────────────────────────────────────────────────────────────────
def yf_symbol(ticker: str) -> str:
    # LSE symbols on Yahoo add ".L"
    return f"{ticker}.L"

def prev_close_and_rsi14(yf_sym: str):
    df = yf.download(yf_sym, period="2mo", interval="1d", auto_adjust=False, progress=False)
    if df.empty or len(df) < 20:
        return None, None
    close = df["Close"]
    prev_close = float(close.iloc[-2])
    delta = close.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up).rolling(14).mean()
    roll_down = pd.Series(down).rolling(14).mean().replace(0, np.nan)
    rs = roll_up / roll_down
    rsi = 100 - (100 / (1 + rs))
    rsi_yday = float(rsi.iloc[-2])
    return prev_close, rsi_yday

def last_price_intraday(yf_sym: str) -> float | None:
    df = yf.download(yf_sym, period="1d", interval="1m", progress=False)
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])

@dataclass
class Plan:
    t212_ticker: str
    entry: float
    target: float
    stop: float
    qty: int

def make_plan(t212_ticker: str, budget_each: float) -> Plan | None:
    yfs = yf_symbol(t212_ticker)
    prev_close, rsi_yday = prev_close_and_rsi14(yfs)
    if prev_close is None or rsi_yday is None:
        return None

    # wait ~60s after open for a tradable price
    time.sleep(60)
    open_px = last_price_intraday(yfs)
    if not open_px:
        return None

    gap_pct = (open_px - prev_close) / prev_close
    if not (MAX_GAP <= gap_pct <= MIN_GAP):   # e.g., between -1.0% and -0.3%
        return None
    if rsi_yday > RSI_MAX:
        return None

    target = prev_close * (1 - SLIPPAGE_BP)  # slightly below full gap-fill
    # Protective stop: tighter of 0.6*|gap| or 0.6%
    stop = open_px * (1 - min(0.006, abs(gap_pct) * 0.6))

    risk_per_share = max(open_px - stop, open_px * 0.002)  # ≥0.2% floor
    max_risk_cap = get_cash_gbp() * PER_TRADE_RISK
    by_risk = math.floor(max_risk_cap / risk_per_share)
    by_budget = math.floor(budget_each / open_px)
    qty = int(max(0, min(by_risk, by_budget)))
    if qty <= 0:
        return None

    return Plan(t212_ticker, open_px, target, stop, qty)

# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────
def run_day():
    print("[BOOT] Gap-fill GBP bot ready. Waiting for market open…")
    wait_until_open()

    budget_each = TOTAL_BUDGET / max(1, len(UNIVERSE))
    spent = 0.0

    for t in UNIVERSE:
        if not is_market_open():
            break
        if spent >= TOTAL_BUDGET:
            print("[INFO] Budget fully allocated.")
            break

        plan = make_plan(t, budget_each)
        if not plan:
            print(f"[SKIP] {t}: no valid setup.")
            continue

        # Trim to avoid breaking the total £2k cap
        max_affordable = math.floor((TOTAL_BUDGET - spent) / plan.entry)
        if max_affordable <= 0:
            print("[INFO] Budget cap hit; skipping remaining.")
            break
        qty = min(plan.qty, max_affordable)

        print(f"[BUY] {t} qty={qty} @≈{plan.entry:.2f} tgt={plan.target:.2f} stop={plan.stop:.2f}")
        post_market_order(plan.t212_ticker, qty)
        positions[plan.t212_ticker] = positions.get(plan.t212_ticker, 0) + qty
        spent += qty * plan.entry
        time.sleep(0.5)  # tiny courtesy pause

        # Watch for gap-fill
        while is_market_open():
            px = last_price_intraday(yf_symbol(plan.t212_ticker))
            if px and px >= plan.target:
                q = positions.get(plan.t212_ticker, 0)
                if q > 0:
                    print(f"[TP] {t} target hit @ {px:.2f}; selling {q}.")
                    market_sell_all(plan.t212_ticker, q)
                    positions[plan.t212_ticker] = 0
                break
            time.sleep(45)  # gentle polling to avoid rate limits

        # Attach stop after entry (DAY)
        try:
            q = positions.get(plan.t212_ticker, 0)
            if q > 0:
                post_stop_order(plan.t212_ticker, q, plan.stop)
                print(f"[STOP] {t} placed @ {plan.stop:.2f}")
        except Exception as e:
            print(f"[WARN] stop placement failed: {e}")

    # End-of-day hard exit for anything left
    for t, q in list(positions.items()):
        if q > 0:
            print(f"[EOD] Closing {t} remaining qty={q}.")
            market_sell_all(t, q)
            positions[t] = 0
        time.sleep(0.5)

def main():
    while True:
        run_day()
        # After close, idle and loop next day
        while is_market_open():
            time.sleep(60)
        print("[SLEEP] Market closed. Sleeping 1 hour…")
        time.sleep(3600)

if __name__ == "__main__":
    main()
