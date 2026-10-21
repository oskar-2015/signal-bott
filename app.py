# -*- coding: utf-8 -*-
import os
import asyncio
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template_string, flash
from pyquotex.stable_api import Quotex

import db

db.init_db()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-please")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-too")


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def user_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------- admin ----------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))
        flash("كلمة سر خاطئة")
    return render_template_string("""
        <form method=post>
        <input type=password name=password placeholder="كلمة سر الأدمن">
        <button type=submit>دخول</button>
        </form>
    """)


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin_panel():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            db.create_key(request.form.get("duration_type"), request.form.get("note"))
        elif action == "pause":
            db.set_key_status(request.form.get("key_id"), "paused")
        elif action == "activate":
            db.set_key_status(request.form.get("key_id"), "active")
        elif action == "revoke":
            db.set_key_status(request.form.get("key_id"), "revoked")
        return redirect(url_for("admin_panel"))

    keys = db.list_keys()
    return render_template_string("""
        <h2>إدارة المفاتيح</h2>
        <form method=post>
            <input type=hidden name=action value=create>
            <select name=duration_type>
                <option value=hour>ساعة</option>
                <option value=day>يوم</option>
                <option value=week>أسبوع</option>
                <option value=month>شهر</option>
                <option value=lifetime>دائم</option>
            </select>
            <input name=note placeholder="ملاحظة (اختياري)">
            <button type=submit>إنشاء مفتاح</button>
        </form>
        <table border=1>
        <tr><th>الكود</th><th>المدة</th><th>الحالة</th><th>ينتهي</th><th>ملاحظة</th><th>إجراء</th></tr>
        {% for k in keys %}
        <tr>
            <td>{{ k.key_code }}</td>
            <td>{{ k.duration_type }}</td>
            <td>{{ k.status }}</td>
            <td>{{ k.expires_at or 'دائم' }}</td>
            <td>{{ k.note or '' }}</td>
            <td>
                <form method=post style="display:inline">
                    <input type=hidden name=key_id value="{{ k.id }}">
                    {% if k.status == 'active' %}
                    <button name=action value=pause>إيقاف مؤقت</button>
                    {% else %}
                    <button name=action value=activate>تفعيل</button>
                    {% endif %}
                    <button name=action value=revoke onclick="return confirm('حذف نهائي؟')">حذف</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </table>
    """, keys=keys)


# ---------------- user login ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        key_code = request.form.get("key_code", "").strip()
        key_row = db.get_key_by_code(key_code)
        if not db.is_key_valid(key_row):
            flash("المفتاح غير صالح أو منتهي")
            return redirect(url_for("login"))
        user = db.get_or_create_user_for_key(key_row["id"])
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))
    return render_template_string("""
        <form method=post>
        <input name=key_code placeholder="أدخل كود المفتاح">
        <button type=submit>دخول</button>
        </form>
    """)


@app.route("/dashboard", methods=["GET", "POST"])
@user_required
def dashboard():
    user_id = session["user_id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "connect_quotex":
            email = request.form.get("qx_email")
            password = request.form.get("qx_password")
            ok, err = asyncio.run(_connect_and_store(user_id, email, password))
            if not ok:
                flash(f"فشل الاتصال بـ Quotex: {err}")
        elif action == "save_settings":
            db.update_user_settings(
                user_id,
                account_mode=request.form.get("account_mode"),
                auto_trade_enabled=(request.form.get("auto_trade_enabled") == "on"),
                stop_loss_usd=float(request.form.get("stop_loss_usd") or 0),
                take_profit_usd=float(request.form.get("take_profit_usd") or 0),
            )
        return redirect(url_for("dashboard"))

    conn = db.get_conn()
    user = dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    conn.close()
    return render_template_string("""
        <h2>لوحة التحكم</h2>
        <h3>ربط حساب Quotex</h3>
        <form method=post>
            <input type=hidden name=action value=connect_quotex>
            <input name=qx_email placeholder="بريد Quotex">
            <input type=password name=qx_password placeholder="كلمة السر">
            <button type=submit>ربط الحساب</button>
        </form>
        <p>الحالة: {{ 'مربوط' if user.qx_cookies else 'غير مربوط' }}</p>

        <h3>الإعدادات</h3>
        <form method=post>
            <input type=hidden name=action value=save_settings>
            <select name=account_mode>
                <option value=demo {{ 'selected' if user.account_mode=='demo' }}>تجريبي</option>
                <option value=real {{ 'selected' if user.account_mode=='real' }}>حقيقي</option>
            </select><br>
            وقف الخسارة ($): <input name=stop_loss_usd value="{{ user.stop_loss_usd }}"><br>
            وقف الربح ($): <input name=take_profit_usd value="{{ user.take_profit_usd }}"><br>
            تفعيل التداول الآلي: <input type=checkbox name=auto_trade_enabled {{ 'checked' if user.auto_trade_enabled }}><br>
            <button type=submit>حفظ</button>
        </form>
        <p>ربح/خسارة الجلسة الحالية: {{ user.session_pl_usd }} $</p>
    """, user=user)


async def _connect_and_store(user_id, email, password):
    client = Quotex(email=email, password=password, lang="en")
    try:
        check, reason = await client.connect()
        if not check:
            return False, reason
        cookies_str = client.session_data.get("cookies") if hasattr(client, "session_data") else None
        ssid = client.session_data.get("token") if hasattr(client, "session_data") else None
        db.update_user_session(user_id, cookies_str or "", ssid or "")
        return True, None
    except Exception as e:
        return False, str(e)


@app.route("/")
def home():
    return "Oscar Trader AI is live"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
