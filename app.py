# -*- coding: utf-8 -*-
import os
import secrets
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

# ==================== API للتطبيق ====================

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

# ==================== نقاط النهاية القديمة للويب ====================

@app.route("/auto_login", methods=["POST"])
@login_required
def auto_login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    key = session["license_key"]
    if not email or not password:
        flash("الرجاء إدخال البريد وكلمة المرور", "error")
        return redirect(url_for("dashboard"))
    try:
        result = copy_manager.run_async(copy_manager.login_with_credentials(email, password), timeout=90)
    except Exception as e:
        result = {"success": False, "error": str(e)}
    if result.get("success"):
        _store_user_session(key, email, result["ssid"], result.get("cookies", ""))
        flash("✅ تم ربط الحساب بنجاح", "success")
    else:
        flash(f"❌ فشل الربط: {result.get('error', 'خطأ')}", "error")
    return redirect(url_for("dashboard"))

@app.route("/link_session", methods=["POST"])
@login_required
def link_session():
    key = session["license_key"]
    ssid = request.form.get("ssid", "").strip()
    cookies = request.form.get("cookies", "").strip()
    email = request.form.get("email", "").strip()
    if not ssid and not cookies:
        flash("الرجاء لصق SSID أو Cookies", "error")
        return redirect(url_for("dashboard"))
    try:
        check = copy_manager.run_async(copy_manager.test_session(ssid, cookies or None), timeout=90)
    except Exception as e:
        check = {"success": False, "error": str(e)}
    if check.get("success"):
        _store_user_session(key, email, ssid, cookies)
        flash("✅ تم ربط الحساب بنجاح", "success")
    else:
        flash(f"❌ الجلسة لا تعمل: {check.get('error', 'خطأ')}", "error")
    return redirect(url_for("dashboard"))

@app.route("/api/link", methods=["POST"])
def api_link():
    data = request.get_json(silent=True) or {}
    key = str(data.get("license_key", "")).strip().upper()
    ssid = str(data.get("ssid", "")).strip()
    cookies = str(data.get("cookies", "")).strip()
    email = str(data.get("email", "")).strip()
    ok, msg = is_key_valid(key)
    if not ok: return jsonify({"ok": False, "error": msg}), 403
    if not ssid and not cookies: return jsonify({"ok": False, "error": "لا توجد جلسة"}), 400
    ensure_user(key)
    try:
        check = copy_manager.run_async(copy_manager.test_session(ssid or "", cookies or None), timeout=90)
    except Exception as e:
        check = {"success": False, "error": str(e)}
    if not check.get("success"):
        return jsonify({"ok": False, "error": check.get("error", "الجلسة لا تعمل")}), 400
    final_ssid = check.get("ssid", "") or ssid
    _store_user_session(key, email, final_ssid, cookies)
    return jsonify({"ok": True, "ssid_extracted": bool(final_ssid and not ssid)})

@app.route("/unlink", methods=["POST"])
@login_required
def unlink():
    key = session["license_key"]
    user = get_user(key)
    clear_user_session(key)
    if user: clear_user_cache(user["id"])
    flash("تم فصل الحساب", "success")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ==================== لوحة المشرف ====================

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        given = request.form.get("password", "")
        if ADMIN_PASSWORD and secrets.compare_digest(given, ADMIN_PASSWORD):
            session["is_admin"] = True
            session.permanent = True
            return redirect(url_for("admin_panel"))
        flash("كلمة المرور غير صحيحة", "error")
    if session.get("is_admin"): return redirect(url_for("admin_panel"))
    return render_template("admin_login.html")

@app.route("/admin/panel")
@admin_required
def admin_panel():
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    active_copy = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_copying = 1").fetchone()["c"]
    active_auto = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_auto_trading = 1").fetchone()["c"]
    users_list = conn.execute("SELECT license_key, quotex_email, quotex_ssid, account_type, is_copying, is_auto_trading, trade_mode, trade_value, start_balance FROM users ORDER BY created_at DESC").fetchall()
    logs = conn.execute("SELECT * FROM trade_logs ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    return render_template(
        "admin.html",
        licenses=get_all_licenses(),
        master_email=get_setting("master_email", ""),
        master_linked=bool(get_secret_setting("master_ssid")),
        master_pct=get_setting("master_percentage", "10"),
        master_acct=get_setting("master_account_type", "demo"),
        total_users=total_users,
        active_copy=active_copy,
        active_auto=active_auto,
        users_list=[dict(u) for u in users_list],
        logs=[dict(l) for l in logs],
        otc_assets=OTC_ASSETS,
        real_assets=REAL_ASSETS,
        expiry_options=EXPIRY_OPTIONS,
        key_duration_options=KEY_DURATION_OPTIONS,
    )

@app.route("/admin/create_key", methods=["POST"])
@admin_required
def admin_create_key():
    try: hours = int(request.form.get("hours", 24))
    except ValueError: hours = 24
    result = generate_key(hours)
    if result["success"]: flash(f"✅ المفتاح: {result['key']}", "success")
    else: flash(f"❌ فشل: {result['error']}", "error")
    return redirect(url_for("admin_panel"))

@app.route("/admin/delete_key", methods=["POST"])
@admin_required
def admin_delete_key():
    key = request.form.get("key", "")
    user = get_user(key)
    delete_key(key)
    if user: clear_user_cache(user["id"])
    flash("تم الحذف", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/toggle_key", methods=["POST"])
@admin_required
def admin_toggle_key():
    key = request.form.get("key", "")
    if request.form.get("action") == "activate":
        reactivate_key(key)
        flash("تم التفعيل", "success")
    else:
        deactivate_key(key)
        flash("تم الإيقاف المؤقت", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/reset_device", methods=["POST"])
@admin_required
def admin_reset_device():
    reset_device(request.form.get("key", ""))
    flash("تم إعادة تعيين الجهاز", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/set_master", methods=["POST"])
@admin_required
def admin_set_master():
    email = request.form.get("master_email", "").strip()
    password = request.form.get("master_password", "").strip()
    
    if not email or not password:
        flash("أدخل البريد وكلمة المرور", "error")
        return redirect(url_for("admin_panel"))
    
    try: 
        result = copy_manager.run_async(copy_manager.login_with_credentials(email, password), timeout=90)
    except Exception as e: 
        result = {"success": False, "error": str(e)}
    
    if result.get("success"):
        set_setting("master_email", email)
        set_secret_setting("master_ssid", result["ssid"])
        set_secret_setting("master_cookies", result.get("cookies", ""))
        flash("✅ تم ربط حساب المشرف", "success")
    else:
        flash(f"❌ فشل: {result.get('error', 'خطأ')}", "error")
    return redirect(url_for("admin_panel"))

@app.route("/admin/clear_master", methods=["POST"])
@admin_required
def admin_clear_master():
    set_secret_setting("master_ssid", "")
    set_secret_setting("master_cookies", "")
    set_setting("master_email", "")
    copy_manager.master_client = None
    flash("تم فصل حساب المشرف", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/save_master_settings", methods=["POST"])
@admin_required
def admin_save_master_settings():
    try: pct = float(request.form.get("master_pct", 10))
    except ValueError: pct = 10
    acct = request.form.get("master_account_type", "demo")
    if acct not in ("demo", "live"): acct = "demo"
    set_setting("master_percentage", str(pct))
    set_setting("master_account_type", acct)
    flash("✅ تم الحفظ", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/open_trade", methods=["POST"])
@admin_required
def admin_open_trade():
    asset = request.form.get("asset", "EURUSD_otc")
    direction = request.form.get("direction", "call")
    if direction not in ("call", "put"): direction = "call"
    try: expiry = int(request.form.get("expiry", 60))
    except ValueError: expiry = 60
    time_mode = request.form.get("time_mode", "TIMER")
    try: master_pct = float(get_setting("master_percentage", "10"))
    except ValueError: master_pct = 10.0
    try:
        result = copy_manager.run_async(copy_manager.broadcast_trade(asset, direction, master_pct, expiry, time_mode), timeout=300)
    except Exception as e:
        result = {"success": False, "error": str(e)}
    if result.get("success"):
        flash(f"✅ نُسخت إلى {result.get('copied_count', 0)} من {result.get('total_followers', 0)}", "success")
    else:
        flash(f"❌ {result.get('error', 'خطأ')}", "error")
    return redirect(url_for("admin_panel"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin"))

if __name__ == "__main__":
    copy_manager.start_background_loop()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
