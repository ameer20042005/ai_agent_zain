# -*- coding: utf-8 -*-
"""إيند بوينتات وكيل الدفع والتحويل.

  POST /agent/message   رسالة نصية (أو اختيار من خيارات سؤال)
  POST /agent/confirm   ضغطة "أكّد" على بطاقة تأكيد معيّنة
  POST /agent/cancel    ضغطة "الغي"
  GET  /agent/session/{id}  حالة الجلسة (للواجهة والتشخيص)

كل رد فيه قائمة messages؛ كل رسالة عندها code ثابت (مثل confirm_transfer،
insufficient_funds) تعتمد عليه الواجهة ومجموعة الاختبار.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.core import Agent, Session, SessionStore
from app.config import settings

router = APIRouter(prefix="/agent", tags=["agent"])

agent = Agent()
sessions = SessionStore()


class MessageRequest(BaseModel):
    text: Optional[str] = None
    option_id: Optional[str] = None           # جواب سؤال بزر بدل ما يكتب
    session_id: Optional[str] = None          # فارغ = جلسة جديدة
    user_id: Optional[str] = None             # فارغ = المستخدم الافتراضي (u1)


class ConfirmRequest(BaseModel):
    session_id: str
    confirmation_id: str


class CancelRequest(BaseModel):
    session_id: str
    confirmation_id: Optional[str] = None
    everything: bool = False


class AgentReply(BaseModel):
    session_id: str
    user_id: str
    parser: str
    state: dict
    messages: List[dict]


def _reply(s: Session, out: list) -> AgentReply:
    return AgentReply(
        session_id=s.id, user_id=s.user_id, parser=s.parser, messages=out,
        state={"awaiting": s.awaiting,
               "pending_confirmation_id": s.pending.id if s.pending else None,
               "queued_requests": len(s.queue)})


def _session(session_id: str) -> Session:
    s = sessions.find(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return s


# الشرح: قفل لكل جلسة — إذا المستخدم ضغط "أكّد" مرتين بسرعة (أو الشبكة
# أعادت الطلب)، الطلب الثاني ينتظر الأول يخلص، بعدين يشوف العملية "منفّذة"
# ويرجع نفس النتيجة بدل تنفيذ ثاني.
@router.post("/message", response_model=AgentReply)
async def message(req: MessageRequest):
    s = sessions.get(req.session_id, req.user_id or settings.default_user_id)
    async with s.lock:
        out = await agent.handle_message(s, req.text or "", req.option_id)
    return _reply(s, out)


@router.post("/confirm", response_model=AgentReply)
async def confirm(req: ConfirmRequest):
    s = _session(req.session_id)
    async with s.lock:
        out = await agent.confirm(s, req.confirmation_id)
    return _reply(s, out)


@router.post("/cancel", response_model=AgentReply)
async def cancel(req: CancelRequest):
    s = _session(req.session_id)
    async with s.lock:
        out = await agent.cancel(s, req.confirmation_id, req.everything)
    return _reply(s, out)


@router.get("/session/{session_id}")
def session_state(session_id: str):
    s = _session(session_id)
    return {"session_id": s.id, "user_id": s.user_id, "awaiting": s.awaiting,
            "pending": s.pending.card if s.pending else None,
            "queued_requests": [d.text for d in s.queue], "log": s.log}


# الشرح: للاختبار فقط — يخلّي بطاقة التأكيد الحالية "منتهية" حتى نختبر سلوك
# انتهاء المهلة بدون ما ننتظر 5 دقائق.
@router.post("/_debug/expire", include_in_schema=False)
async def debug_expire(req: CancelRequest):
    s = _session(req.session_id)
    if s.pending:
        s.pending.expires_at = 0
    return {"expired": bool(s.pending)}
