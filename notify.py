# -*- coding: utf-8 -*-
"""إرسال رسائل تيليجرام (يستخدمه البوت ولوحة المشرف)."""
import os
import requests


def _token():
    return "".join((os.environ.get("TELEGRAM_BOT_TOKEN") or "").split())


def _chat_id():
    return (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def telegram_configured():
    return bool(_token() and _chat_id())


def send_telegram_text(text):
    """يرسل رسالة نصية. يرجع (نجح؟, شرح)."""
    if not telegram_configured():
        return False, "TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوط في Render"
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": _chat_id(), "text": text},
            timeout=15,
        )
    except Exception as e:
        return False, f"تعذر الوصول إلى تيليجرام: {e}"
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code == 200 and data.get("ok"):
        return True, "تم الإرسال"
    desc = data.get("description") or f"HTTP {r.status_code}"
    if r.status_code == 401:
        desc = "توكن البوت غير صحيح أو ملغى (أنشئ توكناً جديداً من BotFather)"
    elif "chat not found" in desc.lower():
        desc = "TELEGRAM_CHAT_ID غير صحيح، أو البوت ليس مشرفاً في القناة"
    return False, desc
