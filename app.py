# -*- coding: utf-8 -*-
import os
import re
import time
import hashlib
import secrets
import json as json_lib
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, make_response
)

from database import (
    init_db, get_db, get_setting, set_setting, get_secret_setting,
    set_secret_setting, save_user_session, clear_user_session, db_info,
)
from license_manager import (
    generate_key, validate_key, get_all_licenses,
    deactivate_key, reactivate_key, delete_key, reset_device,
    is_key_valid, KEY_DURATION_OPTIONS,
)
from quotex_manager import copy_manager
from assets import OTC_ASSETS, REAL_ASSETS, EXPIRY_OPTIONS
from notify import send_telegram_text, telegram_configured

app = Flask(__name__)


def _make_secret_key():
    explicit = (os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if explicit:
        return explicit
    # إذا لم يضبط المشرف مفتاحاً نشتقه من مفتاح التشفير، فلا تنتهي الجلسات عند كل إعادة تشغيل
    base = os.environ.get("ENCRYPTION_KEY") or secrets.token_hex(32)
    return hashlib.sha256(("flask-session:" + base).encode()).hexdigest()


app.secret_key = _make_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# اختياري: إذا ضبطته تصبح واجهات /api/admin/* تحتاج هذا الرمز في الهيدر X-Admin-Token
ADMIN_API_TOKEN = (os.environ.get("ADMIN_API_TOKEN") or "").strip()
DEVICE_COOKIE = "oscar_device"
APK_DOWNLOAD_URL = os.environ.get("APK_DOWNLOAD_URL", "#")

try:
    init_db()
except Exception as _e:
    print(f"[APP] init_db failed: {_e}", flush=True)


# ==================== أدوات عامة ====================

_attempts = {}


def too_many_attempts(bucket, limit=8, window=600):
    """حماية بسيطة من تخمين المفاتيح وكلمة المرور."""
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "?").split(",")[0].strip()
    now = time.time()
    k = f"{bucket}:{ip}"
    hits = [t for t in _attempts.get(k, []) if now - t < window]
    if len(hits) >= limit:
        _attempts[k] = hits
        return True
    hits.append(now)
    _attempts[k] = hits
    return False


@app.after_request
def add_headers(response):
    if request.path.startswith("/admin") or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    if code == 404:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not_found"}), 404
        return redirect(url_for("index"))
    print(f"[ERROR] {request.path}: {type(e).__name__}: {e}", flush=True)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "حدث خطأ في الخادم، حاول بعد قليل"}), 500
    return (
        "<meta charset='utf-8'><body style='font-family:sans-serif;background:#0b1220;color:#e8eefc;"
        "text-align:center;padding:60px'><h2>حدث خطأ مؤقت</h2>"
        "<p>أعد تحميل الصفحة بعد لحظات.</p><p><a style='color:#e3b341' href='/'>العودة للرئيسية</a></p>",
        500,
    )


def clear_user_cache(user_id):
    try:
        copy_manager.clear_cache(user_id)
    except Exception:
        pass


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        wants_json = request.path.startswith("/api/")
        key = session.get("license_key")
        if not key:
            if wants_json:
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("index"))
        ok, msg = is_key_valid(key)
        if not ok:
            session.clear()
            if wants_json:
                return jsonify({"ok": False, "error": msg}), 401
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


def admin_api(f):
    """واجهات تطبيق المدير. إذا لم يُضبط ADMIN_API_TOKEN تبقى مفتوحة كما كانت (للتوافق)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if ADMIN_API_TOKEN:
            given = request.headers.get("X-Admin-Token", "")
            if not (session.get("is_admin") or secrets.compare_digest(given, ADMIN_API_TOKEN)):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
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


# ==================== منطق مشترك (للتطبيق وللموقع) ====================

def build_user_info(key):
    user = ensure_user(key)
    return {
        "ok": True,
        "key": key,
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
        "notification": user.get("last_notification") or "",
        "admin_auto_enabled": get_setting("admin_auto_enabled", "1") == "1",
    }


def do_save_and_enable(key, data):
    """يرجع (dict, status_code)."""
    try:
        account_type = data.get("account_type", "demo")
        trade_mode = data.get("trade_mode", "percent")
        trade_value = float(data.get("trade_value", 1.0))
        max_loss_value = float(data.get("max_loss_value", 0))
        max_profit_value = float(data.get("max_profit_value", 0))
        enable_type = data.get("enable_type", "copy")
    except (ValueError, TypeError):
        return {"ok": False, "error": "قيم غير صحيحة"}, 400

    if account_type not in ("demo", "live"):
        account_type = "demo"
    if trade_mode not in ("percent", "fixed"):
        trade_mode = "percent"
    if enable_type not in ("copy", "auto"):
        enable_type = "copy"
    if trade_mode == "percent":
        if trade_value < 0.1 or trade_value > 100:
            return {"ok": False, "error": "النسبة يجب أن تكون بين 0.1 و 100"}, 400
    elif trade_value < 1:
        return {"ok": False, "error": "المبلغ الثابت 1$ على الأقل"}, 400
    if max_loss_value < 0 or max_profit_value < 0:
        return {"ok": False, "error": "القيم يجب أن تكون موجبة"}, 400

    if enable_type == "auto" and get_setting("admin_auto_enabled", "1") != "1":
        return {"ok": False, "error": "التداول الآلي معطّل حالياً من قبل المدير"}, 400

    user = get_user(key)
    if not user:
        return {"ok": False, "error": "المستخدم غير موجود"}, 400
    if not (user.get("quotex_ssid") or user.get("quotex_cookies")):
        return {"ok": False, "error": "اربط حساب Quotex أولاً"}, 400

    is_copying = 1 if enable_type == "copy" else 0
    is_auto_trading = 1 if enable_type == "auto" else 0

    conn = get_db()
    conn.execute("UPDATE users SET account_type = ? WHERE license_key = ?", (account_type, key))
    conn.commit()
    conn.close()

    fresh_user = get_user(key)
    clear_user_cache(user["id"])
    try:
        stats = copy_manager.run_async(copy_manager.get_user_stats(fresh_user), timeout=90)
    except Exception as e:
        return {"ok": False, "error": f"تعذر الاتصال بالحساب: {e}"}, 400

    if not stats.get("ok"):
        err = stats.get("error", "تعذر الاتصال")
        return {"ok": False, "error": f"تعذر الاتصال بالحساب: {err}"}, 400
    if stats["balance"] <= 0:
        label = "الحقيقي" if account_type == "live" else "التجريبي"
        return {"ok": False, "error": f"رصيد حسابك {label} صفر. أضف أموالاً أو غيّر نوع الحساب"}, 400

    new_start = stats["balance"]

    conn = get_db()
    conn.execute(
        "UPDATE users SET trade_mode = ?, trade_value = ?, "
        "max_loss_value = ?, max_profit_value = ?, settings_locked = 1, "
        "is_copying = ?, is_auto_trading = ?, start_balance = ?, last_notification = NULL, "
        "balance_version = balance_version + 1 WHERE license_key = ?",
        (trade_mode, trade_value, max_loss_value, max_profit_value,
         is_copying, is_auto_trading, new_start, key),
    )
    conn.commit()
    conn.close()
    clear_user_cache(user["id"])
    return {
        "ok": True,
        "is_copying": bool(is_copying),
        "is_auto_trading": bool(is_auto_trading),
        "start_balance": new_start,
        "balance": new_start,
        "account_type": account_type,
    }, 200


def do_stop(key):
    user = get_user(key)
    if not user:
        return {"ok": False, "error": "المستخدم غير موجود"}, 400
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_copying = 0, is_auto_trading = 0, settings_locked = 0, "
        "start_balance = NULL, last_notification = NULL, balance_version = balance_version + 1 "
        "WHERE license_key = ?",
        (key,),
    )
    conn.commit()
    conn.close()
    clear_user_cache(user["id"])
    return {"ok": True}, 200


def do_clear_notification(key):
    user = get_user(key)
    if user:
        conn = get_db()
        conn.execute("UPDATE users SET last_notification = NULL WHERE license_key = ?", (key,))
        conn.commit()
        conn.close()
        clear_user_cache(user["id"])
    return {"ok": True}, 200


def do_stats(key, requested_account=""):
    user = get_user(key)
    if not user or (not user["quotex_ssid"] and not user["quotex_cookies"]):
        return {"ok": False, "error": "غير مرتبط"}, 400
    requested_account = (requested_account or "").strip().lower()
    if requested_account in ("demo", "live"):
        user = dict(user)
        user["account_type"] = requested_account
    return copy_manager.fetch_stats_sync(user), 200


def do_unlink(key):
    user = get_user(key)
    clear_user_session(key)
    if user:
        clear_user_cache(user["id"])
    return {"ok": True}, 200


def _respond(result):
    payload, status = result
    return jsonify(payload), status


# ==================== صفحات المستخدم ====================

@app.route("/health")
def health():
    return "ok"


@app.route("/")
def index():
    if session.get("license_key"):
        return redirect(url_for("dashboard"))
    return render_template("index.html", apk_url=APK_DOWNLOAD_URL)


@app.route("/activate", methods=["POST"])
def activate():
    if too_many_attempts("activate"):
        flash("محاولات كثيرة. انتظر بضع دقائق ثم حاول مرة أخرى.", "error")
        return redirect(url_for("index"))
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
    resp.set_cookie(
        DEVICE_COOKIE, device_id, max_age=60 * 60 * 24 * 365 * 2, httponly=True,
        samesite="Lax", secure=request.headers.get("X-Forwarded-Proto") == "https",
    )
    return resp


@app.route("/dashboard")
@login_required
def dashboard():
    key = session["license_key"]
    ensure_user(key)
    return render_template("dashboard.html", license_key=key, apk_url=APK_DOWNLOAD_URL)


@app.route("/app_login/<key>")
def app_login(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok:
        return f"<h2>⚠️ خطأ</h2><p>{msg}</p>", 403
    session["license_key"] = key
    session.permanent = True
    ensure_user(key)
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- واجهات لوحة المستخدم في الموقع (تعتمد على الجلسة) ----------

@app.route("/api/me")
@login_required
def api_me():
    return jsonify(build_user_info(session["license_key"]))


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    return _respond(do_save_and_enable(session["license_key"], request.get_json(silent=True) or {}))


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    return _respond(do_stop(session["license_key"]))


@app.route("/api/clear_notification", methods=["POST"])
@login_required
def api_clear_notification():
    return _respond(do_clear_notification(session["license_key"]))


@app.route("/api/stats")
@login_required
def api_stats():
    return _respond(do_stats(session["license_key"], request.args.get("account", "")))


@app.route("/api/unlink", methods=["POST"])
@login_required
def api_unlink():
    return _respond(do_unlink(session["license_key"]))


# ==================== API لتطبيق المستخدم (Oscar Linker) ====================

def _app_key_or_error(key):
    key = key.strip().upper()
    ok, msg = is_key_valid(key)
    if not ok:
        return None, (jsonify({"ok": False, "error": msg}), 403)
    return key, None


@app.route("/api/app/user_info/<key>")
def api_app_user_info(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    info = build_user_info(key)
    info.pop("key", None)
    return jsonify(info)


@app.route("/api/app/save_and_enable/<key>", methods=["POST"])
def api_app_save_and_enable(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    return _respond(do_save_and_enable(key, request.get_json(silent=True) or {}))


@app.route("/api/app/stop_trading/<key>", methods=["POST"])
def api_app_stop_trading(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    return _respond(do_stop(key))


@app.route("/api/app/clear_notification/<key>", methods=["POST"])
def api_app_clear_notification(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    return _respond(do_clear_notification(key))


@app.route("/api/app/session_stats/<key>")
def api_app_session_stats(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    return _respond(do_stats(key, request.args.get("account", "")))


@app.route("/api/app/unlink/<key>", methods=["POST"])
def api_app_unlink(key):
    key, err = _app_key_or_error(key)
    if err:
        return err
    return _respond(do_unlink(key))


@app.route("/api/link", methods=["POST"])
def api_link():
    data = request.get_json(silent=True) or {}
    key = str(data.get("license_key", "")).strip().upper()
    ssid = str(data.get("ssid", "")).strip()
    cookies = str(data.get("cookies", "")).strip()
    email = str(data.get("email", "")).strip()
    ok, msg = is_key_valid(key)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 403
    if not ssid and not cookies:
        return jsonify({"ok": False, "error": "لا توجد جلسة"}), 400
    ensure_user(key)
    try:
        check = copy_manager.run_async(copy_manager.test_session(ssid or "", cookies or None), timeout=90)
    except Exception as e:
        check = {"success": False, "error": str(e)}
    if not check.get("success"):
        return jsonify({"ok": False, "error": check.get("error", "الجلسة لا تعمل")}), 400
    final_ssid = check.get("ssid", "") or ssid
    user = get_user(key)
    save_user_session(key, final_ssid, cookies, email)
    if user:
        clear_user_cache(user["id"])
    return jsonify({"ok": True, "ssid_extracted": bool(final_ssid and not ssid)})


# ==================== API لتطبيق المدير (Oscar Admin) ====================

@app.route("/api/admin/is_linked")
@admin_api
def api_admin_is_linked():
    ssid = get_secret_setting("master_ssid")
    is_valid_ssid = bool(ssid and not (ssid.startswith('<') or 'object at 0x' in ssid))
    return jsonify({
        "linked": is_valid_ssid,
        "account_type": get_setting("master_account_type", "demo"),
    })


@app.route("/api/admin/link_master", methods=["POST"])
@admin_api
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
@admin_api
def api_admin_raw():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("raw", ""))[:600]
    print(f"[RAW] {raw[:120]}", flush=True)
    return jsonify({"ok": True})


@app.route("/api/admin/realtime_trade", methods=["POST"])
@admin_api
def api_admin_realtime_trade():
    data = request.get_json(silent=True) or {}

    raw = str(data.get("raw", "")).strip()
    if raw:
        m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw)
        if not m:
            return jsonify({"ok": False, "error": "no json"}), 400
        try:
            p = json_lib.loads(m.group(0))
        except Exception as e:
            print(f"[REALTIME] JSON parse error: {e}", flush=True)
            return jsonify({"ok": False, "error": "parse fail"}), 400
    else:
        p = data

    asset = p.get("asset") or p.get("symbol") or p.get("instrument") or p.get("pair")
    amount = p.get("amount") or p.get("sum") or p.get("price") or p.get("investment")
    action = p.get("action") if "action" in p else p.get("direction", p.get("side"))
    expiry = p.get("time") or p.get("duration") or p.get("expiry") or 60

    print(f"[REALTIME] Parsed: asset={asset} action={action} amount={amount} expiry={expiry}", flush=True)

    if not asset or not amount:
        return jsonify({"ok": False, "error": "missing fields"}), 400

    dir_str = None
    if action in (1, "call", "up", True):
        dir_str = "call"
    elif action in (-1, "put", "down", False):
        dir_str = "put"

    if not dir_str:
        return jsonify({"ok": False, "error": "bad direction"}), 400

    try:
        amount_f = float(amount)
        expiry_i = int(expiry)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad numbers"}), 400

    try:
        result = copy_manager.run_async(
            copy_manager.broadcast_trade(asset, dir_str, amount_f, expiry_i, "TIMER", skip_master=True),
            timeout=300
        )
    except Exception as e:
        print(f"[REALTIME] Exception: {e}", flush=True)
        result = {"success": False, "error": str(e)}

    copied = result.get("copied_count", 0)
    total = result.get("total_followers", 0)
    print(f"[REALTIME] Copied {copied}/{total} for {asset} {dir_str} ${amount_f} ({expiry_i}s)", flush=True)

    if result.get("success"):
        return jsonify({"ok": True, "copied": copied, "total": total})
    return jsonify({"ok": False, "error": result.get("error", "فشل")}), 400


@app.route("/api/admin/toggles")
@admin_api
def api_admin_toggles():
    return jsonify({
        "ok": True,
        "auto_enabled": get_setting("admin_auto_enabled", "1") == "1",
        "bot_enabled": get_setting("admin_bot_enabled", "1") == "1",
    })


# ==================== لوحة المشرف ====================

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if too_many_attempts("admin", limit=6):
            flash("محاولات كثيرة. انتظر بضع دقائق.", "error")
            return render_template("admin_login.html")
        given = request.form.get("password", "")
        if ADMIN_PASSWORD and secrets.compare_digest(given, ADMIN_PASSWORD):
            session["is_admin"] = True
            session.permanent = True
            return redirect(url_for("admin_panel"))
        flash("كلمة المرور غير صحيحة", "error")
    if session.get("is_admin"):
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html", admin_configured=bool(ADMIN_PASSWORD))


def _safe_setting(key, default=""):
    try:
        return get_setting(key, default) or default
    except Exception:
        return default


def system_status():
    di = db_info()
    try:
        master_linked = bool(get_secret_setting("master_ssid"))
    except Exception:
        master_linked = False
    return {
        "db_mode": di["mode"],
        "db_configured": di["configured"],
        "db_error": di["error"],
        "master_linked": master_linked,
        "master_account": _safe_setting("master_account_type", "demo"),
        "telegram_ok": telegram_configured(),
        "signals_env": os.environ.get("ENABLE_SIGNALS", "false").lower() == "true",
        "bot_status": _safe_setting("bot_status", ""),
        "bot_status_at": _safe_setting("bot_status_at", ""),
        "last_signal": _safe_setting("last_signal", ""),
        "last_signal_at": _safe_setting("last_signal_at", ""),
        "quotex_block_min": copy_manager.rate_limit_remaining() // 60 + (1 if copy_manager.rate_limit_remaining() else 0),
        "api_open": not ADMIN_API_TOKEN,
        "weak_password": len(ADMIN_PASSWORD) < 10,
    }


@app.route("/admin/panel")
@admin_required
def admin_panel():
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    linked_users = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE quotex_ssid IS NOT NULL AND quotex_ssid != ''"
    ).fetchone()["c"]
    active_copy = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_copying = 1").fetchone()["c"]
    active_auto = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_auto_trading = 1").fetchone()["c"]
    users_list = conn.execute(
        "SELECT license_key, quotex_email, quotex_ssid, account_type, is_copying, is_auto_trading, "
        "trade_mode, trade_value, start_balance FROM users ORDER BY created_at DESC"
    ).fetchall()
    logs = conn.execute("SELECT * FROM trade_logs ORDER BY created_at DESC LIMIT 30").fetchall()
    conn.close()
    return render_template(
        "admin.html",
        licenses=get_all_licenses(),
        total_users=total_users,
        linked_users=linked_users,
        active_copy=active_copy,
        active_auto=active_auto,
        users_list=[dict(u) for u in users_list],
        logs=[dict(l) for l in logs],
        otc_assets=OTC_ASSETS,
        real_assets=REAL_ASSETS,
        expiry_options=EXPIRY_OPTIONS,
        key_duration_options=KEY_DURATION_OPTIONS,
        admin_auto_enabled=get_setting("admin_auto_enabled", "1") == "1",
        admin_bot_enabled=get_setting("admin_bot_enabled", "1") == "1",
        system=system_status(),
        tab=request.args.get("tab", "status"),
    )


def _back(tab="status"):
    return redirect(url_for("admin_panel", tab=tab))


@app.route("/admin/create_key", methods=["POST"])
@admin_required
def admin_create_key():
    try:
        hours = int(request.form.get("hours", 24))
    except ValueError:
        hours = 24
    result = generate_key(hours)
    if result["success"]:
        flash(f"✅ تم إنشاء المفتاح: {result['key']}", "success")
    else:
        flash(f"❌ فشل: {result['error']}", "error")
    return _back("keys")


@app.route("/admin/delete_key", methods=["POST"])
@admin_required
def admin_delete_key():
    key = request.form.get("key", "").strip()
    if not key:
        flash("المفتاح مطلوب", "error")
        return _back("keys")
    user = get_user(key)
    delete_key(key)
    if user:
        clear_user_cache(user["id"])
    flash(f"✅ تم حذف المفتاح {key} وكل بيانات المستخدم نهائياً", "success")
    return _back("keys")


@app.route("/admin/toggle_key", methods=["POST"])
@admin_required
def admin_toggle_key():
    key = request.form.get("key", "")
    action = request.form.get("action", "")
    if action == "activate":
        reactivate_key(key)
        flash(f"✅ تم تفعيل المفتاح {key}", "success")
    else:
        deactivate_key(key)
        user = get_user(key)
        if user:
            conn = get_db()
            conn.execute(
                "UPDATE users SET is_copying = 0, is_auto_trading = 0, "
                "last_notification = ?, balance_version = balance_version + 1 "
                "WHERE license_key = ?",
                ("⚠️ تم تعطيل مفتاحك مؤقتاً من قبل المدير", key),
            )
            conn.commit()
            conn.close()
        flash(f"⛔ تم إيقاف المفتاح {key} مؤقتاً", "success")
    return _back("keys")


@app.route("/admin/reset_device", methods=["POST"])
@admin_required
def admin_reset_device():
    reset_device(request.form.get("key", ""))
    flash("✅ تم إعادة تعيين الجهاز", "success")
    return _back("keys")


@app.route("/admin/clear_master", methods=["POST"])
@admin_required
def admin_clear_master():
    set_secret_setting("master_ssid", "")
    set_secret_setting("master_cookies", "")
    set_setting("master_email", "")
    copy_manager.master_client = None
    flash("✅ تم فصل حساب المشرف", "success")
    return _back("status")


@app.route("/admin/toggle_auto_trading", methods=["POST"])
@admin_required
def admin_toggle_auto_trading():
    current = get_setting("admin_auto_enabled", "1")
    new_val = "0" if current == "1" else "1"
    set_setting("admin_auto_enabled", new_val)

    if new_val == "0":
        conn = get_db()
        conn.execute(
            "UPDATE users SET is_auto_trading = 0, last_notification = ?, "
            "balance_version = balance_version + 1 WHERE is_auto_trading = 1",
            ("⚠️ تم تعطيل التداول الآلي من قبل المدير",),
        )
        conn.commit()
        conn.close()
        flash("⛔ تم تعطيل التداول الآلي — المستخدمون أُبلغوا", "success")
    else:
        flash("✅ تم تفعيل التداول الآلي — يمكن للمستخدمين التفعيل من جديد", "success")
    return _back("status")


@app.route("/admin/toggle_telegram_bot", methods=["POST"])
@admin_required
def admin_toggle_telegram_bot():
    current = get_setting("admin_bot_enabled", "1")
    new_val = "0" if current == "1" else "1"
    set_setting("admin_bot_enabled", new_val)
    if new_val == "0":
        flash("⛔ تم تعطيل بوت تيليجرام — لن تُرسل إشارات للقناة", "success")
    else:
        flash("✅ تم تفعيل بوت تيليجرام", "success")
    return _back("status")


@app.route("/admin/test_telegram", methods=["POST"])
@admin_required
def admin_test_telegram():
    ok, info = send_telegram_text("✅ رسالة اختبار من لوحة Oscar Trader AI — الربط يعمل بشكل صحيح.")
    if ok:
        flash("✅ وصلت رسالة الاختبار إلى تيليجرام", "success")
    else:
        flash(f"❌ فشل الإرسال: {info}", "error")
    return _back("status")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    copy_manager.start_background_loop()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
