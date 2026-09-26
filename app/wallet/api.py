# -*- coding: utf-8 -*-
"""واجهة HTTP للمحفظة الوهمية — تطبيق FastAPI مستقل يُركَّب على /wallet.

الوكيل يتعامل مع المحفظة **عبر HTTP فقط** (مثل ما راح يتعامل مع محفظة حقيقية)،
مو باستدعاء دوال Python مباشرة. هيچ حدود النظام واضحة، وأعطال الشبكة
(انقطاع، رد ضايع) تنمحاكى فعلياً.
"""

from typing import Optional

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.wallet import db, service
from app.wallet.service import WalletError

wallet_app = FastAPI(title="Mock Wallet API", version="1.0.0")


# الشرح: اتصال SQLite جديد لكل طلب ويتسكّر بعده (رخيص جداً بـ SQLite)، حتى
# الطلبات المتزامنة ما تتشارك نفس الاتصال ونفس المعاملة.
def get_conn():
    db.ensure(settings.wallet_db_path, settings.wallet_seed_path)
    conn = db.connect(settings.wallet_db_path)
    try:
        yield conn
    finally:
        conn.close()


@wallet_app.exception_handler(WalletError)
async def _wallet_error(_, exc: WalletError):
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


# الشرح: حقن الأعطال — لإثبات إن إعادة المحاولة ما تسبب دفعاً مكرراً:
#   - fail_before_commit: المحفظة "طايحة" — ترجع 503 بدون ما تنفّذ شي.
#   - drop_response_after_commit: أخطر حالة: الدفعة **تنفّذت** بس الرد ضاع
#     بالطريق (العميل يشوف 503). العميل يعيد المحاولة بنفس المفتاح، فيستلم
#     النتيجة المحفوظة بدل خصم ثاني.
# times = كم طلب دفع قادم يتأثر بالعطل.
_faults = {"mode": None, "remaining": 0}


def _take_fault(mode: str) -> bool:
    if _faults["mode"] == mode and _faults["remaining"] > 0:
        _faults["remaining"] -= 1
        if _faults["remaining"] == 0:
            _faults["mode"] = None
        return True
    return False


_SIMULATED = {"error": {"code": "SIMULATED_OUTAGE", "message": "simulated network failure"}}


# ---------------------------------------------------------------------------
# القراءة
# ---------------------------------------------------------------------------

@wallet_app.get("/health")
def health():
    return {"status": "healthy"}


@wallet_app.get("/users/{user_id}")
def user(user_id: str, conn=Depends(get_conn)):
    return service.get_user(conn, user_id)


@wallet_app.get("/users/{user_id}/contacts")
def contacts(user_id: str, conn=Depends(get_conn)):
    return service.list_contacts(conn, user_id)


@wallet_app.get("/users/{user_id}/bill-accounts")
def bill_accounts(user_id: str, conn=Depends(get_conn)):
    return service.list_bill_accounts(conn, user_id)


@wallet_app.get("/users/{user_id}/transactions")
def transactions(user_id: str, limit: int = Query(20, ge=1, le=100),
                 idempotency_key: Optional[str] = None, conn=Depends(get_conn)):
    return service.list_transactions(conn, user_id, limit, idempotency_key)


@wallet_app.get("/directory/{phone}")
def directory(phone: str, conn=Depends(get_conn)):
    found = service.lookup_phone(conn, phone)
    if found is None:
        raise WalletError("PHONE_NOT_REGISTERED", "no wallet account for this phone", 404)
    return found


# ---------------------------------------------------------------------------
# التسعير والتنفيذ
# ---------------------------------------------------------------------------

class QuoteRequest(BaseModel):
    kind: str                          # "transfer" | "bill_payment"
    amount: int
    to_contact_id: Optional[str] = None
    to_phone: Optional[str] = None
    bill_account_id: Optional[str] = None


# الشرح: quote = "شنو راح يصير لو نفّذت؟" بدون تنفيذ. الوكيل يستعمله قبل ما
# يعرض بطاقة التأكيد، حتى البطاقة تبيّن العمولة والرصيد بعد العملية، وحتى
# الرفض المتوقع (رصيد ناقص) يظهر **قبل** التأكيد مو بعده.
@wallet_app.post("/users/{user_id}/quotes")
def quote(user_id: str, req: QuoteRequest, conn=Depends(get_conn)):
    try:
        if req.kind == "transfer":
            plan = service.plan_transfer(conn, user_id, req.amount, req.to_contact_id, req.to_phone)
        elif req.kind == "bill_payment":
            plan = service.plan_bill_payment(conn, user_id, req.amount, req.bill_account_id)
        else:
            raise WalletError("INVALID_KIND", "kind must be transfer or bill_payment", 400)
        return {"ok": True, **plan}
    except WalletError as e:
        if e.status == 404 and e.code == "USER_NOT_FOUND":
            raise
        balance = service.get_user(conn, user_id)["balance"]
        return {"ok": False, "balance": balance, **e.to_dict()}


class TransferRequest(BaseModel):
    amount: int
    to_contact_id: Optional[str] = None
    to_phone: Optional[str] = None


class BillPaymentRequest(BaseModel):
    amount: int
    bill_account_id: str


def _execute(fn, conn, user_id: str, key: Optional[str], body: dict):
    if _take_fault("fail_before_commit"):
        return JSONResponse(status_code=503, content=_SIMULATED)
    status, payload = fn(conn, user_id, key or "", body)
    if _take_fault("drop_response_after_commit"):
        return JSONResponse(status_code=503, content=_SIMULATED)   # الدفعة صارت، الرد "ضاع"
    return JSONResponse(status_code=status, content=payload)


@wallet_app.post("/users/{user_id}/transfers")
def transfer(user_id: str, req: TransferRequest, conn=Depends(get_conn),
             idempotency_key: Optional[str] = Header(None)):
    return _execute(service.execute_transfer, conn, user_id, idempotency_key, req.model_dump())


@wallet_app.post("/users/{user_id}/bill-payments")
def bill_payment(user_id: str, req: BillPaymentRequest, conn=Depends(get_conn),
                 idempotency_key: Optional[str] = Header(None)):
    return _execute(service.execute_bill_payment, conn, user_id, idempotency_key, req.model_dump())


# ---------------------------------------------------------------------------
# إدارة (للاختبار والعرض فقط)
# ---------------------------------------------------------------------------

@wallet_app.post("/_admin/reset")
def admin_reset():
    db.reset(settings.wallet_db_path, settings.wallet_seed_path)
    _faults.update(mode=None, remaining=0)
    return {"status": "reset"}


class FaultRequest(BaseModel):
    mode: Optional[str] = None         # fail_before_commit | drop_response_after_commit | None
    times: int = 1


@wallet_app.post("/_admin/faults")
def admin_faults(req: FaultRequest):
    _faults.update(mode=req.mode, remaining=req.times if req.mode else 0)
    return dict(_faults)


@wallet_app.get("/_admin/faults")
def admin_faults_get():
    return dict(_faults)
