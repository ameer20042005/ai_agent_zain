# -*- coding: utf-8 -*-
"""تطبيق FastAPI الأساسي: تشغيل/إيقاف عميل vLLM + صفحة index + فحص الحالة.

الإيند بوينتات الخاصة بيك تضيفها أنت (انظر القسم الأخير بالملف).
"""

# الشرح: الاستيرادات.
#   - asynccontextmanager: لكتابة دالة lifespan (كود يشتغل عند الإقلاع والإغلاق).
#   - Path: لتحديد مكان مجلد static بغض النظر من وين شغّلت السيرفر.
#   - FileResponse: لإرجاع ملف index.html كما هو.
#   - StaticFiles: لتقديم باقي ملفات static (مثل أنميشن Lottie) تحت /static.
#   - llm_engine / settings: المحرك والإعدادات من ملفاتنا.
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.engine import llm_engine
from app.wallet import db as wallet_db
from app.wallet.api import wallet_app


# الشرح: lifespan — كل شي قبل yield يشتغل مرة وحدة عند إقلاع السيرفر،
# وكل شي بعده يشتغل عند الإغلاق. نستعملها حتى:
#   - نفتح اتصال vLLM ونبدأ فحص الجاهزية (llm_engine.start).
#   - نسكّر الاتصال بشكل نظيف عند الإيقاف (llm_engine.shutdown).
@asynccontextmanager
async def lifespan(app: FastAPI):
    wallet_db.ensure(settings.wallet_db_path, settings.wallet_seed_path)
    await llm_engine.start()
    try:
        yield
    finally:
        await llm_engine.shutdown()


# الشرح: إنشاء التطبيق وربطه بالـ lifespan، ثم تفعيل CORS حتى أي واجهة
# (متصفح/تطبيق من دومين ثاني) تقدر تتصل بالـ API. قيّد allow_origins
# لاحقاً بالإنتاج بدل "*".
app = FastAPI(title="AI Agent Zain — Bill Pay & Transfer Agent", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# الشرح: المحفظة الوهمية تطبيق FastAPI مستقل مركّب تحت /wallet (توثيقها
# بـ /wallet/docs). الوكيل يكلّمها عبر HTTP مثل أي نظام خارجي.
app.mount("/wallet", wallet_app)


# الشرح: الصفحة الرئيسية "/" — ترجع static/index.html (صفحة بسيطة
# تعرض حالة السيرفر والموديل). include_in_schema=False حتى ما تظهر بـ /docs.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC_DIR / "index.html")


# الشرح: باقي ملفات مجلد static (مثل "Live chatbot.json" — أنميشن زين بشاشة
# الترحيب) تنخدم تحت /static/... كما هي. الصفحة تطلبها بعنوان نسبي من نفس السيرفر.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# الشرح: فحص الحالة — صفحة index تستدعيه كل شوي:
#   - /health: هل FastAPI نفسه شغّال؟ (يرجع دائماً healthy إذا السيرفر حي)
#   - /status: هل الموديل جاهز؟ + اسمه وعنوان vLLM. model_ready يصير true
#     فقط بعد ما vLLM يكمّل تنزيل وتحميل الأوزان.
@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/status")
def status():
    return {
        "model": settings.model_name,
        "vllm_base_url": settings.vllm_base_url,
        "model_ready": llm_engine.ready,
    }


# ---------------------------------------------------------------------------
# الإيند بوينتات — موجودة بفولدر app/endpoints/ (لا تكتبها هنا).
# ---------------------------------------------------------------------------
# الشرح: نربط كل الراوترات المسجّلة بـ app/endpoints/__init__.py بالتطبيق.
# لإضافة إيند بوينت جديد: اعمل ملف بالفولدر وضيفه لـ all_routers هناك —
# هذا الملف ما يحتاج تعديل.
from app.endpoints import all_routers

for router in all_routers:
    app.include_router(router)
