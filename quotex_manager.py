# -*- coding: utf-8 -*-
import os
import glob
import json
import time
import asyncio
import threading

from pyquotex.stable_api import Quotex
from database import (
    get_db, get_setting, get_secret_setting, get_session_plain, now_iso,
)

QX_HOST = os.environ.get("QX_HOST", "qxbroker.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CONNECT_TIMEOUT = 45
STATS_TTL = 120


def _read_session_file(email):
    folders = [os.getcwd()]
    try:
        import pyquotex
        folders.append(os.path.dirname(pyquotex.__file__))
    except Exception:
        pass
    for folder in folders:
        for path in glob.glob(os.path.join(folder, "**", "session*.json"), recursive=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entry = data.get(email) or {}
                if entry.get("token"):
                    return entry.get("cookies"), entry.get("token")
            except Exception:
                continue
    return None, None


class QuotexCopyManager:
    def __init__(self):
        self.master_client = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stats_cache = {}
        self._tasks = set()

    def start_background_loop(self):
        if self._thread and self._thread.is_alive():
            return

        def run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def run_async(self, coro, timeout=300):
        self.start_background_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    def submit(self, coro):
        self.start_background_loop()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _track(self, task):
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def get_cached_stats(self, user_id):
        item = self._stats_cache.get(user_id)
        if not item:
            return None
        if time.time() - item["ts"] > item["ttl"]:
            return None
        return item["data"]

    def set_cached_stats(self, user_id, data, ttl=STATS_TTL):
        self._stats_cache[user_id] = {"data": data, "ts": time.time(), "ttl": ttl}

    def clear_cache(self, user_id):
        self._stats_cache.pop(user_id, None)

    def fetch_stats_sync(self, user):
        cached = self.get_cached_stats(user["id"])
        if cached:
            return cached
        try:
            stats = self.run_async(self.get_user_stats(user), timeout=70)
        except Exception as e:
            stats = {"ok": False, "error": str(e) or "انتهت المهلة"}
        self.set_cached_stats(user["id"], stats, ttl=STATS_TTL if stats.get("ok") else 30)
        return stats

    def _make_client(self, ssid, cookies=None):
        client = Quotex(email="", password="", lang="en", host=QX_HOST)
        session_args = {"user_agent": USER_AGENT, "ssid": ssid}
        if cookies:
            session_args["cookies"] = cookies
        client.set_session(**session_args)
        return client

    async def _connect(self, client):
        return await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)

    async def _safe_close(self, client):
        try:
            await client.close()
        except Exception:
            pass

    async def _switch_account(self, client, account_type):
        try:
            target = "PRACTICE" if account_type == "demo" else "REAL"
            await client.change_account(target)
            await asyncio.sleep(1)
            return True
        except Exception as e:
            print(f"  switch account failed ({account_type}): {e}")
            return False

    async def _get_balance(self, client):
        try:
            bal = await client.get_balance()
            return float(bal) if bal else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _extract_session(client, email):
        api = getattr(client, "api", None)
        cookies = None
        ssid = None
        for holder in (client, api):
            sd = getattr(holder, "session_data", None)
            if isinstance(sd, dict):
                cookies = cookies or sd.get("cookies")
                ssid = ssid or sd.get("token") or sd.get("ssid")
        if not ssid:
            ssid = getattr(api, "ssid", None) or getattr(client, "ssid", None)
        if not ssid or not cookies:
            file_cookies, file_ssid = _read_session_file(email)
            cookies = cookies or file_cookies
            ssid = ssid or file_ssid
        return ssid, cookies

    async def login_with_credentials(self, email, password):
        client = Quotex(email=email, password=password, lang="en", host=QX_HOST)
        try:
            check, reason = await asyncio.wait_for(client.connect(), timeout=60)
            if not check:
                return {"success": False, "error": str(reason)}
            ssid, cookies = self._extract_session(client, email)
            if not ssid:
                return {"success": False, "error": "تم تسجيل الدخول لكن لم نستطع قراءة بيانات الجلسة"}
            return {"success": True, "ssid": ssid, "cookies": cookies or ""}
        except asyncio.TimeoutError:
            return {"success": False, "error": "انتهت المهلة، ربما Quotex منع الاتصال أو طلب رمز تحقق"}
        except Exception as e:
            text = f"{type(e).__name__}: {e}"
            if "EOF" in text:
                text += " (Quotex طلب رمز تحقق على البريد)"
            return {"success": False, "error": text}
        finally:
            await self._safe_close(client)

    async def test_session(self, ssid, cookies=None):
        client = None
        try:
            client = self._make_client(ssid, cookies)
            check, msg = await self._connect(client)
            if not check:
                return {"success": False, "error": f"فشل الاتصال: {msg}"}
            balance = await self._get_balance(client)
            return {"success": True, "balance": balance}
        except asyncio.TimeoutError:
            return {"success": False, "error": "انتهت المهلة أثناء الاتصال"}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            if client:
                await self._safe_close(client)

    async def connect_master(self):
        ssid = get_secret_setting("master_ssid")
        cookies = get_secret_setting("master_cookies")
        if not ssid:
            return False, "لم يتم ربط حساب المشرف بعد"

        if self.master_client:
            await self._safe_close(self.master_client)
            self.master_client = None

        try:
            client = self._make_client(ssid, cookies)
            check, msg = await self._connect(client)
            if check:
                acct = get_setting("master_account_type", "demo")
                await self._switch_account(client, acct)
                self.master_client = client
                return True, "OK"
            return False, f"فشل الاتصال: {msg}"
        except Exception as e:
            return False, str(e)

    async def get_user_stats(self, user):
        ssid, cookies = get_session_plain(user)
        if not ssid:
            return {"ok": False, "error": "لا توجد جلسة"}

        client = None
        try:
            client = self._make_client(ssid, cookies)
            check, msg = await self._connect(client)
            if not check:
                return {"ok": False, "error": f"فشل الاتصال: {msg}"}
            await self._switch_account(client, user.get("account_type", "demo"))
            balance = await self._get_balance(client)
            return self._format_stats(user, balance)
        except Exception as e:
            return {"ok": False, "error": str(e) or "خطأ في الاتصال"}
        finally:
            if client:
                await self._safe_close(client)

    def _format_stats(self, user, balance):
        start = user.get("start_balance")
        if not start or start <= 0:
            start = balance
        diff = balance - start
        pct = (diff / start * 100) if start else 0.0
        return {
            "ok": True,
            "balance": round(balance, 2),
            "start": round(start, 2),
            "diff": round(diff, 2),
            "pct": round(pct, 2),
            "is_copying": bool(user.get("is_copying")),
            "notification": user.get("last_notification"),
        }

    def _check_limits(self, user, balance):
        start = user.get("start_balance")
        if not start or start <= 0 or not user.get("is_copying"):
            return False, ""

        diff = balance - start
        pct = diff / start * 100.0
        max_loss = user.get("max_loss_value") or 0
        max_profit = user.get("max_profit_value") or 0

        if (user.get("trade_mode") or "percent") == "fixed":
            loss_hit = max_loss > 0 and diff <= -abs(max_loss)
            profit_hit = max_profit > 0 and diff >= abs(max_profit)
            loss_txt = f"${abs(diff):.2f}"
            profit_txt = f"+${diff:.2f}"
        else:
            loss_hit = max_loss > 0 and pct <= -abs(max_loss)
            profit_hit = max_profit > 0 and pct >= abs(max_profit)
            loss_txt = f"{pct:.2f}% (${abs(diff):.2f})"
            profit_txt = f"+{pct:.2f}% (${diff:.2f})"

        tail = f"\nرصيد البداية: ${start:.2f} ← الرصيد الحالي: ${balance:.2f}"
        if loss_hit:
            return True, f"⚠️ تم الوصول لحد الخسارة وإيقاف النسخ تلقائياً.\nخسارة الجلسة: {loss_txt}{tail}"
        if profit_hit:
            return True, f"🎉 تهانينا! حققت هدف الربح وتم إيقاف النسخ تلقائياً.\nربح الجلسة: {profit_txt}{tail}"
        return False, ""

    def _stop_copying(self, user_id, notification):
        conn = get_db()
        conn.execute(
            "UPDATE users SET is_copying = 0, last_notification = ?, "
            "balance_version = balance_version + 1 WHERE id = ?",
            (notification, user_id),
        )
        conn.commit()
        conn.close()

    async def refresh_and_notify(self, user):
        try:
            stats = await self.get_user_stats(user)
            if not stats.get("ok"):
                return stats

            should_stop, note = self._check_limits(user, stats["balance"])
            if should_stop:
                self._stop_copying(user["id"], note)
                stats["is_copying"] = False
                stats["notification"] = note
            else:
                conn = get_db()
                conn.execute(
                    "UPDATE users SET balance_version = balance_version + 1 WHERE id = ?",
                    (user["id"],),
                )
                conn.commit()
                conn.close()

            self.set_cached_stats(user["id"], stats)
            return stats
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _refresh_after_trade(self, delay, license_keys):
        try:
            await asyncio.sleep(delay + 5)
            for key in license_keys:
                try:
                    conn = get_db()
                    row = conn.execute("SELECT * FROM users WHERE license_key = ?", (key,)).fetchone()
                    conn.close()
                    if row and row["quotex_ssid"]:
                        await self.refresh_and_notify(dict(row))
                except Exception as e:
                    print(f"refresh failed for {key}: {e}")
        except Exception as e:
            print(f"_refresh_after_trade error: {e}")

    async def _buy_once(self, client, asset, direction, amount, expiry, time_mode):
        try:
            return await client.buy(
                amount=amount, asset=asset, direction=direction,
                duration=expiry, time_mode=time_mode,
            )
        except TypeError:
            return await client.buy(
                amount=amount, asset=asset, direction=direction, duration=expiry,
            )

    async def _buy(self, client, asset, direction, amount, expiry, time_mode="TIMER"):
        print(f"  trying: {asset} {direction} ${amount:.2f} ({expiry}s, {time_mode})")
        try:
            result = await self._buy_once(client, asset, direction, amount, expiry, time_mode)
            print(f"  result: {result}")
            if result[0]:
                return (True, result[1], asset)
        except Exception as e:
            print(f"  buy error: {type(e).__name__}: {e}")

        if asset.endswith("_otc"):
            alt = asset.replace("_otc", "")
            try:
                result = await self._buy_once(client, alt, direction, amount, expiry, time_mode)
                print(f"  alt result: {result}")
                if result[0]:
                    return (True, result[1], alt)
            except Exception as e:
                print(f"  alt buy error: {type(e).__name__}: {e}")

        return (False, "فشلت جميع المحاولات", asset)

    async def open_master_trade(self, asset, direction, amount, expiry, time_mode):
        if not self.master_client:
            ok, msg = await self.connect_master()
            if not ok:
                return {"success": False, "error": msg, "actual_asset": asset}

        balance = await self._get_balance(self.master_client)
        if balance <= 0:
            return {"success": False, "error": "لا يوجد رصيد كافٍ في حساب المشرف", "actual_asset": asset}

        status, info, actual_asset = await self._buy(
            self.master_client, asset, direction, amount, expiry, time_mode
        )
        if status:
            order_id = info.get("id", "N/A") if isinstance(info, dict) else "N/A"
            return {"success": True, "order_id": order_id, "actual_asset": actual_asset}
        return {"success": False, "error": str(info), "actual_asset": asset}

    async def copy_to_follower(self, follower, asset, direction, expiry, time_mode):
        ssid, cookies = get_session_plain(follower)
        if not ssid:
            return {"success": False, "error": "لا توجد جلسة"}

        user_id = follower["id"]
        account_type = follower.get("account_type", "demo")
        client = None

        def save_notification(msg):
            try:
                conn = get_db()
                conn.execute(
                    "UPDATE users SET last_notification = ?, "
                    "balance_version = balance_version + 1 WHERE id = ?",
                    (msg, user_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        try:
            client = self._make_client(ssid, cookies)
            check, msg = await self._connect(client)
            if not check:
                return {"success": False, "error": f"فشل الاتصال: {msg}"}

            await self._switch_account(client, account_type)

            balance = await self._get_balance(client)
            if balance <= 0:
                err = "❌ لا يوجد رصيد كافٍ للنسخ"
                save_notification(err)
                return {"success": False, "error": err}

            start_balance = follower.get("start_balance")
            if not start_balance or start_balance <= 0:
                start_balance = balance
                conn = get_db()
                conn.execute("UPDATE users SET start_balance = ? WHERE id = ?", (start_balance, user_id))
                conn.commit()
                conn.close()
                follower["start_balance"] = start_balance

            should_stop, note = self._check_limits(follower, balance)
            if should_stop:
                self._stop_copying(user_id, note)
                self.clear_cache(user_id)
                return {"success": False, "error": "تم الوصول لحد الربح أو الخسارة"}

            trade_value = follower.get("trade_value") or 1.0
            if (follower.get("trade_mode") or "percent") == "fixed":
                amount = trade_value
            else:
                amount = round(balance * (trade_value / 100.0), 2)
            if amount < 1:
                amount = 1.0
            if amount > balance:
                amount = balance

            status, info, _ = await self._buy(client, asset, direction, amount, expiry, time_mode)
            if status:
                order_id = info.get("id", "N/A") if isinstance(info, dict) else "N/A"
                return {"success": True, "order_id": order_id, "amount": amount}
            return {"success": False, "error": str(info)}

        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if client:
                await self._safe_close(client)

    def _active_followers(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT u.* FROM users u JOIN licenses l ON l.key = u.license_key "
            "WHERE u.is_copying = 1 AND u.quotex_ssid IS NOT NULL AND u.quotex_ssid != '' "
            "AND l.is_active = 1 AND (l.expires_at IS NULL OR l.expires_at > ?)",
            (now_iso(),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _log_trade(self, asset, direction, amount, expiry, order_id, count):
        conn = get_db()
        conn.execute(
            "INSERT INTO trade_logs (asset, direction, amount, expiry, master_order_id, follower_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (asset, direction, amount, expiry, str(order_id), count),
        )
        conn.commit()
        conn.close()

    async def _copy_all(self, followers, asset, direction, expiry, time_mode):
        results = await asyncio.gather(
            *[self.copy_to_follower(f, asset, direction, expiry, time_mode) for f in followers],
            return_exceptions=True,
        )
        for f, r in zip(followers, results):
            if not (isinstance(r, dict) and r.get("success")):
                print(f"  copy failed for user {f['id']}: {r if not isinstance(r, dict) else r.get('error')}")
        return sum(1 for r in results if isinstance(r, dict) and r.get("success"))

    async def broadcast_trade(self, asset, direction, master_pct, expiry, time_mode="TIMER"):
        ok, msg = await self.connect_master()
        if not ok:
            return {"success": False, "error": msg}

        balance = await self._get_balance(self.master_client)
        if balance <= 0:
            return {"success": False, "error": "لا يوجد رصيد كافٍ في حساب المشرف"}

        master_amount = max(1.0, round(balance * (master_pct / 100.0), 2))
        master = await self.open_master_trade(asset, direction, master_amount, expiry, time_mode)
        if not master["success"]:
            return master

        actual_asset = master.get("actual_asset", asset)
        followers = self._active_followers()
        copied = await self._copy_all(followers, actual_asset, direction, expiry, time_mode) if followers else 0
        self._log_trade(actual_asset, direction, master_amount, expiry, master["order_id"], copied)

        if followers:
            self._track(asyncio.create_task(
                self._refresh_after_trade(expiry, [f["license_key"] for f in followers])
            ))

        return {
            "success": True,
            "master_order_id": master["order_id"],
            "master_amount": master_amount,
            "actual_asset": actual_asset,
            "copied_count": copied,
            "total_followers": len(followers),
        }

    async def _master_signal_trade(self, asset, direction, expiry):
        try:
            pct = float(get_setting("master_percentage", "10"))
        except ValueError:
            pct = 10.0
        try:
            ok, msg = await self.connect_master()
            if not ok:
                return
            balance = await self._get_balance(self.master_client)
            if balance <= 0:
                return
            amount = max(1.0, round(balance * (pct / 100.0), 2))
            await self.open_master_trade(asset, direction, amount, expiry, "TIMER")
        except Exception as e:
            print(f"master signal trade error: {e}")

    async def broadcast_signal(self, asset, direction, expiry=60, lead_sec=60):
        await asyncio.sleep(lead_sec)

        followers = self._active_followers()
        jobs = []
        if followers:
            jobs.append(self._copy_all(followers, asset, direction, expiry, "TIMER"))
        if get_setting("master_follow_signals", "0") == "1" and get_secret_setting("master_ssid"):
            jobs.append(self._master_signal_trade(asset, direction, expiry))
        if not jobs:
            return

        results = await asyncio.gather(*jobs, return_exceptions=True)
        copied = results[0] if followers and isinstance(results[0], int) else 0

        self._log_trade(asset, direction, 0, expiry, "signal", copied)
        if followers:
            self._track(asyncio.create_task(
                self._refresh_after_trade(expiry, [f["license_key"] for f in followers])
            ))


copy_manager = QuotexCopyManager()
