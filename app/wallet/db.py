# -*- coding: utf-8 -*-
"""قاعدة بيانات المحفظة الوهمية (SQLite) — إنشاء الجداول وتحميل بيانات الاختبار.

ملف واحد (data/wallet.sqlite3 افتراضياً) بلا أي خادم قاعدة بيانات. يُبنى
تلقائياً من data/wallet_seed.json أول مرة، ويُعاد لحالته الأصلية بـ reset().
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# الشرح: الجداول. ملاحظات على التصميم:
#   - كل المبالغ INTEGER بالدينار — ما نستعمل float للفلوس أبداً (أخطاء تقريب).
#   - idempotency: جدول يحفظ نتيجة كل طلب دفع حسب مفتاحه. إذا وصل نفس المفتاح
#     مرة ثانية (إعادة محاولة) نرجّع النتيجة المحفوظة بدل ما ننفّذ مرة ثانية.
#     المفتاح الأساسي (user_id, key) يمنع تكرار المفتاح على مستوى القاعدة نفسها.
#   - transactions.idempotency_key: يربط كل حركة بالطلب اللي أنشأها، حتى الوكيل
#     يكدر "يتأكد" لاحقاً إذا الدفعة صارت فعلاً أو لا (reconciliation).
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
    balance INTEGER NOT NULL CHECK (balance >= 0), status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL, phone TEXT NOT NULL, relation TEXT, nickname TEXT,
    wallet_user_id TEXT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS billers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    fee INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bill_accounts (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    biller_id TEXT NOT NULL REFERENCES billers(id), label TEXT NOT NULL,
    account_no TEXT NOT NULL, due_amount INTEGER NOT NULL CHECK (due_amount >= 0)
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL, amount INTEGER NOT NULL, fee INTEGER NOT NULL,
    counterparty TEXT NOT NULL, counterparty_ref TEXT, status TEXT NOT NULL,
    idempotency_key TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_user ON transactions(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tx_idem ON transactions(user_id, idempotency_key);
CREATE TABLE IF NOT EXISTS idempotency (
    user_id TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

# الشرح: قفل كتابة عام. SQLite يسمح بكاتب واحد بنفس الوقت؛ القفل + BEGIN IMMEDIATE
# يضمنون إن طلبين متزامنين بنفس مفتاح الـ idempotency ما ينفّذون سوية
# (الثاني ينتظر، بعدين يلگى نتيجة الأول محفوظة فيرجّعها).
write_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """اتصال جديد. isolation_level=None يعني نتحكم بالـ BEGIN/COMMIT يدوياً."""
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reset(path: Path, seed_path: Path) -> None:
    """يمسح القاعدة ويعيد بناءها من ملف البذرة — حالة معروفة قبل كل اختبار."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with write_lock:
        conn = connect(path)
        try:
            drops = "".join(f"DROP TABLE IF EXISTS {t};" for t in (
                "idempotency", "transactions", "bill_accounts", "billers",
                "contacts", "users", "settings"))
            conn.executescript(drops + SCHEMA)
            _load_seed(conn, json.loads(seed_path.read_text(encoding="utf-8")))
        finally:
            conn.close()


def ensure(path: Path, seed_path: Path) -> None:
    """يبني القاعدة فقط إذا ما موجودة (الإقلاع العادي ما يمسح بيانات موجودة)."""
    if not path.exists():
        reset(path, seed_path)


def _load_seed(conn: sqlite3.Connection, seed: dict) -> None:
    """يدخّل بيانات البذرة. days_ago بالحركات يتحوّل لتاريخ حقيقي نسبةً لليوم."""
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO settings VALUES (?, ?)", seed["settings"].items())
    conn.executemany(
        "INSERT INTO users VALUES (:id, :name, :phone, :balance, :status)", seed["users"])
    conn.executemany(
        "INSERT INTO contacts VALUES (:id, :owner_id, :name, :phone, :relation, :nickname, :wallet_user_id)",
        seed["contacts"])
    conn.executemany(
        "INSERT INTO billers VALUES (:id, :name, :category, :fee, :status)", seed["billers"])
    conn.executemany(
        "INSERT INTO bill_accounts VALUES (:id, :user_id, :biller_id, :label, :account_no, :due_amount)",
        seed["bill_accounts"])
    now = datetime.now(timezone.utc)
    for i, tx in enumerate(seed["transactions"], 1):
        created = (now - timedelta(days=tx["days_ago"])).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', NULL, ?)",
            (f"TX-SEED-{i:03d}", tx["user_id"], tx["type"], tx["amount"], tx["fee"],
             tx["counterparty"], tx["counterparty_ref"], created))
    conn.execute("COMMIT")
