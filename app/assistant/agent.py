# -*- coding: utf-8 -*-
"""الوكيل الحر: الموديل الخام يدير المحادثة بنفسه ويستدعي الأدوات (قيوده بالبرومبت).

    رسالة المستخدم ──► الموديل ──► يرد بكلامه
                          │  ▲
                 tool_call ▼  │ نتيجة JSON
                        الأدوات (quote، بطاقة، سجل...)

    زر "أكّد" ──► الكود ينفّذ بالمحفظة (بمفتاح البطاقة) ──► حدث للموديل ──► يحچي النتيجة

ماكو آلة حالات ولا قوالب ردود ولا حرّاس على وسائط الأدوات — الموديل يقرر شنو
يسأل، وأي مبلغ ولمن، وشلون يصيغ. اللي يبقى بالكود هو عمل التطبيق نفسه: التنفيذ
بزر أكّد، أرقام البطاقة من المحفظة، ومنع الخصم المكرر.
"""

# الشرح: الاستيرادات.
#   - llm_engine: عميل الموديل (vLLM أو LM Studio حسب الإعدادات).
#   - normalize/tokens: فاحص "اكد" بالأسفل يشتغل على النص الموحّد.
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.assistant.textnorm import normalize, tokens
from app.assistant.tools import TOOLS, Card, ToolRunner, _describe
from app.assistant.wallet_client import WalletClient
from app.config import settings
from app.engine import llm_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# كلمات الموافقة
# ---------------------------------------------------------------------------

# الشرح: كتابة "اكد" تنفّذ البطاقة بقرار من الكود، مو من الموديل — حتى ما يكدر
# نص خبيث يقنع الموديل إن المستخدم وافق. الموافقة لازم تكون رسالة قصيرة كلها
# كلمات موافقة ("اي اكد حبيبي"). "اي بس خليها 30" مو موافقة — بيها "بس" يعني
# تعديل، فتروح للموديل يفهمها.
_YES = {"اكد", "اكدها", "تاكيد", "نعم", "اي", "ايه", "ايي", "اوك", "ok", "okay", "تمام", "موافق",
        "يلا", "نفذ", "نفذها", "كمل", "صح", "اكيد", "ماشي", "بلي", "yes", "confirm", "ثبت", "ثبتها"}
# فعل العملية نفسه كجواب على بطاقتها ("دزها" على بطاقة تحويل) = موافقة. بس
# "ادفع" على بطاقة تحويل مو موافقة.
_YES_VERBS = {"transfer": {"دز", "دزها", "حول", "حولها"}, "bill_payment": {"ادفع", "ادفعها", "سدد", "سددها"}}
# كلمات تجي ويا الموافقة بدون ما تغيّر معناها ("اكد التحويل"، "اي نفذ العملية").
_FILLER = {"حبيبي", "عيوني", "رجاءا", "رجاء", "اخي", "خويه", "هسه", "بسرعه", "الله", "يخليك",
           "و", "من", "فضلك", "لطفا", "شكرا", "التحويل", "تحويل", "الحواله", "الدفع", "الدفعه",
           "العمليه", "عمليه", "الفاتوره", "القائمه", "القايمه", "هاي", "هذي", "هذا", "ها", "ياه"}


def _is_yes(toks: List[str], kind: Optional[str] = None) -> bool:
    yes = _YES | _YES_VERBS.get(kind, set())
    return 0 < len(toks) <= 5 and all(t in yes | _FILLER for t in toks) and any(t in yes for t in toks)

# الشرح: حدود الحلقة:
#   - _MAX_STEPS: أقصى عدد مرات يستدعي بيها الموديل أدوات قبل ما يرد (يمنع حلقة لا نهائية).
#   - _HISTORY: كم رسالة سابقة نرسل للموديل (سياق أقصر = رد أسرع).
#   - _TEMPERATURE: شوية حرية بالصياغة بدل رد حرفي متكرر.
_MAX_STEPS = 5
_HISTORY = 40
_TEMPERATURE = 0.4


# ---------------------------------------------------------------------------
# الجلسة
# ---------------------------------------------------------------------------

# الشرح: كل جلسة تحفظ:
#   - history: المحادثة بصيغة OpenAI (user / assistant / tool / أحداث التطبيق).
#   - cards: كل بطاقات التأكيد (المعلّقة والمنتهية)، بالرقم.
#   - lock: يمنع طلبين بنفس الوقت على نفس الجلسة (ضغطتين سريعتين على أكّد).
@dataclass
class Session:
    id: str
    user_id: str
    history: List[dict] = field(default_factory=list)
    cards: Dict[str, Card] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    updated_at: float = field(default_factory=time.time)

    def pending(self) -> List[Card]:
        return [c for c in self.cards.values() if c.status == "pending"]


class SessionStore:
    """جلسات بالذاكرة، تنمسح بعد ساعة بدون نشاط."""

    def __init__(self, ttl_seconds: int = 3600):
        self._sessions: dict = {}
        self._ttl = ttl_seconds

    def get(self, session_id: Optional[str], user_id: str) -> Session:
        now = time.time()
        for sid in [k for k, s in self._sessions.items() if now - s.updated_at > self._ttl]:
            del self._sessions[sid]
        s = self._sessions.get(session_id) if session_id else None
        if s is None:
            s = Session(id=session_id or uuid.uuid4().hex[:12], user_id=user_id)
            self._sessions[s.id] = s
        s.updated_at = now
        return s

    def find(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)


# ---------------------------------------------------------------------------
# الوكيل
# ---------------------------------------------------------------------------

class Assistant:
    def __init__(self, wallet: Optional[WalletClient] = None):
        self.wallet = wallet or WalletClient()
        self.tools = ToolRunner(self.wallet)

    # ── رسالة من المستخدم ──────────────────────────────────────────────────
    # الشرح: حالة وحدة بس يقررها الكود قبل الموديل: "اكد" (رسالة قصيرة كلها
    # موافقة) وأكو بطاقة وحدة معلّقة → تنفيذ مباشر، نفس زر أكّد. التنفيذ ما
    # ينترك للموديل أبداً. أي شي ثاني (حتى "لا خليها 25 الف" — تعديل مو إلغاء)
    # يروح للموديل يفهمه بحرية؛ الإلغاء عنده أداة cancel_pending وهو آمن.
    async def handle_message(self, s: Session, text: str) -> list:
        text = (text or "").strip()
        if not text:
            return []
        if not llm_engine.ready:
            return [_msg("model_offline", "error", "الموديل مو شغّال هسه — شغّل LM Studio وجرّب مرة ثانية. ما تنفّذ شي.")]
        s.history.append({"role": "user", "content": text})

        pending = s.pending()
        if len(pending) == 1 and _is_yes(tokens(normalize(text)), pending[0].kind):
            return await self._execute(s, pending[0])
        return await self._run(s)

    # ── زر أكّد ───────────────────────────────────────────────────────────
    async def confirm(self, s: Session, confirmation_id: str) -> list:
        card = s.cards.get(confirmation_id)
        if card is None:
            _event(s, {"event": "confirm_pressed_on_unknown_card", "note": "nothing was executed"})
            return await self._run(s)
        return await self._execute(s, card)

    # ── زر إلغاء ──────────────────────────────────────────────────────────
    async def cancel(self, s: Session, confirmation_id: Optional[str]) -> list:
        cards = [s.cards[confirmation_id]] if confirmation_id in s.cards else s.pending()
        cancelled = [_describe(c) for c in cards if c.status == "pending"]
        for c in cards:
            if c.status == "pending":
                c.status = "cancelled"
        _event(s, {"event": "user_pressed_cancel", "cancelled": cancelled, "note": "nothing was executed"})
        return await self._run(s)

    # ── التنفيذ (الكود فقط) ────────────────────────────────────────────────
    # الشرح: هذا المكان الوحيد اللي تتحرك بيه الفلوس. الترتيب:
    #   1) بطاقة منفّذة أصلاً (ضغطة ثانية) → ما ننفّذ، نبلّغ الموديل.
    #   2) بطاقة ملغية/مرفوضة → ما ننفّذ.
    #   3) بطاقة منتهية وما انحاول تنفيذها → ما ننفّذ؛ الموديل يعرض بطاقة جديدة
    #      بأرقام محدّثة إذا المستخدم بعده يريدها.
    #   4) غير ذلك → تنفيذ بمفتاح = رقم البطاقة (الإعادة ما تخصم مرتين).
    # بكل الحالات النتيجة تروح للموديل كـ "حدث" وهو يحچيها للمستخدم بكلامه.
    async def _execute(self, s: Session, card: Card) -> list:
        facts = {"card": _describe(card)}
        if card.status == "completed":
            _event(s, {"event": "confirm_pressed_again", **facts, "note": "already completed earlier — NOT executed twice",
                       "transaction_id": card.result["transaction"]["id"]})
            return await self._run(s, kind="result", transaction=card.result["transaction"])
        if card.status != "pending":
            _event(s, {"event": "confirm_pressed_on_inactive_card", **facts, "card_status": card.status,
                       "note": "nothing was executed"})
            return await self._run(s)
        if time.time() > card.expires_at and not card.attempted:
            card.status = "expired"
            _event(s, {"event": "card_expired", **facts, "note": "nothing was executed. If the user still wants it, "
                                                                 "propose it again to show fresh numbers."})
            return await self._run(s)

        card.attempted = True
        res = await self.wallet.execute(s.user_id, card.kind, card.body, key=card.id)
        if res.outcome == "completed":
            card.status, card.result = "completed", res.data
            tx = res.data["transaction"]
            _event(s, {"event": "payment_completed", **facts, "transaction_id": tx["id"], "amount": tx["amount"],
                       "fee": tx["fee"], "to": tx["counterparty"], "new_balance": res.data.get("balance")})
            return await self._run(s, kind="result", transaction=tx, balance=res.data.get("balance"))
        if res.outcome == "rejected":
            card.status = "rejected"
            err = (res.data or {}).get("error") or {}
            _event(s, {"event": "payment_rejected", **facts, "error_code": res.error_code,
                       "wallet_message": err.get("message"), "note": "nothing was deducted"})
            return await self._run(s, kind="error", fallback=f"ما تنفّذت ({res.error_code}). ما انخصم شي.")
        if res.outcome == "not_done":
            _event(s, {"event": "wallet_unreachable", **facts, "note": "Tell the user clearly: the payment did NOT go "
                       "through and nothing was deducted (the wallet did not respond). The same card stays active: "
                       "pressing confirm again is safe (same key, cannot charge twice)."})
            return await self._run(s, kind="error", keep_card=card,
                                   fallback="ما تنفّذت وما انخصم شي. تكدر تضغط أكّد مرة ثانية بأمان.")
        _event(s, {"event": "payment_status_unknown", **facts, "note": "Tell the user clearly: we could not confirm "
                   "whether it went through. Do NOT say it succeeded or failed. They should not start a new request; "
                   "pressing confirm again on the same card is safe (same key, cannot charge twice)."})
        return await self._run(s, kind="error", keep_card=card,
                               fallback="ما نعرف بعد إذا تنفّذت. اضغط أكّد على نفس البطاقة (آمن، ما تنخصم مرتين).")

    # ── حلقة الموديل ↔ الأدوات ─────────────────────────────────────────────
    # الشرح: قلب الوكيل:
    #   1) نرسل للموديل: البرومبت الحي + آخر المحادثة + تعريف الأدوات.
    #   2) إذا طلب أدوات → ننفّذها ونضيف نتائجها للمحادثة ونرجع للخطوة 1.
    #   3) إذا رد بنص بدون أدوات → هذا رده النهائي للمستخدم.
    # الرد يطلع: نص الموديل أولاً، بعده أي بطاقة تأكيد انعرضت بهذا الدور.
    # fallback: جملة الحقيقة المجرّدة إذا الموديل طاح بنفس اللحظة — ما نكدر نسكت
    # عن نتيجة دفعة. (بالحالة العادية ما تنعرض؛ الموديل هو اللي يحچي.)
    async def _run(self, s: Session, kind: str = "agent", keep_card: Optional[Card] = None,
                   fallback: Optional[str] = None, **extra) -> list:
        cards: List[Card] = []
        text = ""
        try:
            for _ in range(_MAX_STEPS):
                messages = [{"role": "system", "content": await self.tools.system_prompt(s)}] + _recent(s.history)
                data = await llm_engine.chat(messages, tools=TOOLS, temperature=_TEMPERATURE, max_tokens=700)
                msg = data["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                text = (msg.get("content") or "").strip()
                s.history.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
                if not calls:
                    break
                for call in calls:
                    result, card = await self.tools.run(s, call["function"]["name"], call["function"].get("arguments"))
                    s.history.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                      "content": json.dumps(result, ensure_ascii=False)})
                    if card is not None:
                        cards.append(card)
        except Exception as e:                                  # الموديل طايح / مهلة / رد خربان
            logger.warning("assistant LLM loop failed: %s", e)
            text = fallback or ("" if extra.get("transaction") else "ما گدرت أوصل للموديل هسه. ما تنفّذ شي.")
            kind = "error" if kind == "agent" else kind

        out = []
        if text or extra.get("transaction"):
            out.append(_msg("reply", kind, text, **extra))
        if keep_card is not None and keep_card.status == "pending":
            cards.insert(0, keep_card)
        for c in cards:
            if c.status == "pending":
                out.append(_msg("confirmation", "confirmation", "", confirmation=c.card))
        return out


# ---------------------------------------------------------------------------
# دوال مساعدة
# ---------------------------------------------------------------------------

def _msg(code: str, kind: str, text: str, **extra) -> dict:
    return {"code": code, "kind": kind, "text": text, **{k: v for k, v in extra.items() if v is not None}}


# الشرح: "حدث من التطبيق" = رسالة system بنص المحادثة تخبر الموديل بشي صار خارج
# كلامه (ضغطة زر، نتيجة دفعة). الموديل يثق بيها لأنها مو من المستخدم.
def _event(s: Session, payload: dict) -> None:
    s.history.append({"role": "system", "content": "APP EVENT (from the app, not the user): "
                                                   + json.dumps(payload, ensure_ascii=False)})


# الشرح: نقص المحادثة لآخر _HISTORY رسالة، بس نبدأ دائماً من رسالة مستخدم أو
# حدث — حتى ما نقطع بين طلب أداة ونتيجتها (القالب يرفض نتيجة أداة بدون طلبها).
def _recent(history: List[dict]) -> List[dict]:
    part = history[-_HISTORY:]
    while part and part[0]["role"] not in ("user", "system"):
        part = part[1:]
    return part
