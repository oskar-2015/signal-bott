# -*- coding: utf-8 -*-
import asyncio
from pyquotex.stable_api import Quotex

import db

TRADE_AMOUNT_USD = 1
TRADE_DURATION_SEC = 60


async def execute_for_user(user_row, asset, direction):
    user_id = user_row["id"]

    # وقف خسارة / وقف ربح: تحقق قبل أي تنفيذ جديد
    pl = user_row["session_pl_usd"]
    sl = user_row["stop_loss_usd"]
    tp = user_row["take_profit_usd"]
    if sl and pl <= -abs(sl):
        db.disable_auto_trade(user_id)
        print(f"user {user_id}: stop-loss reached, auto-trade disabled")
        return
    if tp and pl >= abs(tp):
        db.disable_auto_trade(user_id)
        print(f"user {user_id}: take-profit reached, auto-trade disabled")
        return

    cookies_str, ssid = db.get_decrypted_session(user_row)
    if not cookies_str or not ssid:
        print(f"user {user_id}: no session, skipping")
        return

    client = Quotex(email="x@x.com", password="x", lang="en")
    client.set_session(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        cookies=cookies_str,
        ssid=ssid
    )

    try:
        check, reason = await client.connect()
        if not check:
            print(f"user {user_id}: connect failed ({reason})")
            return

        client.set_account_mode("PRACTICE" if user_row["account_mode"] == "demo" else "REAL")

        status, buy_info = await client.buy(
            TRADE_AMOUNT_USD, asset, direction, TRADE_DURATION_SEC
        )
        if not status:
            print(f"user {user_id}: trade failed to place")
            return

        db.log_trade(user_id, asset, direction, TRADE_AMOUNT_USD, result="pending")

        # ننتظر انتهاء الصفقة ثم نتحقق من النتيجة
        await asyncio.sleep(TRADE_DURATION_SEC + 5)
        win = await client.check_win(buy_info["id"])
        profit = client.get_profit()
        pl_result = profit if win else -TRADE_AMOUNT_USD
        result = "win" if win else "loss"

        db.add_session_pl(user_id, pl_result)
        db.log_trade(user_id, asset, direction, TRADE_AMOUNT_USD, result=result, pl_usd=pl_result)
        print(f"user {user_id}: trade {result}, pl={pl_result}")

    except Exception as e:
        print(f"user {user_id}: error during execution: {e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def execute_signal_for_all_users(asset, direction):
    users = db.get_active_trading_users()
    if not users:
        print("no active trading users")
        return
    print(f"executing signal for {len(users)} user(s)")
    await asyncio.gather(*[
        execute_for_user(u, asset, direction) for u in users
    ], return_exceptions=True)
