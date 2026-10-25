# -*- coding: utf-8 -*-
import os
import json
import time
import asyncio
from threading import Thread
from datetime import datetime, timedelta

import requests
import pandas as pd
from waitress import serve
from pyquotex.stable_api import Quotex

from app import app as web_app
from quotex_manager import copy_manager
from database import get_setting, get_secret_setting

try:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = None
    TELEGRAM_CHAT_ID = None

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID

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


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    serve(web_app, host='0.0.0.0', port=port, threads=16)


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


def load_master_session():
    """
    يُحمّل جلسة الماستر فقط من قاعدة البيانات.
    لا يستخدم qx_session.json ولا متغيرات البيئة القديمة.
    """
    try:
        master_ssid = get_secret_setting("master_ssid")
        master_cookies = get_secret_setting("master_cookies")
        if not master_ssid:
            return None, None
        if master_ssid.startswith('<') or 'object at 0x' in master_ssid:
            return None, None
        return master_cookies, master_ssid
    except Exception as e:
        print(f"load_master_session error: {e}", flush=True)
        return None, None


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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("telegram skipped: missing credentials", flush=True)
        return
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
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=15)
        print(f"telegram sent: {asset} {direction} (status: {r.status_code})", flush=True)
    except Exception as e:
        print(f"telegram error: {e}", flush=True)


async def connect_client():
    cookies_str, ssid = load_master_session()
    if not ssid:
        return None, "master_not_linked"
    
    client = Quotex(email="x@x.com", password="x", lang="en", host="quotex.com")
    client.set_session(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        cookies=cookies_str or "",
        ssid=ssid
    )
    try:
        check, reason = await asyncio.wait_for(client.connect(), timeout=90)
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    
    if not check:
        return None, f"rejected: {reason}"
    
    print("✅ signal bot connected successfully", flush=True)
    return client, "OK"


async def get_top_open_assets(client, n=NUM_PAIRS, min_payout=MIN_PAYOUT):
    try:
        all_assets = client.get_all_asset_name()
    except Exception as e:
        print(f"asset list error: {e}", flush=True)
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
            print(f"candles error {name}: {e}", flush=True)
            continue
        if strength < MIN_STRENGTH:
            continue
        if best is None or strength > best[2]:
            best = (name, payout, strength, direction)
    return best


async def main_loop():
    """
    حلقة التداول. تستيقظ تلقائياً عند ربط الماستر.
    عند الفشل، تنتظر أوقات طويلة (5-60 دقيقة) لحماية IP من الحظر.
    """
    client = None
    cycle = 0
    fail_streak = 0

    while True:
        try:
            if client is None:
                # تحقق من وجود جلسة ماستر
                _ck, _ss = load_master_session()
                if not _ss:
                    print("[BOT] master not linked yet, waiting 2 minutes...", flush=True)
                    fail_streak = 0
                    await asyncio.sleep(120)
                    continue
                
                client, err = await connect_client()
                if client is None:
                    fail_streak += 1
                    if fail_streak >= 5:
                        wait_s = 3600   # 60 دقيقة
                    elif fail_streak >= 3:
                        wait_s = 1800   # 30 دقيقة
                    elif fail_streak >= 2:
                        wait_s = 900    # 15 دقيقة
                    else:
                        wait_s = 300    # 5 دقائق
                    
                    print(f"[BOT] connect failed ({err}) - attempt {fail_streak} - waiting {wait_s}s to protect IP", flush=True)
                    await asyncio.sleep(wait_s)
                    continue
                
                fail_streak = 0
            
            cycle += 1
            print(f"[BOT] cycle {cycle} - {datetime.now().strftime('%H:%M:%S')}", flush=True)
            
            top_assets = await get_top_open_assets(client)
            if not top_assets:
                print("[BOT] no open assets this cycle", flush=True)
                await asyncio.sleep(LEAD_TIME_SEC)
                continue

            best = await pick_best_signal(client, top_assets)
            if best is None:
                print("[BOT] no strong signal this cycle", flush=True)
                await asyncio.sleep(LEAD_TIME_SEC)
                continue

            asset, payout, strength, direction = best
            print(f"[BOT] signal: {asset} {direction} (strength {strength:.2f})", flush=True)

            entry_time = (datetime.now() + timedelta(seconds=LEAD_TIME_SEC)).strftime("%H:%M")
            
            if get_setting("admin_bot_enabled", "1") == "1":
                send_telegram(asset, direction, entry_time, TRADE_DURATION_MIN)
            else:
                print("[BOT] telegram disabled by admin - not sending", flush=True)

            if get_setting("admin_auto_enabled", "1") == "1":
                trade_dir = "call" if direction == "up" else "put"
                copy_manager.submit(copy_manager.broadcast_signal(
                    asset, trade_dir, TRADE_DURATION_MIN * 60, LEAD_TIME_SEC
                ))
            else:
                print("[BOT] auto trading disabled by admin - not copying", flush=True)

            wait_seconds = (TRADE_DURATION_MIN * 60) + LEAD_TIME_SEC
            print(f"[BOT] waiting {wait_seconds}s", flush=True)
            await asyncio.sleep(wait_seconds)

        except Exception as e:
            print(f"[BOT] loop error: {e}, reconnecting in 15 minutes", flush=True)
            client = None
            fail_streak += 1
            await asyncio.sleep(900)


if __name__ == "__main__":
    copy_manager.start_background_loop()
    keep_alive()

    signals_enabled = os.environ.get("ENABLE_SIGNALS", "false").lower() == "true"

    if not signals_enabled:
        print("=" * 70, flush=True)
        print("ℹ️  Signal bot DISABLED (ENABLE_SIGNALS != true)", flush=True)
        print("ℹ️  Web + copy trading still work normally", flush=True)
        print("=" * 70, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        print("=" * 70, flush=True)
        print("✅ Signal bot ENABLED", flush=True)
        print("ℹ️  Will wait until master account is linked", flush=True)
        print("=" * 70, flush=True)
        try:
            asyncio.run(main_loop())
        except KeyboardInterrupt:
            print("stopped")
