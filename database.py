# -*- coding: utf-8 -*-
import os
import sqlite3
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken

_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_BASE, "oskar.db")

_enc_key = os.environ.get("ENCRYPTION_KEY")
if not _enc_key:
    raise RuntimeError("ENCRYPTION_KEY env var is missing")
_fernet = Fernet(_enc_key.encode())


def encrypt(text):
    if not text:
        return None
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


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            expires_at TEXT,
            is_active INTEGER DEFAULT 1,
            device_ip TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
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
    """)
    
    # ترقية قاعدة البيانات القديمة (إضافة العمود إذا لم يكن موجوداً)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_auto_trading INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
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
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def get_secret_setting(key):
    return decrypt(get_setting(key))


def set_secret_setting(key, value):
    set_setting(key, encrypt(value) if value else "")


def save_user_session(license_key, ssid, cookies, email):
    conn = get_db()
    conn.execute(
        "UPDATE users SET quotex_ssid = ?, quotex_cookies = ?, quotex_email = ?, "
        "balance_version = balance_version + 1 WHERE license_key = ?",
        (encrypt(ssid), encrypt(cookies), email or None, license_key),
    )
    conn.commit()
    conn.close()


def clear_user_session(license_key):
    conn = get_db()
    conn.execute(
        "UPDATE users SET quotex_ssid = NULL, quotex_email = NULL, quotex_cookies = NULL, "
        "is_copying = 0, is_auto_trading = 0, start_balance = NULL, last_notification = NULL, "
        "settings_locked = 0, balance_version = balance_version + 1 WHERE license_key = ?",
        (license_key,),
    )
    conn.commit()
    conn.close()


def get_session_plain(user):
    return decrypt(user.get("quotex_ssid")), decrypt(user.get("quotex_cookies"))
