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

# ──────────────────────────────────────────────────────────────────────────────
# Config - US + LSE STOCKS VERSION
# ──────────────────────────────────────────────────────────────────────────────
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

def parse_hhmm(s: str) -> dt.time:
    hh, mm = s.split(":")
    return dt.time(int(hh), int(mm))

OPEN_T = parse_hhmm(OPEN_HHMM)
CLOSE_T = parse_hhmm(CLOSE_HHMM)

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
# Trading 212 API wrapper
# ──────────────────────────────────────────────────────────────────────────────
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
            sleep_s = (2 
