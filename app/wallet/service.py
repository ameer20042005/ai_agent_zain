# -*- coding: utf-8 -*-
"""منطق المحفظة الوهمية: قراءة البيانات، التسعير (quote)، وتنفيذ التحويل ودفع الفواتير.

كل قاعدة عمل (رصيد، حدود، حالة المستلم/الجهة) تُفحص **هنا بالمحفظة** وقت
التنفيذ، حتى لو الوكيل فحصها قبلها — الوكيل ممكن يكون عنده معلومة قديمة
(الرصيد تغيّر بين التأكيد والتنفيذ)، فالمحفظة هي الحكم الأخير.
"""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from app.wallet.db import now_iso, write_lock


# الشرح: خطأ عمل برمز ثابت. الوكيل يعتمد على `code` (مو على النص) حتى يختار
# رسالة الفشل المناسبة بالعراقي. status هو كود HTTP اللي يرجع للعميل.
class WalletError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def _setting(conn, key: str) -> int:
    return conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()["value"]


# ---------------------------------------------------------------------------
# القراءة
# ---------------------------------------------------------------------------

def get_user(conn, user_id: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise WalletError("USER_NOT_FOUND", f"user {user_id} not found", 404)
    return dict(row)


def list_contacts(conn, user_id: str) -> list:
    get_user(conn, user_id)
    rows = conn.execute("SELECT * FROM contacts WHERE owner_id = ? ORDER BY id", (user_id,))
    return [dict(r) for r in rows]


def list_bill_accounts(conn, user_id: str) -> list:
    get_user(conn, user_id)
    rows = conn.execute(
        """SELECT a.*, b.name AS biller_name, b.category, b.fee, b.status AS biller_status
           FROM bill_accounts a JOIN billers b ON b.id = a.biller_id
           WHERE a.user_id = ? ORDER BY a.id""", (user_id,))
    return [dict(r) for r in rows]


def list_transactions(conn, user_id: str, limit: int = 20, idempotency_key: Optional[str] = None) -> list:
    get_user(conn, user_id)
    if idempotency_key:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key))
    else:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit))
    return [dict(r) for r in rows]


def lookup_phone(conn, phone: str) -> Optional[dict]:
    """دليل المحفظة: هل هذا الرقم عنده محفظة؟ (للتحويل برقم مو محفوظ بالأسماء)."""
    row = conn.execute("SELECT id, name, phone, status FROM users WHERE phone = ?", (phone,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# الفحص (مشترك بين quote والتنفيذ)
# ---------------------------------------------------------------------------

def _check_amount(conn, amount) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < _setting(conn, "min_amount"):
        raise WalletError("INVALID_AMOUNT", f"amount must be an integer >= {_setting(conn, 'min_amount')} IQD")
    if amount > _setting(conn, "per_transaction_limit"):
        raise WalletError("LIMIT_EXCEEDED", "amount exceeds the per-transaction limit")


def _spent_today(conn, user_id: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transactions
           WHERE user_id = ? AND type IN ('transfer_out', 'bill_payment') AND created_at >= ?""",
        (user_id, today))
    return row.fetchone()["s"]


def _check_sender(conn, user: dict, amount: int, total: int) -> None:
    if user["status"] != "active":
        raise WalletError("ACCOUNT_FROZEN", "sender account is not active", 403)
    if _spent_today(conn, user["id"]) + amount > _setting(conn, "daily_limit"):
        raise WalletError("DAILY_LIMIT_EXCEEDED", "daily limit would be exceeded")
    if user["balance"] < total:
        raise WalletError("INSUFFICIENT_FUNDS", "balance is lower than amount + fee")


def plan_transfer(conn, user_id: str, amount, to_contact_id: Optional[str] = None,
                  to_phone: Optional[str] = None) -> dict:
    """يفحص تحويلاً بدون تنفيذ ويرجّع خطته (المستلم، العمولة، المجموع، الرصيد بعده)."""
    user = get_user(conn, user_id)
    _check_amount(conn, amount)
    if to_contact_id:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ? AND owner_id = ?",
                               (to_contact_id, user_id)).fetchone()
        if contact is None:
            raise WalletError("CONTACT_NOT_FOUND", "contact not found", 404)
        recipient_id, phone = contact["wallet_user_id"], contact["phone"]
    elif to_phone:
        found = lookup_phone(conn, to_phone)
        recipient_id, phone = (found["id"] if found else None), to_phone
    else:
        raise WalletError("RECIPIENT_REQUIRED", "to_contact_id or to_phone is required", 400)
    if recipient_id is None:
        raise WalletError("RECIPIENT_NOT_ON_WALLET", "recipient has no wallet account")
    if recipient_id == user_id:
        raise WalletError("SELF_TRANSFER", "cannot transfer to yourself")
    recipient = get_user(conn, recipient_id)
    if recipient["status"] != "active":
        raise WalletError("RECIPIENT_UNAVAILABLE", "recipient account cannot receive money")
    fee = _setting(conn, "transfer_fee")
    _check_sender(conn, user, amount, amount + fee)
    return {"kind": "transfer", "amount": amount, "fee": fee, "total": amount + fee,
            "balance": user["balance"], "balance_after": user["balance"] - amount - fee,
            "recipient": {"user_id": recipient_id, "name": recipient["name"], "phone": phone}}


def plan_bill_payment(conn, user_id: str, amount, bill_account_id: str) -> dict:
    """يفحص دفع فاتورة بدون تنفيذ."""
    user = get_user(conn, user_id)
    acc = conn.execute(
        """SELECT a.*, b.name AS biller_name, b.fee, b.status AS biller_status, b.category
           FROM bill_accounts a JOIN billers b ON b.id = a.biller_id
           WHERE a.id = ? AND a.user_id = ?""", (bill_account_id, user_id)).fetchone()
    if acc is None:
        raise WalletError("BILL_ACCOUNT_NOT_FOUND", "bill account not found", 404)
    if acc["biller_status"] != "active":
        raise WalletError("BILLER_UNAVAILABLE", f"{acc['biller_name']} is not accepting payments right now", 503)
    if acc["due_amount"] == 0:
        raise WalletError("NOTHING_DUE", "there is no outstanding bill on this account")
    _check_amount(conn, amount)
    if amount > acc["due_amount"]:
        raise WalletError("AMOUNT_EXCEEDS_DUE", "amount is larger than the outstanding bill")
    _check_sender(conn, user, amount, amount + acc["fee"])
    return {"kind": "bill_payment", "amount": amount, "fee": acc["fee"], "total": amount + acc["fee"],
            "balance": user["balance"], "balance_after": user["balance"] - amount - acc["fee"],
            "bill": {"account_id": acc["id"], "biller": acc["biller_name"], "category": acc["category"],
                     "label": acc["label"], "account_no": acc["account_no"],
                     "due_amount": acc["due_amount"]}}


# ---------------------------------------------------------------------------
# التنفيذ مع منع الدفع المكرر
# ---------------------------------------------------------------------------

def _tx_id() -> str:
    return "TX-" + uuid.uuid4().hex[:10].upper()


def _request_hash(kind: str, body: dict) -> str:
    return hashlib.sha256(json.dumps({"kind": kind, "body": body}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


# الشرح: قلب الحماية من الدفع المكرر. كل طلب دفع لازم يجي بمفتاح (Idempotency-Key):
#   1) نقفل القاعدة للكتابة (قفل + BEGIN IMMEDIATE) حتى ما يدخل طلبين سوية.
#   2) إذا المفتاح مسجّل سابقاً → ما ننفّذ شي، نرجّع النتيجة المحفوظة كما هي
#      (replayed=true). إذا نفس المفتاح بس بطلب مختلف (مبلغ ثاني) → 409 رفض.
#   3) إذا جديد → ننفّذ، ونحفظ النتيجة مع المفتاح **بنفس المعاملة (transaction)**.
#      فإما الخصم + تسجيل المفتاح يصيرون سوية، أو ولا واحد منهم — ما يصير خصم
#      بدون تسجيل مفتاح (اللي كان راح يسمح بخصم ثاني عند إعادة المحاولة).
def run_idempotent(conn: sqlite3.Connection, user_id: str, key: str, kind: str, body: dict,
                   operation: Callable[[sqlite3.Connection], dict]) -> tuple:
    if not key or len(key) > 128:
        raise WalletError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    request_hash = _request_hash(kind, body)
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM idempotency WHERE user_id = ? AND key = ?",
                               (user_id, key)).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                if row["request_hash"] != request_hash:
                    raise WalletError("IDEMPOTENCY_KEY_REUSED",
                                      "this key was already used for a different request", 409)
                return row["status_code"], {**json.loads(row["response_json"]), "replayed": True}

            conn.execute("SAVEPOINT op")
            try:
                status, response = 201, operation(conn)
                conn.execute("RELEASE op")
            except WalletError as e:
                # الرفض (رصيد غير كافٍ مثلاً) يُحفظ هو كمان: إعادة نفس الطلب ترجع نفس الرفض.
                conn.execute("ROLLBACK TO op")
                conn.execute("RELEASE op")
                status, response = e.status, e.to_dict()
            conn.execute(
                "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, key, request_hash, status, json.dumps(response, ensure_ascii=False), now_iso()))
            conn.execute("COMMIT")
            return status, {**response, "replayed": False}
        except WalletError:
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def execute_transfer(conn, user_id: str, key: str, body: dict) -> tuple:
    def operation(c):
        plan = plan_transfer(c, user_id, body.get("amount"), body.get("to_contact_id"), body.get("to_phone"))
        sender = get_user(c, user_id)
        rec = plan["recipient"]
        # الخصم مشروط بـ balance >= total داخل نفس الأمر: حماية إضافية حتى لو تغيّر الرصيد.
        cur = c.execute("UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
                        (plan["total"], user_id, plan["total"]))
        if cur.rowcount != 1:
            raise WalletError("INSUFFICIENT_FUNDS", "balance is lower than amount + fee")
        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (plan["amount"], rec["user_id"]))
        tx_id, created = _tx_id(), now_iso()
        c.execute("INSERT INTO transactions VALUES (?, ?, 'transfer_out', ?, ?, ?, ?, 'completed', ?, ?)",
                  (tx_id, user_id, plan["amount"], plan["fee"], rec["name"], rec["phone"], key, created))
        c.execute("INSERT INTO transactions VALUES (?, ?, 'transfer_in', ?, 0, ?, ?, 'completed', NULL, ?)",
                  (tx_id + "-IN", rec["user_id"], plan["amount"], sender["name"], sender["phone"], created))
        return {"transaction": {"id": tx_id, "type": "transfer_out", "amount": plan["amount"],
                                "fee": plan["fee"], "total": plan["total"], "counterparty": rec["name"],
                                "counterparty_ref": rec["phone"], "status": "completed",
                                "created_at": created},
                "balance": get_user(c, user_id)["balance"]}
    return run_idempotent(conn, user_id, key, "transfer", body, operation)


def execute_bill_payment(conn, user_id: str, key: str, body: dict) -> tuple:
    def operation(c):
        plan = plan_bill_payment(c, user_id, body.get("amount"), body.get("bill_account_id"))
        bill = plan["bill"]
        cur = c.execute("UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
                        (plan["total"], user_id, plan["total"]))
        if cur.rowcount != 1:
            raise WalletError("INSUFFICIENT_FUNDS", "balance is lower than amount + fee")
        c.execute("UPDATE bill_accounts SET due_amount = due_amount - ? WHERE id = ?",
                  (plan["amount"], bill["account_id"]))
        tx_id, created = _tx_id(), now_iso()
        name = f"{bill['biller']} — {bill['label']}"
        c.execute("INSERT INTO transactions VALUES (?, ?, 'bill_payment', ?, ?, ?, ?, 'completed', ?, ?)",
                  (tx_id, user_id, plan["amount"], plan["fee"], name, bill["account_id"], key, created))
        return {"transaction": {"id": tx_id, "type": "bill_payment", "amount": plan["amount"],
                                "fee": plan["fee"], "total": plan["total"], "counterparty": name,
                                "counterparty_ref": bill["account_no"], "status": "completed",
                                "created_at": created},
                "balance": get_user(c, user_id)["balance"],
                "remaining_due": bill["due_amount"] - plan["amount"]}
    return run_idempotent(conn, user_id, key, "bill_payment", body, operation)
