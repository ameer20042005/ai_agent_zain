# -*- coding: utf-8 -*-
"""إيند بوينتات الوكيل الحر (الموديل يكتب كل الردود ويستدعي الأدوات).

  POST /assistant/message   رسالة نصية
  POST /assistant/confirm   ضغطة "أكّد" على بطاقة — المكان الوحيد اللي ينفّذ دفعة
  POST /assistant/cancel    ضغطة "إلغاء"
  GET  /assistant/session/{id}  المحادثة والبطاقات (للتشخيص)

شكل الرد نفس /agent (session_id, parser, state, messages) حتى الصفحة تعرضه
بنفس الطريقة. الوكيل القديم /agent باقي مثل ما هو لمجموعة الاختبار.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.assistant.agent import Assistant, Session, SessionStore
from app.config import settings

router = APIRouter(prefix="/assistant", tags=["assistant"])

assistant = Assistant()
sessions = SessionStore()


# الشرح: أشكال الطلبات. session_id فارغ = محادثة جديدة.
class MessageRequest(BaseModel):
    text: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None


class ConfirmRequest(BaseModel):
    session_id: str
    confirmation_id: str


class CancelRequest(BaseModel):
    session_id: str
    confirmation_id: Optional[str] = None


class AssistantReply(BaseModel):
    session_id: str
    user_id: str
    parser: str
    state: dict
    messages: List[dict]


# الشرح: الرد يحمل قائمة البطاقات المعلّقة حتى الصفحة تقفل أزرار أي بطاقة
# ما عادت فعّالة (انلغت، تنفّذت، أو انستبدلت ببطاقة أحدث).
def _reply(s: Session, out: list) -> AssistantReply:
    ids = [c.id for c in s.pending()]
    return AssistantReply(session_id=s.id, user_id=s.user_id, parser="llm", messages=out,
                          state={"pending_confirmation_ids": ids,
                                 "pending_confirmation_id": ids[-1] if ids else None})


def _session(session_id: str) -> Session:
    s = sessions.find(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return s


# الشرح: كل إيند بوينت يمسك قفل الجلسة — ضغطتين سريعتين على "أكّد" تتعالج
# وحدة بعد الثانية، والثانية تلگى البطاقة منفّذة فما تخصم مرة ثانية.
@router.post("/message", response_model=AssistantReply)
async def message(req: MessageRequest):
    s = sessions.get(req.session_id, req.user_id or settings.default_user_id)
    async with s.lock:
        out = await assistant.handle_message(s, req.text)
    return _reply(s, out)


@router.post("/confirm", response_model=AssistantReply)
async def confirm(req: ConfirmRequest):
    s = _session(req.session_id)
    async with s.lock:
        out = await assistant.confirm(s, req.confirmation_id)
    return _reply(s, out)


@router.post("/cancel", response_model=AssistantReply)
async def cancel(req: CancelRequest):
    s = _session(req.session_id)
    async with s.lock:
        out = await assistant.cancel(s, req.confirmation_id)
    return _reply(s, out)


@router.get("/session/{session_id}")
def session_state(session_id: str):
    s = _session(session_id)
    return {"session_id": s.id, "user_id": s.user_id, "history": s.history,
            "cards": {k: {"status": c.status, "card": c.card} for k, c in s.cards.items()}}
