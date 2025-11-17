import os
import math
import base64
import random
import time
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz
from dotenv import load_dotenv

print("ENV BUDGET:", os.environ.get("TOTAL_BUDGET_GBP"))

# ──────────────────────
# Config
# ──────────────────────
load_dotenv()

BASE = os.getenv("T212_BASE_URL", "https://live.trading212.com/api/v0").rstrip("/")
API_KEY = os.getenv("T212_API_KEY")
API_SECRET = os.getenv("T212_API_SECRET")
ACCOUNT_CCY = os.getenv("ACCOUNT_CURRENCY", "GBP")

UNIVERSE = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,GME,AMC,COIN,AAPL,NVDA,AMD,BABA,PLTR,RR,ACHR,EQQQ").split(",")]
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET_GBP", "100"))
PER_TRADE_RISK = float(os.getenv("PER_TRADE_RISK_PCT", "0.005"))

MIN_GAP = float(os.getenv("MIN_GAP_DOWN", "-0.005"))
MAX_GAP = float(os.getenv("MAX_GAP_DOWN", "-0.030"))
RSI_MAX = float(os.getenv("RSI_MAX", "50"))
SLIPPAGE_BP = float(os.getenv("SLIPPAGE_BP", "5")) / 10000.0

TZ = os.getenv("TIMEZONE", "America/New_York")
OPEN_HHMM = os.getenv("US_OPEN_HHMM", "09:30")
CLOSE_HHMM = os.getenv("US_CLOSE_HHMM", "16:00")

T212_CODES = {kv.split("=")[0]:kv.split("=")[1] for kv in os.getenv("T212_CODES","").split(",") if "=" in kv}

def parse_hhmm(s: str) -> dt.time:
    hh, mm = s.split(":")
    return dt.time(int(hh), int(mm))

OPEN_T = parse_hhmm(OPEN_HHMM)
CLOSE_T = parse_hhmm(CLOSE_HHMM)

positions: dict[str, int] = {}

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

def auth_header() -> dict:
    assert API_KEY and API_SECRET, "Missing T212 API creds in .env"
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def t212_request(method: str, path: str, **kwargs):
    url = f"{BASE}{path}"
    headers = kwargs.pop("headers", {})
    headers.update(auth_header())

    for attempt in range(6):
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
    return resp

def get_cash_gbp() -> float:
    r = t212_request("GET", "/equity/account/cash")
    return float(r.json().get("free", 0.0))

def post_market_order(instrument_code: str, qty: float):
    payload = {"instrumentCode": instrument_code, "quantity": round(qty, 6), "timeValidity": "DAY"}
    print(f"[DEBUG] Order payload for {instrument_code}:", payload)
    r = t212_request("POST", "/equity/orders/market", json=payload)
    return r.json()

def post_stop_order(instrument_code: str, qty_to_sell: float, stop_price: float):
    payload = {"instrumentCode": instrument_code, "quantity": -abs(round(qty_to_sell, 6)),
               "stopPrice": round(stop_price, 4), "timeValidity": "DAY"}
    r = t212_request("POST", "/equity/orders/stop", json=payload)
    return r.json()

def market_sell_all(instrument_code: str, qty: float):
    payload = {"instrumentCode": instrument_code, "quantity": -abs(round(qty, 6)), "timeValidity": "DAY"}
    r = t212_request("POST", "/equity/orders/market", json=payload)
    return r.json()

def yf_symbol(ticker: str) -> str:
    LSE_TICKERS = {'RR', 'EQQQ', 'VUSA', 'VUAG', 'ISF', 'VUKE', 'VMID'}
    if ticker.upper() in LSE_TICKERS:
        return f"{ticker}.L"
    return ticker

def prev_close_and_rsi14(yf_sym: str):
    try:
        df = yf.download(yf_sym, period="3mo", interval="1d", auto_adjust=False, progress=False)
        if df.empty or len(df) < 20:
            return None, None
        
        now_hour = zdt_now().hour
        if now_hour < 17:
            df = df[:-1]
        
        if len(df) < 15:
            return None, None
            
        close = df["Close"]
        prev_close = float(close.iloc[-1])
        
        delta = close.diff()
        up = delta.copy()
        down = delta.copy()
        up[up < 0] = 0.0
        down[down > 0] = 0.0
        down = abs(down)
        roll_up = up.rolling(14).mean()
        roll_down = down.rolling(14).mean().replace(0, np.nan)
        rs = roll_up / roll_down
        rsi = 100 - (100 / (1 + rs))
        rsi_yday = float(rsi.iloc[-1])
        return prev_close, rsi_yday
    except Exception as e:
        print(f"[ERROR] prev_close_and_rsi14 for {yf_sym}: {e}")
        return None, None

def get_actual_open_price(yf_sym: str, max_wait_minutes: int = 5) -> float | None:
    for attempt in range(max_wait_minutes):
        try:
            df = yf.download(yf_sym, period="1d", interval="1m", progress=False)
            if not df.empty and len(df) >= 1:
                open_price = float(df["Open"].iloc[0])
                print(f"[DATA] {yf_sym} open price: ${open_price:.2f} (from first candle)")
                return open_price
        except Exception as e:
            print(f"[WARN] get_actual_open_price attempt {attempt+1}: {e}")
        if attempt < max_wait_minutes - 1:
            print(f"[WAIT] Waiting for {yf_sym} opening data... ({attempt+1}/{max_wait_minutes})")
            time.sleep(60)
    return None

def last_price_intraday(yf_sym: str) -> float | None:
    try:
        df = yf.download(yf_sym, period="1d", interval="1m", progress=False)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except:
        return None

@dataclass
class Plan:
    instrument_code: str
    yf_ticker: str
    entry: float
    target: float
    stop: float
    qty: int

def make_plan(ticker: str, budget_each: float) -> Plan | None:
    yfs = yf_symbol(ticker)
    print(f"\n{'='*60}")
    print(f"[SCAN] Analyzing {ticker}...")
    prev_close, rsi_yday = prev_close_and_rsi14(yfs)
    if prev_close is None or rsi_yday is None:
        print(f"[SKIP] {ticker}: Could not fetch historical data")
        return None
    print(f"[DATA] {ticker}: Prev Close = ${prev_close:.2f}, RSI = {rsi_yday:.2f}")
    open_px = get_actual_open_price(yfs, max_wait_minutes=3)
    if not open_px:
        print(f"[SKIP] {ticker}: No opening price available")
        return None
    gap_pct = (open_px - prev_close) / prev_close
    print(f"[GAP] {ticker}: {gap_pct*100:.3f}% (Open: ${open_px:.2f})")
    if not (MAX_GAP <= gap_pct <= MIN_GAP):
        print(f"[SKIP] {ticker}: Gap {gap_pct*100:.3f}% outside range [{MAX_GAP*100:.2f}%, {MIN_GAP*100:.2f}%]")
        return None
    if rsi_yday > RSI_MAX:
        print(f"[SKIP] {ticker}: RSI {rsi_yday:.2f} > {RSI_MAX}")
        return None
    print(f"[PASS] {ticker}: ✓ Gap and RSI filters passed!")
    target = prev_close * (1 - SLIPPAGE_BP)
    stop = open_px * (1 - min(0.006, abs(gap_pct) * 0.6))
    risk_per_share = max(open_px - stop, open_px * 0.002)
    max_risk_cap = get_cash_gbp() * PER_TRADE_RISK
    by_risk = math.floor(max_risk_cap / risk_per_share)
    by_budget = math.floor(budget_each / open_px)
    qty = int(max(0, min(by_risk, by_budget)))
    if qty <= 0:
        print(f"[SKIP] {ticker}: Qty = 0 (insufficient budget or risk cap)")
        return None
    instrument_code = T212_CODES.get(ticker, ticker)
    print(f"[PLAN] {ticker}: Entry=${open_px:.2f}, Target=${target:.2f}, Stop=${stop:.2f}, Qty={qty}")
    print(f"[T212] Will use instrument code: {instrument_code}")
    return Plan(instrument_code, yfs, open_px, target, stop, qty)

def run_day():
    print("\n" + "="*60)
    print(f"[BOOT] US+LSE Gap-fill bot starting {zdt_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[CONFIG] Gap range: {MAX_GAP*100:.2f}% to {MIN_GAP*100:.2f}%")
    print(f"[CONFIG] RSI max: {RSI_MAX}")
    print(f"[CONFIG] Total budget: £{TOTAL_BUDGET}")
    print(f"[CONFIG] Universe: {', '.join(UNIVERSE)}")
    print("="*60)
    wait_until_open()
    print(f"[START] Market open! Beginning scan at {zdt_now().strftime('%H:%M:%S %Z')}")
    budget_each = TOTAL_BUDGET / max(1, len(UNIVERSE))
    spent = 0.0
    for t in UNIVERSE:
        if not is_market_open():
            print("[INFO] Market closed during scan.")
            break
        if spent >= TOTAL_BUDGET:
            print("[INFO] Budget fully allocated.")
            break
        plan = make_plan(t, budget_each)
        if not plan:
            continue
        max_affordable = math.floor((TOTAL_BUDGET - spent) / plan.entry)
        if max_affordable <= 0:
            print("[INFO] Budget cap hit; skipping remaining.")
            break
        qty = min(plan.qty, max_affordable)
        try:
            print(f"\n[BUY] {t} → Market order for {qty} shares @ ≈${plan.entry:.2f}")
            resp = post_market_order(plan.instrument_code, qty)
            print(f"[ORDER] Response: {resp}")
            positions[plan.instrument_code] = positions.get(plan.instrument_code, 0) + qty
            spent += qty * plan.entry
            time.sleep(1)
            try:
                stop_resp = post_stop_order(plan.instrument_code, qty, plan.stop)
                print(f"[STOP] Placed @ ${plan.stop:.2f} - Response: {stop_resp}")
            except Exception as e:
                print(f"[WARN] Stop placement failed: {e}")
        except Exception as e:
            print(f"[ERROR] Order failed for {t}: {e}")
            import traceback
            traceback.print_exc()
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                print("API Error Response:", e.response.text)
            continue
        print(f"[MONITOR] Watching {t} for target ${plan.target:.2f}...")
        monitor_start = time.time()
        while is_market_open() and (time.time() - monitor_start) < 3600:
            try:
                px = last_price_intraday(plan.yf_ticker)
                if px and px >= plan.target:
                    q = positions.get(plan.instrument_code, 0)
                    if q > 0:
                        print(f"[TARGET] {t} hit ${px:.2f} ≥ ${plan.target:.2f} → Selling {q} shares")
                        market_sell_all(plan.instrument_code, q)
                        positions[plan.instrument_code] = 0
                    break
            except Exception as e:
                print(f"[WARN] Monitor error for {t}: {e}")
            time.sleep(45)
    print(f"\n[EOD] End of day cleanup at {zdt_now().strftime('%H:%M:%S %Z')}")
    for t, q in list(positions.items()):
        if q > 0:
            try:
                print(f"[EOD] Closing {t} remaining qty={q}")
                market_sell_all(t, q)
                positions[t] = 0
                time.sleep(1)
            except Exception as e:
                print(f"[ERROR] EOD close failed for {t}: {e}")
    print(f"[DONE] Trading day complete. Total spent: £{spent:.2f}")

def main():
    print("\n" + "#"*60)
    print("# US+LSE Gap-Fill Trading Bot")
    print("# Press Ctrl+C to stop")
    print("#"*60 + "\n")
    while True:
        try:
            run_day()
            while is_market_open():
                time.sleep(60)
            print(f"\n[SLEEP] Market closed at {zdt_now().strftime('%H:%M:%S %Z')}. Sleeping 1 hour...")
            time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[EXIT] Bot stopped by user.")
            break
        except Exception as e:
            print(f"\n[ERROR] Unexpected error: {e}")
            import traceback
            traceback.print_exc()
            print("[RECOVERY] Sleeping 5 minutes before retry...")
            time.sleep(300)

if __name__ == "__main__":
    main()
