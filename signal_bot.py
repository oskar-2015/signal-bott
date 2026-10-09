# -*- coding: utf-8 -*-
import os
import json
import time
import random
import asyncio
from threading import Thread
from datetime import datetime, timedelta

import requests
import pandas as pd
from flask import Flask
from waitress import serve
from pyquotex.stable_api import Quotex

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

SESSION_FILE = "qx_session.json"
NUM_PAIRS = 15
TRADE_DURATION_MIN = 1
LEAD_TIME_SEC = 60
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
MIN_PAYOUT = 70
CANDLE_HISTORY_SECONDS = 10800
MIN_STRENGTH = 15

PLATFORM_LINK = "https://broker-qx.pro/sign-up/?lid=2384798"
CHANNEL_LINK = 'قناة أوسكار 🔥'

CRYPTO_SYMBOLS = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "DOT", "LTC", "BCH", "ETC",
    "BNB", "TON", "DOGE", "AVA", "AVAX", "ATO", "ATOM", "DAS", "DASH",
    "ZEC", "XLM", "TRX", "LINK", "UNI", "AXS", "MATIC", "SHIB", "TRUMP"
]

app = Flask('')


@app.route('/')
def home():
    return 'Bot is alive!'


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    serve(app, host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()


def is_crypto(asset_name):
    base = asset_name.replace("_otc", "").upper()
    for sym in CRYPTO_SYMBOLS:
        if base.startswith(sym):
            return True
    return False


def load_session():
    env_cookies = os.environ.get("QX_COOKIES")
    env_ssid = os.environ.get("QX_SSID")
    if env_cookies and env_ssid:
        print("session loaded from environment variables")
        return env_cookies, env_ssid

    if not os.path.exists(SESSION_FILE):
        return None, None
    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cookies_str"), data.get("ssid")


def clean_candles(candles):
    items = []
    for c in candles:
        try:
            if isinstance(c, dict):
                ts = c.get("time") or c.get("timestamp") or c.get("from")
                close = c.get("close")
            else:
                ts, close = c[0], c[2]
            if ts is not None and close is not None:
                items.append((int(ts), float(close)))
        except Exception:
            continue
    items = sorted(set(items), key=lambda x: x[0])
    return [v for _, v in items]


def analyze_strength(closes):
    if len(closes) < max(EMA_SLOW, RSI_PERIOD) + 5:
        up = closes[-1] >= closes[0]
        return ("up" if up else "down"), 0

    s = pd.Series(closes)
    ema_fast = s.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = s.ewm(span=EMA_SLOW, adjust=False).mean()
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    last_rsi = rsi.iloc[-1]
    ema_diff_pct = abs(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / ema_slow.iloc[-1] * 100
    direction = "up" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "down"

    last3 = s.iloc[-3:]
    confirm_up = last3.iloc[-1] > last3.iloc[0]
    confirmed = (direction == "up" and confirm_up) or (direction == "down" and not confirm_up)
    if not confirmed:
        return direction, 0

    rsi_strength = abs(last_rsi - 50)
    strength = rsi_strength + (ema_diff_pct * 10)

    return direction, strength


def send_telegram(asset, direction, entry_time, duration_min):
    arrow = "صعود Call \U0001F7E2" if direction == "up" else "هبوط Put \U0001F534"
    text = (
        "\U0001F680 إشارة قوية مكتشفة!\n"
        f"\u2705 الزوج: {asset}\n"
        f"\u23F3 مدة الصفقة: {duration_min} دقيقة\n"
        f"\u23F0 الوقت: {entry_time}\n"
        f"\u2B05\uFE0F الاتجاه: {arrow}\n\n"
        "\U0001F447 رابط المنصة :\n"
        f"{PLATFORM_LINK}\n\n"
        f"{CHANNEL_LINK}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=15)
        print(f"sent: {asset} {direction}")
    except Exception as e:
        print(f"telegram error: {e}")


async def connect_client():
    cookies_str, ssid = load_session()
    if not cookies_str or not ssid:
        print("no session data available")
        return None
    client = Quotex(email="x@x.com", password="x", lang="en")
    client.set_session(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        cookies=cookies_str,
        ssid=ssid
    )
    check, reason = await client.connect()
    if not check:
        print(f"connect failed: {reason}")
        return None
    print("connected")
    return client


async def get_top_open_assets(client, n=NUM_PAIRS, min_payout=MIN_PAYOUT):
    try:
        all_assets = client.get_all_asset_name()
    except Exception as e:
        print(f"asset list error: {e}")
        return []
    candidates = []
    for item in all_assets:
        name = item[0]
        if is_crypto(name):
            continue
        try:
            result = await client.check_asset_open(name)
            is_open = result[1][2] if len(result) > 1 else False
            payout = result[0][5] if len(result[0]) > 5 else 0
            if is_open and payout >= min_payout:
                candidates.append((name, payout))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:n]


async def get_closes(client, asset):
    try:
        candles = await client.get_historical_candles(
            asset, amount_of_seconds=CANDLE_HISTORY_SECONDS, period=60, max_workers=3
        )
    except Exception:
        candles = await client.get_candles(asset, int(time.time()), CANDLE_HISTORY_SECONDS, 60)

    return clean_candles(candles)


async def pick_best_signal(client, assets):
    best = None
    for name, payout in assets:
        try:
            closes = await get_closes(client, name)
            direction, strength = analyze_strength(closes)
        except Exception as e:
            print(f"candles error {name}: {e}")
            continue

        if strength < MIN_STRENGTH:
            continue

        if best is None or strength > best[2]:
            best = (name, payout, strength, direction)

    return best


async def main_loop():
    client = await connect_client()
    if not client:
        return
    cycle = 0
    while True:
        cycle += 1
        print(f"cycle {cycle} - {datetime.now().strftime('%H:%M:%S')}")
        top_assets = await get_top_open_assets(client)
        if not top_assets:
            print("no open assets this cycle")
            await asyncio.sleep(LEAD_TIME_SEC)
            continue

        best = await pick_best_signal(client, top_assets)
        if best is None:
            print("no strong confirmed signal this cycle, skipping")
            await asyncio.sleep(LEAD_TIME_SEC)
            continue

        asset, payout, strength, direction = best
        print(f"best: {asset} payout={payout} strength={strength:.2f} dir={direction}")

        entry_time = (datetime.now() + timedelta(seconds=LEAD_TIME_SEC)).strftime("%H:%M")
        send_telegram(asset, direction, entry_time, TRADE_DURATION_MIN)

        wait_seconds = (TRADE_DURATION_MIN * 60) + LEAD_TIME_SEC
        print(f"waiting {wait_seconds}s")
        await asyncio.sleep(wait_seconds)


if __name__ == "__main__":
    keep_alive()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("stopped")
