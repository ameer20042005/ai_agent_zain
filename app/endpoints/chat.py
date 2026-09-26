# -*- coding: utf-8 -*-
"""إيند بوينت افتراضي بسيط: POST /chat — يرسل رسالة للموديل ويرجع الرد.

نقطة بداية فقط؛ عدّله أو انسخه لإيند بوينتات مخصصة.
"""

# الشرح: الاستيرادات.
#   - APIRouter: مجموعة إيند بوينتات مستقلة تنربط بالتطبيق من main.py.
#   - HTTPException: لإرجاع خطأ HTTP واضح (مثل 503 إذا الموديل بعده يتحمّل).
#   - BaseModel: لتعريف شكل جسم الطلب والرد — FastAPI يتحقق منه تلقائياً
#     ويعرضه بـ /docs.
#   - llm_engine: المحرك اللي يتصل بخادم vLLM.
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.engine import llm_engine


# الشرح: الراوتر. prefix="/chat" يعني كل إيند بوينت هنا يبدأ بـ /chat،
# و tags تجمعهم تحت عنوان واحد بصفحة /docs.
router = APIRouter(prefix="/chat", tags=["chat"])


# الشرح: شكل الطلب والرد.
#   - message: رسالة المستخدم الحالية (إجبارية).
#   - system_prompt: تعليمات اختيارية تحدد شخصية/سلوك الموديل.
#   - history: رسائل سابقة اختيارية بصيغة OpenAI حتى الموديل يتذكر المحادثة،
#     مثل [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}].
#   - max_tokens: طول الرد (None = الافتراضي من config.py).
class ChatRequest(BaseModel):
    message: str
    system_prompt: Optional[str] = None
    history: List[dict] = []
    max_tokens: Optional[int] = None

class ChatResponse(BaseModel):
    reply: str


# الشرح: الإيند بوينت نفسه — POST /chat.
#   1) إذا vLLM بعده يحمّل الموديل نرجع 503 (خدمة غير متاحة مؤقتاً) بدل خطأ غامض.
#   2) نبني قائمة messages بالترتيب: system (إن وجد) → السجل → رسالة المستخدم.
#   3) نرسلها للموديل عبر llm_engine.generate ونرجع النص.
#   4) أي فشل بالاتصال بـ vLLM يرجع 502 (مشكلة بالخادم اللي خلفنا).
@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not llm_engine.ready:
        raise HTTPException(status_code=503, detail="Model is still loading")

    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    messages.extend(req.history)
    messages.append({"role": "user", "content": req.message})

    try:
        reply = await llm_engine.generate(messages, max_tokens=req.max_tokens)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"vLLM error: {e}")
    return ChatResponse(reply=reply)
