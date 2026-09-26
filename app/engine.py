# -*- coding: utf-8 -*-
"""عميل vLLM — الباك اند ما يحمّل الموديل بنفسه، بل يتصل بخادم vLLM منفصل.

    ┌─────────────────────┐  HTTP   ┌──────────────────────────────┐
    │ FastAPI (منفذ 8000) │ ──────► │ vLLM serve (منفذ 18001)      │
    │ الإيند بوينتات مالتك │         │ الموديل المحدد في config.py   │
    └─────────────────────┘         └──────────────────────────────┘

vLLM يطبّق قالب المحادثة (chat template) بجهة الخادم، فنرسل له messages
مباشرة بصيغة OpenAI. محلياً بدون خادم vLLM يبقى `ready = False`.
"""

# الشرح: الاستيرادات.
#   - asyncio: لتشغيل فاحص الجاهزية بالخلفية بدون ما يوقف FastAPI.
#   - logging: لطباعة متى جهز vLLM ومتى وقف.
#   - httpx: عميل HTTP غير متزامن (async) نتصل بيه بخادم vLLM.
#   - settings: عنوان الخادم واسم الموديل من app/config.py.
import asyncio
import logging
from typing import Dict, List, Optional, Union

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# الشرح: ثوابت بسيطة:
#   - Message: رسالة بصيغة OpenAI مثل {"role": "user", "content": "..."}.
#   - _READY_POLL_SECONDS: كل كم ثانية نفحص إذا vLLM صار جاهز.
#   - _REQUEST_TIMEOUT: أقصى وقت ننتظره لطلب توليد واحد.
Message = Dict[str, object]
_READY_POLL_SECONDS = 10
_REQUEST_TIMEOUT = 120.0


# الشرح: الكلاس الرئيسي. __init__ يجهّز الحالة فقط (بدون اتصال فعلي):
#   - _client: عميل httpx يُنشأ لاحقاً بـ start().
#   - _ready: هل خادم vLLM جاهز يستقبل طلبات؟
#   - _poller_task: مهمة الخلفية اللي تفحص الجاهزية.
# الخاصية ready تكشف الحالة للخارج (main.py يستخدمها بـ /status).
class LLMEngine:
    def __init__(self):
        self._base_url = settings.vllm_base_url
        self._client: Optional[httpx.AsyncClient] = None
        self._ready = False
        self._poller_task: Optional[asyncio.Task] = None

    @property
    def ready(self) -> bool:
        return self._ready

    # الشرح: دورة الحياة — start() تُستدعى عند إقلاع FastAPI.
    # تفتح عميل HTTP وتشغّل فاحص الجاهزية بالخلفية. ما ننتظر vLLM هنا
    # لأن تحميل الأوزان ياخذ دقائق، وما نريد FastAPI يتأخر بالإقلاع.
    # shutdown() تُستدعى عند الإغلاق: توقف الفاحص وتسكّر العميل.
    async def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=_REQUEST_TIMEOUT)
        self._poller_task = asyncio.create_task(self._readiness_poller())

    async def shutdown(self) -> None:
        if self._poller_task is not None:
            self._poller_task.cancel()
            try:
                await self._poller_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
        self._ready = False

    # الشرح: فحص الجاهزية.
    # _probe: طلب واحد لـ /v1/models — vLLM يرجع 200 فقط بعد ما يكمّل تحميل الموديل.
    # _readiness_poller: حلقة لا نهائية تفحص كل 10 ثواني، فتلتقط:
    #   1) لحظة جهوزية vLLM بعد إقلاعه المتأخر.
    #   2) لحظة سقوطه لاحقاً (فيرجع ready = False تلقائياً).
    async def _probe(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def _readiness_poller(self) -> None:
        while True:
            was_ready = self._ready
            self._ready = await self._probe()
            if self._ready and not was_ready:
                logger.info("✅ vLLM ready at %s", self._base_url)
            elif was_ready and not self._ready:
                logger.warning("⚠️ vLLM (%s) stopped responding", self._base_url)
            await asyncio.sleep(_READY_POLL_SECONDS)

    # الشرح: chat() — الدالة الأساسية اللي راح تستعملها بالإيند بوينتات مالتك.
    # ترسل messages لـ /v1/chat/completions وترجع رد vLLM كاملاً (dict بصيغة OpenAI).
    #   - max_tokens: طول الرد (الافتراضي من config).
    #   - temperature: 0.0 = رد حتمي (نفس السؤال → نفس الجواب).
    #   - **extra: أي حقول إضافية يدعمها vLLM تمررها كما هي، مثل:
    #       tools / tool_choice / response_format / stop / top_p
    #     هيچ تقدر تخصص الطلب من الإيند بوينت بدون تعديل هذا الملف.
    async def chat(
        self,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
        **extra,
    ) -> dict:
        if not self._ready:
            raise RuntimeError("vLLM server is not ready yet")
        body = {
            "model": settings.model_name,
            "messages": messages,
            "max_tokens": max_tokens or settings.max_new_tokens,
            "temperature": temperature,
            **extra,
        }
        # الشرح: إذا الإعدادات تطلب مستوى تفكير (مثل "none" مع LM Studio) نضيفه
        # للطلب. setdefault تخلي أي قيمة يمررها الإيند بوينت بنفسه هي الغالبة.
        if settings.llm_reasoning_effort:
            body.setdefault("reasoning_effort", settings.llm_reasoning_effort)
        resp = await self._client.post("/chat/completions", json=body)
        resp.raise_for_status()
        return resp.json()

    # الشرح: generate() — اختصار مريح فوق chat(): تعطيه نص أو messages
    # ويرجع لك نص الرد فقط بدل الـ dict الكامل.
    # إذا مررت نص خام، يتغلّف تلقائياً كرسالة user واحدة.
    async def generate(self, prompt: Union[str, List[Message]], **kwargs) -> str:
        if isinstance(prompt, str):
            prompt = [{"role": "user", "content": prompt}]
        data = await self.chat(prompt, **kwargs)
        return (data["choices"][0].get("message") or {}).get("content") or ""


# الشرح: نسخة واحدة مشتركة من المحرك — main.py يشغّلها بالـ lifespan،
# والإيند بوينتات مالتك تستوردها هيچ:
#   from app.engine import llm_engine
#   text = await llm_engine.generate("شلونك؟")
llm_engine = LLMEngine()
