# -*- coding: utf-8 -*-
import os
import sys
import glob
import json
import time
import asyncio
import threading

def _patch_pyquotex_domain():
    try:
        for module_name in list(sys.modules.keys()):
            if module_name.startswith('pyquotex'):
                mod = sys.modules[module_name]
                for attr_name in dir(mod):
                    try:
                        attr = getattr(mod, attr_name)
                        if isinstance(attr, str) and 'qxbroker.com' in attr:
                            setattr(mod, attr_name, attr.replace('qxbroker.com', 'quotex.com'))
                    except:
                        pass
    except:
        pass

_patch_pyquotex_domain()

os.environ['NO_PROXY'] = '127.0.0.1,localhost'
os.environ['no_proxy'] = '127.0.0.1,localhost'

from pyquotex.stable_api import Quotex
from database import (
    get_db, get_setting, get_secret_setting, get_session_plain, now_iso,
)

HOSTS_TO_TRY = ["quotex.com"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CONNECT_TIMEOUT = 40
STATS_TTL_OK = 180
STATS_TTL_FAIL = 600


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
                if email and isinstance(data, dict):
                    entry = data.get(email) or {}
                    if entry.get("token"):
                        return entry.get("cookies"), entry.get("token")
            except Exception:
                continue
    return None, None


def _extract_ssid_string(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        if obj.startswith('<') and 'object at 0x' in obj:
            return None
        return obj
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode('utf-8')
        except:
            return None
    for attr in ('value', 'token', 'ssid', 'session', 'string', 'text'):
        try:
            if hasattr(obj, attr):
                val = getattr(obj, attr)
                if isinstance(val, str) and val and not (val.startswith('<') and 'object at 0x' in val):
                    return val
        except:
            pass
    return None


class QuotexCopyManager:
    def __init__(self):
        self.master_client = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stats_cache = {}
        self._tasks = set()
        self._last_attempt = {}
        self._fail_count = {}

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

    def set_cached_stats(self, user_id, data, ttl):
        self._stats_cache[user_id] = {"data": data, "ts": time.time(), "ttl": ttl}

    def clear_cache(self, user_id):
        self._stats_cache.pop(user_id, None)

    def fetch_stats_sync(self, user):
        cached = self.get_cached_stats(user["id"])
        if cached:
            return cached
        try:
            stats = self.run_async(self.get_user_stats(user), timeout=90)
        except Exception as e:
            stats = {"ok": False, "error": str(e) or "timeout"}
        ttl = STATS_TTL_OK if stats.get("ok") else STATS_TTL_FAIL
        self.set_cached_stats(user["id"], stats, ttl)
        return stats

    def _make_client(self, ssid, cookies, host):
        client = Quotex(email="", password="", lang="en", host=host)
        args = {"user_agent": USER_AGENT}
        if ssid: args["ssid"] = ssid
        if cookies: args["cookies"] = cookies
        client.set_session(**args)
        return client

    async def _connect_any_host(self, ssid, cookies, user_id=None, bypass_cooldown=False):
        if user_id is not None and not bypass_cooldown:
            now = time.time()
            last = self._last_attempt.get(user_id, 0)
            fails = self._fail_count.get(user_id, 0)
            if fails >= 5:
                cooldown = 3600
            elif fails >= 3:
                cooldown = 1800
            elif fails >= 2:
                cooldown = 900
            elif fails >= 1:
                cooldown = 300
            else:
                cooldown = 0
            if now - last < cooldown:
                wait = int(cooldown - (now - last))
                return None, None, f"cooldown ({wait}s, {fails} prev fails)"
            self._last_attempt[user_id] = now

        last_error = "unknown"
        for host in HOSTS_TO_TRY:
            client = None
            try:
                client = self._make_client(ssid, cookies, host)
                check, msg = await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                if check:
                    extracted = ssid or None
                    if not extracted:
                        try:
                            api = getattr(client, 'api', None)
                            if api and hasattr(api, 'ssid'):
                                extracted = _extract_ssid_string(api.ssid)
                        except:
                            pass
                    if user_id is not None and not bypass_cooldown:
                        self._fail_count[user_id] = 0
                    return client, extracted, "OK"
                last_error = str(msg)
                await self._safe_close(client)
            except asyncio.TimeoutError:
                last_error = "timeout"
                if client: await self._safe_close(client)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if client: await self._safe_close(client)

        if user_id is not None and not bypass_cooldown:
            self._fail_count[user_id] = self._fail_count.get(user_id, 0) + 1
        return None, None, last_error

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
        except:
            return False

    async def _get_balance(self, client):
        try:
            bal = await client.get_balance()
            return float(bal) if bal else 0.0
        except:
            return 0.0

    async def test_session(self, ssid, cookies=None):
        client, _, msg = await self._connect_any_host(ssid or "", cookies, user_id="test_" + (ssid or "ck")[:20])
        if client is None:
            return {"success": False, "error": msg}
        try:
            balance = await self._get_balance(client)
            return {"success": True, "balance": balance, "ssid": ssid}
        finally:
            await self._safe_close(client)

    async def connect_master(self):
        ssid = get_secret_setting("master_ssid")
        cookies = get_secret_setting("master_cookies")
        if ssid and (ssid.startswith('<') or 'object at 0x' in ssid):
            ssid = None
        if not ssid and not cookies:
            return False, "master_not_linked"
        if self.master_client:
            await self._safe_close(self.master_client)
            self.master_client = None
        client, _, msg = await self._connect_any_host(ssid or "", cookies, user_id="master")
        if client is None:
            return False, msg
        acct = get_setting("master_account_type", "demo")
        await self._switch_account(client, acct)
        self.master_client = client
        return True, "OK"

    async def get_user_stats(self, user):
        ssid, cookies = get_session_plain(user)
        if ssid and (ssid.startswith('<') or 'object at 0x' in ssid):
            ssid = None
        if not ssid and not cookies:
            return {"ok": False, "error": "no_session"}
        client, _, msg = await self._connect_any_host(ssid or "", cookies, user_id=user.get("id"))
        if client is None:
            return {"ok": False, "error": msg}
        try:
            await self._switch_account(client, user.get("account_type", "demo"))
            balance = await self._get_balance(client)
            start = user.get("start_balance") or balance
            diff = balance - start
            pct = (diff / start * 100) if start else 0.0
            return {
                "ok": True,
                "balance": round(balance, 2),
                "start": round(start, 2),
                "diff": round(diff, 2),
                "pct": round(pct, 2),
                "is_copying": bool(user.get("is_copying")),
                "is_auto_trading": bool(user.get("is_auto_trading")),
                "notification": user.get("last_notification"),
            }
        finally:
            await self._safe_close(client)

    def _check_limits(self, user, balance):
        start = user.get("start_balance")
        if not start or start <= 0:
            return False, ""
        if not user.get("is_copying") and not user.get("is_auto_trading"):
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
        tail = f"\nرصيد البداية: ${start:.2f} ← الحالي: ${balance:.2f}"
        if loss_hit:
            return True, f"⚠️ تم الوصول لحد الخسارة وإيقاف النسخ تلقائياً.\nخسارة الجلسة: {loss_txt}{tail}"
        if profit_hit:
            return True, f"🎉 تم الوصول لهدف الربح وإيقاف النسخ تلقائياً.\nربح الجلسة: {profit_txt}{tail}"
        return False, ""

    def _stop_trading(self, user_id, notification):
        conn = get_db()
        conn.execute(
            "UPDATE users SET is_copying = 0, is_auto_trading = 0, settings_locked = 0, "
            "last_notification = ?, balance_version = balance_version + 1 WHERE id = ?",
            (notification, user_id),
        )
        conn.commit()
        conn.close()
        print(f"[STOP_LIMIT] user {user_id} stopped", flush=True)

    async def refresh_and_notify(self, user):
        try:
            stats = await self.get_user_stats(user)
            if not stats.get("ok"):
                return stats
            stop, note = self._check_limits(user, stats["balance"])
            if stop:
                self._stop_trading(user["id"], note)
                stats["is_copying"] = False
                stats["is_auto_trading"] = False
                stats["notification"] = note
                self.clear_cache(user["id"])
            self.set_cached_stats(user["id"], stats, STATS_TTL_OK)
            return stats
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _refresh_after_trade(self, delay, license_keys):
        try:
            await asyncio.sleep(delay + 8)
            for key in license_keys:
                try:
                    conn = get_db()
                    row = conn.execute("SELECT * FROM users WHERE license_key = ?", (key,)).fetchone()
                    conn.close()
                    if not row or not row["quotex_ssid"]:
                        continue
                    user_dict = dict(row)
                    await self.refresh_and_notify(user_dict)
                except Exception as e:
                    print(f"[REFRESH] {key} failed: {e}", flush=True)
        except Exception as e:
            print(f"[REFRESH_AFTER] {e}", flush=True)

    async def _buy_once(self, client, asset, direction, amount, expiry, time_mode):
        try:
            return await client.buy(amount=amount, asset=asset, direction=direction, duration=expiry, time_mode=time_mode)
        except TypeError:
            return await client.buy(amount=amount, asset=asset, direction=direction, duration=expiry)

    async def _buy(self, client, asset, direction, amount, expiry, time_mode="TIMER"):
        try:
            result = await self._buy_once(client, asset, direction, amount, expiry, time_mode)
            if result[0]: return (True, result[1], asset)
        except Exception as e:
            print(f"Buy error: {e}", flush=True)
        if asset.endswith("_otc"):
            alt = asset.replace("_otc", "")
            try:
                result = await self._buy_once(client, alt, direction, amount, expiry, time_mode)
                if result[0]: return (True, result[1], alt)
            except:
                pass
        return (False, "فشلت", asset)

    async def copy_to_follower(self, follower, asset, direction, expiry, time_mode, bypass_cooldown=False):
        ssid, cookies = get_session_plain(follower)
        if ssid and (ssid.startswith('<') or 'object at 0x' in ssid):
            ssid = None
        if not ssid and not cookies:
            return {"success": False, "error": "no_session"}
        user_id = follower["id"]
        account_type = follower.get("account_type", "demo")
        acct_label = "تجريبي" if account_type == "demo" else "حقيقي"

        def save_notification(msg):
            try:
                conn = get_db()
                conn.execute("UPDATE users SET last_notification = ?, balance_version = balance_version + 1 WHERE id = ?", (msg, user_id))
                conn.commit()
                conn.close()
            except:
                pass

        client, _, msg = await self._connect_any_host(ssid or "", cookies, user_id=user_id, bypass_cooldown=bypass_cooldown)
        if client is None:
            print(f"[COPY] user {user_id}: connect failed ({msg})", flush=True)
            return {"success": False, "error": msg}

        try:
            await self._switch_account(client, account_type)
            balance = await self._get_balance(client)

            start_balance = follower.get("start_balance")
            if not start_balance or start_balance <= 0:
                start_balance = balance
                conn = get_db()
                conn.execute("UPDATE users SET start_balance = ? WHERE id = ?", (start_balance, user_id))
                conn.commit()
                conn.close()
                follower["start_balance"] = start_balance

            stop, note = self._check_limits(follower, balance)
            if stop:
                self._stop_trading(user_id, note)
                self.clear_cache(user_id)
                return {"success": False, "error": "limit_reached"}

            tv = follower.get("trade_value") or 1.0
            if (follower.get("trade_mode") or "percent") == "fixed":
                amount = tv
            else:
                amount = round(balance * (tv / 100.0), 2)
            if amount < 1: amount = 1.0

            if amount > balance:
                save_notification(f"⚠️ رصيد غير كافٍ في الحساب ({acct_label}).\nالمطلوب: ${amount:.2f}\nالرصيد: ${balance:.2f}")
                print(f"[COPY] user {user_id}: insufficient balance ({balance:.2f} < {amount:.2f})", flush=True)
                return {"success": False, "error": "insufficient_balance"}

            status, info, _ = await self._buy(client, asset, direction, amount, expiry, time_mode)
            if status:
                print(f"[COPY] ✅ user {user_id} ({acct_label}): ${amount:.2f} opened", flush=True)
                return {"success": True, "order_id": info.get("id", "N/A") if isinstance(info, dict) else "N/A", "amount": amount}
            else:
                save_notification(f"⚠️ فشل تنفيذ الصفقة على الحساب ({acct_label}).\nالسبب: {info}")
                print(f"[COPY] ❌ user {user_id} ({acct_label}): buy failed ({info})", flush=True)
                return {"success": False, "error": str(info)}
        except Exception as e:
            print(f"[COPY] user {user_id} exception: {e}", flush=True)
            return {"success": False, "error": str(e)}
        finally:
            await self._safe_close(client)

    def _active_copy_followers(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT u.* FROM users u JOIN licenses l ON l.key = u.license_key "
            "WHERE u.is_copying = 1 AND u.quotex_ssid IS NOT NULL AND u.quotex_ssid != '' "
            "AND l.is_active = 1 AND (l.expires_at IS NULL OR l.expires_at > ?)",
            (now_iso(),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _active_auto_traders(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT u.* FROM users u JOIN licenses l ON l.key = u.license_key "
            "WHERE u.is_auto_trading = 1 AND u.quotex_ssid IS NOT NULL AND u.quotex_ssid != '' "
            "AND l.is_active = 1 AND (l.expires_at IS NULL OR l.expires_at > ?)",
            (now_iso(),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _log_trade(self, asset, direction, amount, expiry, order_id, count):
        conn = get_db()
        conn.execute(
            "INSERT INTO trade_logs (asset, direction, amount, expiry, master_order_id, follower_count) VALUES (?, ?, ?, ?, ?, ?)",
            (asset, direction, amount, expiry, str(order_id), count),
        )
        conn.commit()
        conn.close()

    async def _copy_all(self, followers, asset, direction, expiry, time_mode, bypass_cooldown=False):
        results = await asyncio.gather(
            *[self.copy_to_follower(f, asset, direction, expiry, time_mode, bypass_cooldown) for f in followers],
            return_exceptions=True,
        )
        return sum(1 for r in results if isinstance(r, dict) and r.get("success"))

    async def broadcast_trade(self, asset, direction, master_pct, expiry, time_mode="TIMER", skip_master=False):
        if not skip_master:
            ok, msg = await self.connect_master()
            if not ok: return {"success": False, "error": msg}
            balance = await self._get_balance(self.master_client)
            if balance <= 0: return {"success": False, "error": "no_balance"}
            master_amount = max(1.0, round(balance * (master_pct / 100.0), 2))
            master = await self.open_master_trade(asset, direction, master_amount, expiry, time_mode)
            if not master["success"]: return master
            actual_asset = master.get("actual_asset", asset)
            order_id = master["order_id"]
            amount_used = master_amount
        else:
            actual_asset = asset
            order_id = "realtime_browser"
            amount_used = master_pct

        followers = self._active_copy_followers()
        copied = await self._copy_all(followers, actual_asset, direction, expiry, time_mode, bypass_cooldown=True) if followers else 0
        self._log_trade(actual_asset, direction, amount_used, expiry, order_id, copied)
        if followers:
            self._track(asyncio.create_task(self._refresh_after_trade(expiry, [f["license_key"] for f in followers])))
        return {
            "success": True, "master_order_id": order_id,
            "master_amount": amount_used, "actual_asset": actual_asset,
            "copied_count": copied, "total_followers": len(followers),
        }

    async def open_master_trade(self, asset, direction, amount, expiry, time_mode):
        if not self.master_client:
            ok, msg = await self.connect_master()
            if not ok: return {"success": False, "error": msg, "actual_asset": asset}
        balance = await self._get_balance(self.master_client)
        if balance <= 0:
            return {"success": False, "error": "no_balance", "actual_asset": asset}
        status, info, actual_asset = await self._buy(self.master_client, asset, direction, amount, expiry, time_mode)
        if status:
            return {"success": True, "order_id": info.get("id", "N/A") if isinstance(info, dict) else "N/A", "actual_asset": actual_asset}
        return {"success": False, "error": str(info), "actual_asset": asset}

    async def broadcast_signal(self, asset, direction, expiry=60, lead_sec=60):
        try:
            await asyncio.sleep(lead_sec)
            followers = self._active_auto_traders()
            print(f"[SIGNAL] {asset} {direction} -> {len(followers)} auto-traders", flush=True)
            if not followers:
                print(f"[SIGNAL] No auto-traders enabled, skipping", flush=True)
                return
            copied = await self._copy_all(followers, asset, direction, expiry, "TIMER", bypass_cooldown=True)
            print(f"[SIGNAL] Copied {copied}/{len(followers)}", flush=True)
            self._log_trade(asset, direction, 0, expiry, "signal", copied)
            if followers:
                self._track(asyncio.create_task(self._refresh_after_trade(expiry, [f["license_key"] for f in followers])))
        except Exception as e:
            print(f"[SIGNAL] FATAL: {e}", flush=True)


copy_manager = QuotexCopyManager()
