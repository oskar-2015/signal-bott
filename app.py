# -*- coding: utf-8 -*-
import os
import secrets
import re
import json as json_lib
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, make_response
)

from database import (
    init_db, get_db, get_setting, set_setting, get_secret_setting,
    set_secret_setting, save_user_session, clear_user_session,
)
from license_manager import (
    generate_key, validate_key, get_all_licenses,
    deactivate_key, reactivate_key, delete_key, reset_device,
    is_key_valid, KEY_DURATION_OPTIONS,
)
from quotex_manager import copy_manager
from assets import OTC_ASSETS, REAL_ASSETS, EXPIRY_OPTIONS

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DEVICE_COOKIE = "oscar_device"
APK_DOWNLOAD_URL = os.environ.get("APK_DOWNLOAD_URL", "#")

init_db()

@app.after_request
def add_no_cache(response):
    if request.path.startswith("/admin") or request.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

def clear_user_cache(user_id):
    try: copy_manager.clear_cache(user_id)
    except Exception: pass

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        wants_json = request.path.startswith("/api/")
        key = session.get("license_key")
        if not key:
            if wants_json: return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("index"))
        ok, msg = is_key_valid(key)
        if not ok:
            session.clear()
            if wants_json: return jsonify({"ok": False, "error": msg}), 401
            flash(msg, "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin"))
        return f(*args, **kwargs)
    return decorated

def get_user(key):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE license_key = ?", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None

def ensure_user(key):
    user = get_user(key)
    if user is None:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO users (license_key) VALUES (?)", (key,))
        conn.commit()
        conn.close()
        user = get_user(key)
    return user

@app.route("/health")
def health(): return "ok"

@app.route("/")
def index():
    if session.get("license_key"): return redirect(url_for("dashboard"))
    return render_template("index.html", apk_url=APK_DOWNLOAD_URL)

@app.route("/activate", methods=["POST"])
def activate():
    key = request.form.get("license_key", "").strip().upper()
    device_id = request.cookies.get(DEVICE_COOKIE) or secrets.token_hex(16)
    result = validate_key(key, device_id=device_id)
    if not result["success"]:
        flash(result["error"], "error")
        return redirect(url_for("index"))
    session["license_key"] = key
    session.permanent = True
    ensure_user(key)
    resp = make_response(redirect(url_for("dashboard")))
    resp.set_cookie(DEVICE_COOKIE, device_id, max_age=60 * 60 * 24 * 365 * 2, httponly=True, samesite="Lax", secure=request.headers.get("X-Forwarded-Proto") == "https")
    return resp

@app.route("/dashboard")
@login_required
def dashboard():
    key = session["license_key"]
    user = ensure_user(key)
    return render_template("dashboard.html", user=user, apk_url=APK_DOWNLOAD_URL)

@app.route("/app_login/<key>")
def app_login(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return f"<h2>⚠️ خطأ</h2><p>{msg}</p>", 403
    session["license_key"] = key
    session.permanent = True
    ensure_user(key)
    return redirect(url_for("dashboard"))

def _store_user_session(key, email, ssid, cookies):
    user = get_user(key)
    save_user_session(key, ssid, cookies, email)
    if user: clear_user_cache(user["id"])

# ==================== API للتطبيق المستخدم ====================

@app.route("/api/app/user_info/<key>")
def api_app_user_info(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    user = ensure_user(key)
    return jsonify({
        "ok": True,
        "linked": bool(user.get("quotex_ssid") or user.get("quotex_cookies")),
        "email": user.get("quotex_email") or "",
        "account_type": user.get("account_type") or "demo",
        "trade_mode": user.get("trade_mode") or "percent",
        "trade_value": float(user.get("trade_value") or 1.0),
        "max_loss_value": float(user.get("max_loss_value") or 0),
        "max_profit_value": float(user.get("max_profit_value") or 0),
        "is_copying": bool(user.get("is_copying")),
        "is_auto_trading": bool(user.get("is_auto_trading")),
        "settings_locked": bool(user.get("settings_locked")),
        "start_balance": user.get("start_balance"),
    })

@app.route("/api/app/save_and_enable/<key>", methods=["POST"])
def api_app_save_and_enable(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    data = request.get_json(silent=True) or {}
    
    try:
        account_type = data.get("account_type", "demo")
        trade_mode = data.get("trade_mode", "percent")
        trade_value = float(data.get("trade_value", 1.0))
        max_loss_value = float(data.get("max_loss_value", 0))
        max_profit_value = float(data.get("max_profit_value", 0))
        enable_type = data.get("enable_type", "copy") 
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "قيم غير صحيحة"}), 400

    if account_type not in ("demo", "live"): account_type = "demo"
    if trade_mode not in ("percent", "fixed"): trade_mode = "percent"
    if trade_mode == "percent":
        if trade_value < 0.1 or trade_value > 100: return jsonify({"ok": False, "error": "النسبة بين 0.1 و 100"}), 400
    elif trade_value < 1: return jsonify({"ok": False, "error": "المبلغ الثابت 1$ على الأقل"}), 400
    if max_loss_value < 0 or max_profit_value < 0: return jsonify({"ok": False, "error": "القيم يجب أن تكون موجبة"}), 400

    user = get_user(key)
    if not user: return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 400

    is_copying = 1 if enable_type == "copy" else 0
    is_auto_trading = 1 if enable_type == "auto" else 0

    try:
        stats = copy_manager.run_async(copy_manager.get_user_stats(user), timeout=70)
    except Exception as e:
        return jsonify({"ok": False, "error": f"تعذر جلب الرصيد: {e}"}), 400

    if not (stats.get("ok") and stats["balance"] > 0):
        return jsonify({"ok": False, "error": "الرصيد صفر أو تعذر جلبه"}), 400

    new_start = stats["balance"]

    conn = get_db()
    conn.execute(
        "UPDATE users SET account_type = ?, trade_mode = ?, trade_value = ?, "
        "max_loss_value = ?, max_profit_value = ?, settings_locked = 1, "
        "is_copying = ?, is_auto_trading = ?, start_balance = ?, last_notification = NULL, "
        "balance_version = balance_version + 1 WHERE license_key = ?",
        (account_type, trade_mode, trade_value, max_loss_value, max_profit_value,
         is_copying, is_auto_trading, new_start, key),
    )
    conn.commit()
    conn.close()
    if user: clear_user_cache(user["id"])
    return jsonify({"ok": True, "is_copying": bool(is_copying), "is_auto_trading": bool(is_auto_trading), "start_balance": new_start})

@app.route("/api/app/stop_trading/<key>", methods=["POST"])
def api_app_stop_trading(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    user = get_user(key)
    if not user: return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 400

    conn = get_db()
    conn.execute(
        "UPDATE users SET is_copying = 0, is_auto_trading = 0, settings_locked = 0, "
        "start_balance = NULL, last_notification = NULL, balance_version = balance_version + 1 WHERE license_key = ?",
        (key,),
    )
    conn.commit()
    conn.close()
    if user: clear_user_cache(user["id"])
    return jsonify({"ok": True})

@app.route("/api/app/session_stats/<key>")
def api_app_session_stats(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    user = get_user(key)
    if not user or (not user["quotex_ssid"] and not user["quotex_cookies"]):
        return jsonify({"ok": False, "error": "غير مرتبط"}), 400
    return jsonify(copy_manager.fetch_stats_sync(user))

@app.route("/api/app/unlink/<key>", methods=["POST"])
def api_app_unlink(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    user = get_user(key)
    clear_user_session(key)
    if user: clear_user_cache(user["id"])
    return jsonify({"ok": True})

# ==================== API للماستر ====================

@app.route("/api/admin/is_linked")
def api_admin_is_linked():
    ssid = get_secret_setting("master_ssid")
    is_valid_ssid = bool(ssid and not (ssid.startswith('<') or 'object at 0x' in ssid))
    print(f"[IS_LINKED] master_ssid exists={bool(ssid)}, valid={is_valid_ssid}", flush=True)
    return jsonify({
        "linked": is_valid_ssid,
        "account_type": get_setting("master_account_type", "demo"),
    })

@app.route("/api/admin/link_master", methods=["POST"])
def api_admin_link_master():
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid", "")).strip()
    cookies = str(data.get("cookies", "")).strip()
    
    if not ssid and not cookies:
        return jsonify({"ok": False, "error": "لا توجد جلسة"}), 400
        
    try:
        check = copy_manager.run_async(copy_manager.test_session(ssid or "", cookies or None), timeout=90)
    except Exception as e:
        check = {"success": False, "error": str(e)}
        
    if not check.get("success"):
        return jsonify({"ok": False, "error": check.get("error", "الجلسة لا تعمل")}), 400
        
    final_ssid = check.get("ssid", "") or ssid
    set_secret_setting("master_ssid", final_ssid)
    set_secret_setting("master_cookies", cookies)
    print(f"[LINK_MASTER] Saved ssid prefix={final_ssid[:20] if final_ssid else 'none'}", flush=True)
    return jsonify({"ok": True})

@app.route("/api/admin/raw", methods=["POST"])
def api_admin_raw():
    """يستقبل كل رسالة خام من التطبيق للتشخيص."""
    data = request.get_json(silent=True) or {}
    raw = str(data.get("raw", ""))[:600]
    print(f"[RAW] {raw}", flush=True)
    return jsonify({"ok": True})

@app.route("/api/admin/realtime_trade", methods=["POST"])
def api_admin_realtime_trade():
    data = request.get_json(silent=True) or {}
    
    # إذا وصلت رسالة خام، نحللها هنا
    raw = str(data.get("raw", "")).strip()
    if raw:
        # استخراج JSON من الرسالة
        m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw)
        if not m:
            print(f"[REALTIME] No JSON in raw: {raw[:100]}", flush=True)
            return jsonify({"ok": False, "error": "no json"}), 400
        try:
            p = json_lib.loads(m.group(0))
        except Exception as e:
            print(f"[REALTIME] JSON parse error: {e} | raw={raw[:200]}", flush=True)
            return jsonify({"ok": False, "error": "parse fail"}), 400
    else:
        p = data
    
    asset = p.get("asset") or p.get("symbol") or p.get("instrument") or
