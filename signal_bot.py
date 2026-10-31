# -*- coding: utf-8 -*-
import os
import time
import random
import asyncio
from threading import Thread
from datetime import datetime, timedelta

import pandas as pd
from waitress import serve
from pyquotex.stable_api import Quotex

from app import app as web_app
from quotex_manager import copy_manager
from database import get_setting, set_setting, get_secret_setting
from notify import send_telegram_text, telegram_configured

NUM_PAIRS = 15
TRADE_DURATION_MIN = 1
LEAD_TIME_SEC = 60
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
MIN_PAYOUT = 60
CANDLE_HISTORY_SECONDS = 10800
MIN_STRENGTH = 12
RECONNECT_AFTER_CYCLES = 30

PLATFORM_LINK = "https://broker-qx.pro/sign-up/?lid=2384798"
CHANNEL_LINK = 'قناة أوسكار 🔥'

CRYPTO_SYMBOLS = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "DOT", "LTC", "BCH", "ETC",
    "BNB", "TON", "DOGE", "AVA", "AVAX", "ATO", "ATOM", "DAS", "DASH",
    "ZEC", "XLM", "TRX", "LINK", "UNI", "AXS", "MATIC", "SHIB", "TRUMP"
]


# ───────────────────────── أدوات مساعدة آمنة ─────────────────────────
def report(key, value):
    """يكتب حالة البوت ليظهرها المشرف في اللوحة. لا يوقف البوت إذا فشلت الكتابة."""
    try:
        set_setting(key, str(value))
    except Exception as e:
        print(f"[BOT] status write failed: {e}", flush=True)


def set_status(text):
    print(f"[BOT] {text}", flush=True)
    report("bot_status", text)
    report("bot_status_at", datetime.utcnow().isoformat(timespec="seconds"))


def safe_setting(key, default):
    try:
        return get_setting(key, default)
    except Exception as e:
        print(f"[BOT] read setting {key} failed: {e}", flush=True)
        return default


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


# ───────────────────────── التحليل ─────────────────────────
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
        if not closes:
            return "up", 0
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


# ───────────────────────── تيليجرام ─────────────────────────
def send_telegram(asset, direction, entry_time, duration_min):
    if not telegram_configured():
        print("[TG] skipped: missing credentials", flush=True)
        return False
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
    ok, info = send_telegram_text(text)
    print(f"[TG] {'sent' if ok else 'FAILED'}: {asset} {direction} ({info})", flush=True)
    return ok


# ───────────────────────── الاتصال بـ Quotex ─────────────────────────
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


async def close_client(client):
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        pass


async def get_top_open_assets(client, n=NUM_PAIRS, min_payout=MIN_PAYOUT):
    try:
        all_assets = client.get_all_asset_name()
    except Exception as e:
        print(f"[ASSETS] get_all_asset_name error: {e}", flush=True)
        return []

    if not all_assets:
        print("[ASSETS] empty list - reconnect needed", flush=True)
        return []

    candidates = []
    checked = 0
    failed = 0
    not_open = 0
    low_payout = 0
    for item in all_assets:
        try:
            name = item[0]
        except Exception:
            continue
        if is_crypto(name):
            continue
        checked += 1
        try:
            result = await asyncio.wait_for(client.check_asset_open(name), timeout=8)
            is_open = result[1][2] if len(result) > 1 else False
            payout = result[0][5] if len(result[0]) > 5 else 0
            if not is_open:
                not_open += 1
                continue
            if payout < min_payout:
                low_payout += 1
                continue
            candidates.append((name, payout))
        except asyncio.TimeoutError:
            failed += 1
            continue
        except Exception:
            failed += 1
            continue

    print(f"[ASSETS] checked={checked} failed={failed} not_open={not_open} low_payout={low_payout} candidates={len(candidates)}", flush=True)
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:n]


async def get_closes(client, asset):
    try:
        candles = await asyncio.wait_for(
            client.get_historical_candles(asset, amount_of_seconds=CANDLE_HISTORY_SECONDS, period=60, max_workers=3),
            timeout=15
        )
    except Exception:
        try:
            candles = await asyncio.wait_for(
                client.get_candles(asset, int(time.time()), CANDLE_HISTORY_SECONDS, 60),
                timeout=15
            )
        except Exception:
            return []
    return clean_candles(candles)


async def pick_best_signal(client, assets):
    best = None
    for name, payout in assets:
        try:
            closes = await get_closes(client, name)
            direction, strength = analyze_strength(closes)
        except Exception as e:
            print(f"[PICK] {name} candles error: {e}", flush=True)
            continue
        if strength < MIN_STRENGTH:
            continue
        if best is None or strength > best[2]:
            best = (name, payout, strength, direction)
    return best


def backoff_seconds(failures):
    if failures >= 5:
        return 3600
    if failures >= 3:
        return 1800
    if failures >= 2:
        return 900
    return 300


async def main_loop():
    client = None
    cycle = 0
    consecutive_failures = 0
    cycles_with_client = 0

    while True:
        try:
            # إذا كان Quotex حاظراً الاتصال مؤقتاً (429) ننتظر ولا نضغط عليه
            blocked = copy_manager.rate_limit_remaining()
            if blocked > 0 and client is None:
                set_status(f"Quotex حظر الاتصال مؤقتاً (429) — انتظار {blocked // 60 + 1} دقيقة")
                await asyncio.sleep(min(blocked + 5, 600))
                continue

            if client is not None and cycles_with_client >= RECONNECT_AFTER_CYCLES:
                print(f"[BOT] forcing reconnect after {cycles_with_client} cycles", flush=True)
                await close_client(client)
                client = None
                cycles_with_client = 0

            if client is None:
                _ck, _ss = load_master_session()
                if not _ss:
                    set_status("بانتظار ربط حساب المشرف (من تطبيق المدير)")
                    consecutive_failures = 0
                    await asyncio.sleep(120)
                    continue

                set_status("جاري الاتصال بـ Quotex...")
                client, err = await connect_client()
                if client is None:
                    consecutive_failures += 1
                    if copy_manager.is_rate_limit_error(err) or copy_manager.rate_limit_remaining() > 0:
                        copy_manager.sniffed_429()
                    wait_s = backoff_seconds(consecutive_failures)
                    wait_s = max(wait_s, copy_manager.rate_limit_remaining())
                    wait_s += random.randint(0, 60)
                    set_status(f"فشل الاتصال ({str(err)[:80]}) — محاولة بعد {wait_s // 60} دقيقة")
                    await asyncio.sleep(wait_s)
                    continue
                consecutive_failures = 0
                cycles_with_client = 0
                copy_manager.note_connect_ok()

            cycles_with_client += 1
            cycle += 1
            set_status(f"يعمل — الدورة {cycle}، يبحث عن إشارة")

            top_assets = await get_top_open_assets(client)
            if not top_assets:
                print("[BOT] no candidates - reconnect next cycle", flush=True)
                await close_client(client)
                client = None
                await asyncio.sleep(LEAD_TIME_SEC * 2)
                continue

            best = await pick_best_signal(client, top_assets)
            if best is None:
                set_status("يعمل — لا توجد إشارة قوية في هذه الدورة")
                await asyncio.sleep(LEAD_TIME_SEC)
                continue

            asset, payout, strength, direction = best
            print(f"[BOT] SIGNAL: {asset} {direction} strength={strength:.2f} payout={payout}", flush=True)

            entry_time = (datetime.now() + timedelta(seconds=LEAD_TIME_SEC)).strftime("%H:%M")
            sent = False

            if safe_setting("admin_bot_enabled", "1") == "1":
                sent = send_telegram(asset, direction, entry_time, TRADE_DURATION_MIN)
            else:
                print("[BOT] telegram disabled by admin", flush=True)

            if safe_setting("admin_auto_enabled", "1") == "1":
                trade_dir = "call" if direction == "up" else "put"
                copy_manager.submit(copy_manager.broadcast_signal(
                    asset, trade_dir, TRADE_DURATION_MIN * 60, LEAD_TIME_SEC
                ))
            else:
                print("[BOT] auto trading disabled by admin", flush=True)

            arrow = "صعود" if direction == "up" else "هبوط"
            report("last_signal", f"{asset} — {arrow} — {'أُرسلت لتيليجرام' if sent else 'لم تُرسل لتيليجرام'}")
            report("last_signal_at", datetime.utcnow().isoformat(timespec="seconds"))

            wait_seconds = (TRADE_DURATION_MIN * 60) + LEAD_TIME_SEC
            await asyncio.sleep(wait_seconds)

        except Exception as e:
            consecutive_failures += 1
            wait_s = min(300 * consecutive_failures, 1800)
            set_status(f"خطأ غير متوقع ({str(e)[:80]}) — إعادة المحاولة بعد {wait_s // 60} دقيقة")
            await close_client(client)
            client = None
            await asyncio.sleep(wait_s)


if __name__ == "__main__":
    copy_manager.start_background_loop()
    keep_alive()

    signals_enabled = os.environ.get("ENABLE_SIGNALS", "false").lower() == "true"

    if not signals_enabled:
        print("=" * 70, flush=True)
        print("ℹ️  Signal bot DISABLED (ENABLE_SIGNALS != true)", flush=True)
        print("ℹ️  Web + copy trading still work normally", flush=True)
        print("=" * 70, flush=True)
        set_status("متوقف — ENABLE_SIGNALS ليست true في Render")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        print("=" * 70, flush=True)
        print("✅ Signal bot ENABLED", flush=True)
        print("=" * 70, flush=True)
        try:
            asyncio.run(main_loop())
        except KeyboardInterrupt:
            print("stopped")
