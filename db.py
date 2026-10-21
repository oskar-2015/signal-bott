# -*- coding: utf-8 -*-
import os
import sqlite3
import uuid
import secrets
from datetime import datetime, timedelta
from cryptography.fernet import Fernet

DB_FILE = "oscar_trader.db"

_enc_key = os.environ.get("ENCRYPTION_KEY")
if not _enc_key:
    raise RuntimeError("ENCRYPTION_KEY env var is missing. Generate one with Fernet.generate_key()")
_fernet = Fernet(_enc_key.encode())

DURATION_MAP = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "lifetime": None,
}


def encrypt(text):
    if text is None:
        return None
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token):
    if token is None:
        return None
    return _fernet.decrypt(token.encode()).decode()


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS activation_keys (
        id TEXT PRIMARY KEY,
        key_code TEXT UNIQUE NOT NULL,
        duration_type TEXT NOT NULL,
        expires_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        key_id TEXT NOT NULL REFERENCES activation_keys(id),
        qx_cookies TEXT,
        qx_ssid TEXT,
        account_mode TEXT NOT NULL DEFAULT 'demo',
        auto_trade_enabled INTEGER NOT NULL DEFAULT 0,
        stop_loss_usd REAL NOT NULL DEFAULT 0,
        take_profit_usd REAL NOT NULL DEFAULT 0,
        session_pl_usd REAL NOT NULL DEFAULT 0,
        last_login_at TEXT
    );

    CREATE TABLE IF NOT EXISTS trade_log (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        asset TEXT NOT NULL,
        direction TEXT NOT NULL,
        amount_usd REAL NOT NULL,
        result TEXT NOT NULL DEFAULT 'pending',
        pl_usd REAL NOT NULL DEFAULT 0,
        executed_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


# ---------- activation_keys ----------

def create_key(duration_type, note=None):
    if duration_type not in DURATION_MAP:
        raise ValueError("invalid duration_type")
    key_id = str(uuid.uuid4())
    key_code = secrets.token_urlsafe(12)
    delta = DURATION_MAP[duration_type]
    expires_at = (datetime.utcnow() + delta).isoformat() if delta else None
    conn = get_conn()
    conn.execute(
        "INSERT INTO activation_keys (id, key_code, duration_type, expires_at, status, created_at, note) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?)",
        (key_id, key_code, duration_type, expires_at, datetime.utcnow().isoformat(), note)
    )
    conn.commit()
    conn.close()
    return {"id": key_id, "key_code": key_code, "expires_at": expires_at}


def list_keys():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM activation_keys ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_key_status(key_id, status):
    if status not in ("active", "paused", "revoked"):
        raise ValueError("invalid status")
    conn = get_conn()
    conn.execute("UPDATE activation_keys SET status = ? WHERE id = ?", (status, key_id))
    conn.commit()
    conn.close()


def get_key_by_code(key_code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM activation_keys WHERE key_code = ?", (key_code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def is_key_valid(key_row):
    if key_row is None or key_row["status"] != "active":
        return False
    if key_row["expires_at"]:
        if datetime.fromisoformat(key_row["expires_at"]) < datetime.utcnow():
            return False
    return True


# ---------- users ----------

def get_or_create_user_for_key(key_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE key_id = ?", (key_id,)).fetchone()
    if row:
        conn.close()
        return dict(row)
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, key_id, account_mode, auto_trade_enabled, stop_loss_usd, take_profit_usd, session_pl_usd, last_login_at) "
        "VALUES (?, ?, 'demo', 0, 0, 0, 0, ?)",
        (user_id, key_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row)


def update_user_session(user_id, cookies_str, ssid):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET qx_cookies = ?, qx_ssid = ?, last_login_at = ? WHERE id = ?",
        (encrypt(cookies_str), encrypt(ssid), datetime.utcnow().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def update_user_settings(user_id, account_mode=None, auto_trade_enabled=None,
                          stop_loss_usd=None, take_profit_usd=None):
    fields, values = [], []
    if account_mode is not None:
        fields.append("account_mode = ?"); values.append(account_mode)
    if auto_trade_enabled is not None:
        fields.append("auto_trade_enabled = ?"); values.append(1 if auto_trade_enabled else 0)
    if stop_loss_usd is not None:
        fields.append("stop_loss_usd = ?"); values.append(stop_loss_usd)
    if take_profit_usd is not None:
        fields.append("take_profit_usd = ?"); values.append(take_profit_usd)
    if not fields:
        return
    values.append(user_id)
    conn = get_conn()
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_active_trading_users():
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.* FROM users u
        JOIN activation_keys k ON u.key_id = k.id
        WHERE u.auto_trade_enabled = 1
          AND k.status = 'active'
          AND (k.expires_at IS NULL OR k.expires_at > ?)
          AND u.qx_cookies IS NOT NULL
    """, (datetime.utcnow().isoformat(),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_decrypted_session(user_row):
    return decrypt(user_row["qx_cookies"]), decrypt(user_row["qx_ssid"])


def add_session_pl(user_id, pl_usd):
    conn = get_conn()
    conn.execute("UPDATE users SET session_pl_usd = session_pl_usd + ? WHERE id = ?", (pl_usd, user_id))
    conn.commit()
    conn.close()


def disable_auto_trade(user_id):
    conn = get_conn()
    conn.execute("UPDATE users SET auto_trade_enabled = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- trade_log ----------

def log_trade(user_id, asset, direction, amount_usd, result="pending", pl_usd=0):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trade_log (id, user_id, asset, direction, amount_usd, result, pl_usd, executed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, asset, direction, amount_usd, result, pl_usd, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
