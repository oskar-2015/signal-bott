# -*- coding: utf-8 -*-
"""
قاعدة البيانات.

- إذا وُجد TURSO_DATABASE_URL و TURSO_AUTH_TOKEN نستخدم Turso عبر HTTP
  (لا تحتاج أي مكتبة خاصة، فقط requests).
- إذا فشل الاتصال بـ Turso لا يتوقف البرنامج: يعمل مؤقتاً على قاعدة محلية
  ويحاول الرجوع إلى Turso كل دقيقة، ولوحة المشرف تعرض تحذيراً واضحاً.
"""
import os
import base64
import sqlite3
import threading
import time
from datetime import datetime

import requests
from cryptography.fernet import Fernet, InvalidToken

_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_BASE, "oskar.db")


def _clean(value):
    """يحذف أي مسافات أو أسطر جديدة دخلت بالخطأ عند لصق القيمة في Render."""
    return "".join((value or "").split())


_TURSO_URL = _clean(os.environ.get("TURSO_DATABASE_URL", ""))
_TURSO_TOKEN = _clean(os.environ.get("TURSO_AUTH_TOKEN", ""))

_enc_key = (os.environ.get("ENCRYPTION_KEY") or "").strip()
if not _enc_key:
    raise RuntimeError("المتغير ENCRYPTION_KEY غير موجود في إعدادات Render")
_fernet = Fernet(_enc_key.encode())

_USE_TURSO = False
_DB_ERROR = ""
_turso_client = None
_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# عميل Turso عبر HTTP
# ═══════════════════════════════════════════════════════════════
class _Row(dict):
    """صف نتيجة: يعمل بالاسم row["name"] وبالرقم row[0] ويتحول إلى dict."""

    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = list(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, result):
        cols = [c.get("name") for c in result.get("cols", [])]
        self._rows = [
            _Row(cols, [_decode_value(v) for v in row])
            for row in result.get("rows", [])
        ]
        self._pos = 0
        self.rowcount = result.get("affected_row_count", -1)
        last = result.get("last_insert_rowid")
        self.lastrowid = int(last) if last not in (None, "") else None

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def _encode_value(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _decode_value(v):
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v.get("base64", ""))
    return v.get("value")


class _TursoHTTP:
    def __init__(self, url, token):
        u = url.strip()
        for prefix in ("libsql://", "wss://", "ws://"):
            if u.startswith(prefix):
                u = "https://" + u[len(prefix):]
                break
        if not u.startswith("http"):
            u = "https://" + u
        self.base = u.rstrip("/")
        self.token = token
        self._local = threading.local()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def run(self, statements):
        """statements = [(sql, params), ...] -> list of result dicts"""
        reqs = [
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": [_encode_value(p) for p in (params or ())]},
            }
            for sql, params in statements
        ]
        reqs.append({"type": "close"})
        resp = self._session().post(
            self.base + "/v2/pipeline",
            json={"requests": reqs},
            headers={"Authorization": "Bearer " + self.token},
            timeout=25,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Turso رفض التوكن (TURSO_AUTH_TOKEN غير صحيح أو لقاعدة أخرى أو منتهي)"
            )
        if resp.status_code == 404:
            raise RuntimeError("رابط قاعدة Turso غير صحيح (TURSO_DATABASE_URL)")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        out = []
        for i in range(len(statements)):
            item = results[i]
            if item.get("type") == "error":
                raise RuntimeError(item.get("error", {}).get("message", "خطأ في قاعدة البيانات"))
            out.append(item["response"]["result"])
        return out


class _TursoConnection:
    """يعمل مثل sqlite3.Connection في الأجزاء التي يستخدمها المشروع."""

    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        return _Cursor(self._client.run([(sql, params)])[0])

    def executescript_list(self, statements):
        self._client.run([(s, ()) for s in statements])

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ═══════════════════════════════════════════════════════════════
# التشفير والوقت
# ═══════════════════════════════════════════════════════════════
def encrypt(text):
    if not text:
        return None
    if not isinstance(text, str):
        text = str(text)
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token):
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def now_iso():
    return datetime.utcnow().isoformat()


# ═══════════════════════════════════════════════════════════════
# الاتصال
# ═══════════════════════════════════════════════════════════════
def get_db():
    if _USE_TURSO:
        return _TursoConnection(_turso_client)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def db_info():
    """حالة قاعدة البيانات لعرضها في لوحة المشرف."""
    return {
        "mode": "turso" if _USE_TURSO else "local",
        "configured": bool(_TURSO_URL and _TURSO_TOKEN),
        "error": _DB_ERROR,
    }


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE NOT NULL,
        expires_at TEXT,
        is_active INTEGER DEFAULT 1,
        device_ip TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT UNIQUE NOT NULL,
        quotex_ssid TEXT,
        quotex_email TEXT,
        quotex_cookies TEXT,
        is_copying INTEGER DEFAULT 0,
        is_auto_trading INTEGER DEFAULT 0,
        trade_mode TEXT DEFAULT 'percent',
        trade_value REAL DEFAULT 1.0,
        max_loss_value REAL DEFAULT 0,
        max_profit_value REAL DEFAULT 0,
        start_balance REAL,
        account_type TEXT DEFAULT 'demo',
        device_ip TEXT,
        last_notification TEXT,
        balance_version INTEGER DEFAULT 0,
        settings_locked INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (license_key) REFERENCES licenses(key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT,
        direction TEXT,
        amount REAL,
        expiry INTEGER,
        master_order_id TEXT,
        follower_count INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]


def init_db():
    conn = get_db()
    try:
        if _USE_TURSO:
            conn.executescript_list(_SCHEMA)
        else:
            folder = os.path.dirname(DB_PATH)
            if folder:
                os.makedirs(folder, exist_ok=True)
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()
    finally:
        conn.close()
    print(f"[DB] init_db completed (turso={_USE_TURSO})", flush=True)


def _try_turso():
    """يجرب الاتصال بـ Turso. يرجع True عند النجاح."""
    global _USE_TURSO, _DB_ERROR, _turso_client
    try:
        client = _TursoHTTP(_TURSO_URL, _TURSO_TOKEN)
        client.run([("SELECT 1", ())])
    except Exception as e:
        _DB_ERROR = str(e)[:300]
        print(f"[DB] ⚠️ تعذر الاتصال بـ Turso: {_DB_ERROR}", flush=True)
        return False
    with _lock:
        _turso_client = client
        _USE_TURSO = True
        _DB_ERROR = ""
    print(f"[DB] ✅ متصل بـ Turso: {_TURSO_URL[:45]}", flush=True)
    return True


def _retry_turso_forever():
    while not _USE_TURSO:
        time.sleep(60)
        if _try_turso():
            try:
                init_db()
            except Exception as e:
                print(f"[DB] init after reconnect failed: {e}", flush=True)
            return


def _startup():
    if _TURSO_URL and _TURSO_TOKEN:
        if not _try_turso():
            print("[DB] سيعمل البرنامج مؤقتاً على قاعدة محلية وسيعيد المحاولة كل دقيقة", flush=True)
            threading.Thread(target=_retry_turso_forever, daemon=True).start()
    else:
        print("[DB] لا توجد إعدادات Turso — قاعدة محلية مؤقتة (تُمسح عند إعادة التشغيل)", flush=True)


_startup()


# ═══════════════════════════════════════════════════════════════
# الإعدادات والجلسات
# ═══════════════════════════════════════════════════════════════
def get_setting(key, default=None):
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def get_secret_setting(key):
    return decrypt(get_setting(key))


def set_secret_setting(key, value):
    set_setting(key, encrypt(value) if value else "")


def save_user_session(license_key, ssid, cookies, email):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET quotex_ssid = ?, quotex_cookies = ?, quotex_email = ?, "
            "balance_version = balance_version + 1 WHERE license_key = ?",
            (encrypt(ssid), encrypt(cookies), email or None, license_key),
        )
        conn.commit()
    finally:
        conn.close()


def clear_user_session(license_key):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET quotex_ssid = NULL, quotex_email = NULL, quotex_cookies = NULL, "
            "is_copying = 0, is_auto_trading = 0, start_balance = NULL, last_notification = NULL, "
            "settings_locked = 0, balance_version = balance_version + 1 WHERE license_key = ?",
            (license_key,),
        )
        conn.commit()
    finally:
        conn.close()


def get_session_plain(user):
    return decrypt(user.get("quotex_ssid")), decrypt(user.get("quotex_cookies"))
