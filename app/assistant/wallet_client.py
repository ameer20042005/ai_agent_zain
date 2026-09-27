# -*- coding: utf-8 -*-
"""عميل HTTP للمحفظة — كل تواصل الوكيل مع المحفظة يمر من هنا.

إذا wallet_base_url فارغ، الطلبات تروح للمحفظة المركّبة بنفس العملية عبر
ASGITransport (نفس طبقة HTTP: مسارات، هيدرات، أكواد حالة — بدون منفذ شبكة).
إذا مضبوط لرابط، تروح عبر الشبكة لمحفظة مستقلة.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    """نتيجة تنفيذ دفعة.
    outcome:
      completed  تمت (data فيها الحركة والرصيد الجديد)
      rejected   المحفظة رفضت بسبب عمل (رصيد، حدود...) — **ما انخصم شي**
      not_done   فشل تقني، وتأكدنا من سجل المحفظة إن ما صار خصم
      unknown    فشل تقني وما گدرنا نتأكد — لا نقول "فشل" ولا "نجح"
    """
    outcome: str
    data: Optional[dict] = None
    error_code: Optional[str] = None
    attempts: int = 1
    replayed: bool = False


class WalletClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        if client is not None:
            self._client = client
        elif settings.wallet_base_url:
            self._client = httpx.AsyncClient(base_url=settings.wallet_base_url, timeout=10.0)
        else:
            from app.wallet.api import wallet_app      # استيراد متأخر لتجنّب الدوران
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=wallet_app), base_url="http://wallet", timeout=10.0)

    async def _get(self, path: str, **params):
        resp = await self._client.get(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    # ── القراءة ────────────────────────────────────────────────────────────
    async def user(self, uid: str) -> dict:
        return await self._get(f"/users/{uid}")

    async def contacts(self, uid: str) -> list:
        return await self._get(f"/users/{uid}/contacts")

    async def bill_accounts(self, uid: str) -> list:
        return await self._get(f"/users/{uid}/bill-accounts")

    async def transactions(self, uid: str, limit: int = 5, idempotency_key: Optional[str] = None) -> list:
        params = {"limit": limit}
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        return await self._get(f"/users/{uid}/transactions", **params)

    async def quote(self, uid: str, body: dict) -> dict:
        resp = await self._client.post(f"/users/{uid}/quotes", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── التنفيذ ────────────────────────────────────────────────────────────
    # الشرح: التنفيذ مع إعادة المحاولة الآمنة:
    #   1) نرسل الطلب مع Idempotency-Key = رقم بطاقة التأكيد. كل المحاولات
    #      تستعمل **نفس المفتاح**، فالمحفظة تنفّذ مرة وحدة كحد أقصى.
    #   2) خطأ شبكة أو 5xx → ننتظر شوي ونعيد (نفس المفتاح).
    #   3) خطأ 4xx = رفض عمل (رصيد ناقص...) → ما نعيد؛ الإعادة ما تغيّر شي.
    #   4) إذا خلصت المحاولات → نسأل المحفظة: "أكو حركة بهذا المفتاح؟"
    #      (reconciliation). هيچ نعرف إذا الدفعة صارت رغم ضياع الرد.
    async def execute(self, uid: str, kind: str, body: dict, key: str) -> ExecResult:
        path = f"/users/{uid}/transfers" if kind == "transfer" else f"/users/{uid}/bill-payments"
        attempts = settings.wallet_retry_attempts
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.post(path, json=body, headers={"Idempotency-Key": key})
            except httpx.TransportError as e:
                logger.warning("wallet transport error (attempt %d/%d): %s", attempt, attempts, e)
            else:
                payload = resp.json() if resp.content else {}
                if resp.status_code in (200, 201):
                    return ExecResult("completed", payload, attempts=attempt,
                                      replayed=bool(payload.get("replayed")))
                if resp.status_code < 500:
                    code = (payload.get("error") or {}).get("code", f"HTTP_{resp.status_code}")
                    return ExecResult("rejected", payload, code, attempts=attempt)
                code = (payload.get("error") or {}).get("code", "")
                if code == "BILLER_UNAVAILABLE":            # 503 بس سببه الجهة، مو الشبكة
                    return ExecResult("rejected", payload, code, attempts=attempt)
                logger.warning("wallet %s (attempt %d/%d)", resp.status_code, attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(0.2 * attempt)

        try:
            found = await self.transactions(uid, idempotency_key=key)
        except Exception as e:
            logger.error("reconciliation failed for key %s: %s", key, e)
            return ExecResult("unknown", attempts=attempts)
        if found:
            balance = (await self.user(uid))["balance"]
            return ExecResult("completed", {"transaction": found[0], "balance": balance},
                              attempts=attempts, replayed=True)
        return ExecResult("not_done", attempts=attempts)
