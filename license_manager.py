# -*- coding: utf-8 -*-
import secrets
import string
from datetime import datetime, timedelta
from database import get_db, now_iso

KEY_DURATION_OPTIONS = [
    (1, "ساعة واحدة"),
    (24, "يوم واحد"),
    (168, "أسبوع"),
    (720, "شهر (30 يوم)"),
    (0, "للأبد"),
]


def generate_key(hours=24):
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    key = "OSCAR-" + "-".join(parts)

    expires_at = None
    if hours and hours > 0:
        expires_at = (datetime.utcnow() + timedelta(hours=hours)).isoformat()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO licenses (key, expires_at) VALUES (?, ?)",
            (key, expires_at),
        )
        conn.commit()
        return {"success": True, "key": key, "expires_at": expires_at}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def _row_status(row):
    if not row:
        return False, "المفتاح غير موجود"
    if not row["is_active"]:
        return False, "المفتاح موقوف"
    if row["expires_at"] and now_iso() > row["expires_at"]:
        return False, "انتهت صلاحية المفتاح"
    return True, "OK"


def validate_key(key, device_id=None):
    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE key = ?", (key,)).fetchone()
    ok, msg = _row_status(row)
    if not ok:
        conn.close()
        return {"success": False, "error": msg}

    if device_id:
        if not row["device_ip"]:
            conn.execute("UPDATE licenses SET device_ip = ? WHERE key = ?", (device_id, key))
            conn.commit()
        elif row["device_ip"] != device_id:
            conn.close()
            return {
                "success": False,
                "error": "هذا المفتاح مرتبط بجهاز آخر. تواصل مع المشرف لإعادة تعيين الجهاز.",
            }

    conn.close()
    return {"success": True, "key": key, "expires_at": row["expires_at"]}


def is_key_valid(key):
    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE key = ?", (key,)).fetchone()
    conn.close()
    return _row_status(row)


def deactivate_key(key):
    conn = get_db()
    conn.execute("UPDATE licenses SET is_active = 0 WHERE key = ?", (key,))
    conn.execute("UPDATE users SET is_copying = 0 WHERE license_key = ?", (key,))
    conn.commit()
    conn.close()


def reactivate_key(key):
    conn = get_db()
    conn.execute("UPDATE licenses SET is_active = 1 WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def reset_device(key):
    conn = get_db()
    conn.execute("UPDATE licenses SET device_ip = NULL WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def delete_key(key):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE license_key = ?", (key,))
    conn.execute("DELETE FROM licenses WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def get_all_licenses():
    conn = get_db()
    rows = conn.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
