# -*- coding: utf-8 -*-
"""إعدادات التطبيق الثابتة — المصدر الوحيد لاسم الموديل والمنافذ وحدود vLLM.

start.sh يقرأ هذه القيم نفسها (بدل ما نكررها بالسكربت) حتى يبقى FastAPI
وخادم vLLM متطابقين دائماً.
"""

# الشرح: الاستيرادات اللازمة للإعدادات.
#   - os: لقراءة HF_TOKEN من متغيرات البيئة (السر ما ينكتب بالكود).
#   - dataclass/field: نعرّف الإعدادات ككلاس ثابت (frozen) حتى ما يتغير بالغلط وقت التشغيل.
#   - Optional: لأن التوكن ممكن يكون غير موجود محلياً.
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent


# الشرح: كلاس الإعدادات. كل حقل هنا يُستخدم بمكانين:
#   1) app/engine.py → يعرف عنوان خادم vLLM واسم الموديل اللي يرسله بالطلبات.
#   2) start.sh      → يقرأ نفس القيم ويمررها كأعلام لأمر `vllm serve`.
# frozen=True يعني الكائن للقراءة فقط بعد إنشائه.
@dataclass(frozen=True)
class Settings:
    # اسم مستودع الموديل على Hugging Face. vLLM ينزّله تلقائياً بأول تشغيل
    # (يُخزَّن بكاش HF ~/.cache/huggingface) ثم يحمّله على الـ GPU.
    # نفس الاسم يُرسل بحقل "model" بكل طلب /v1/chat/completions.
    # MODEL_NAME من البيئة يبدّله بدون تعديل الكود — مثلاً اسم الموديل المحمّل
    # بـ LM Studio عند التشغيل المحلي (start_local.ps1).
    model_name: str = field(default_factory=lambda: os.environ.get(
        "MODEL_NAME", "lmstudio-community/gemma-4-E4B-it-GGUF"))

    # عنوان خادم vLLM المتوافق مع OpenAI. الباك اند مجرد عميل HTTP له.
    # لازم المنفذ هنا يطابق vllm_port بالأسفل.
    # VLLM_BASE_URL من البيئة يوجّهه لأي خادم متوافق مع OpenAI، مثل LM Studio
    # (http://localhost:1234/v1).
    vllm_base_url: str = field(default_factory=lambda: os.environ.get(
        "VLLM_BASE_URL", "http://127.0.0.1:18001/v1"))

    # الشرح: موديلات Gemma 4 أحياناً "تفكر" (reasoning) قبل الرد، والتفكير ياكل
    # من max_tokens فيقطع الرد أو وسائط الأداة (JSON) بالنص. قيمة "none" تطفي التفكير بـ LM Studio.
    # فارغ = ما نرسل الحقل أصلاً (الافتراضي، حتى vLLM ما يرفض قيمة ما يعرفها).
    llm_reasoning_effort: str = field(default_factory=lambda: os.environ.get(
        "LLM_REASONING_EFFORT", ""))

    # توكن Hugging Face — مطلوب إذا الموديل gated (مثل Gemma) أو المستودع خاص.
    # يُقرأ من البيئة فقط حتى ما ينرفع سر حقيقي مع الكود.
    hf_token: Optional[str] = field(default_factory=lambda: os.environ.get("HF_TOKEN"))

    # إعدادات خادم vLLM (start.sh يمررها لـ vllm serve):
    # نسبة الـ VRAM اللي يحجزها vLLM للأوزان + KV cache.
    gpu_memory_utilization: float = 0.85
    # أقصى طول سياق (برومبت + رد). أقصر = طلبات متزامنة أكثر.
    max_model_len: int = 8192
    # أقصى عدد طلبات يعالجها vLLM بنفس الوقت.
    max_num_seqs: int = 64

    # المنافذ: vLLM داخلي فقط، و FastAPI هو المكشوف للخارج.
    vllm_port: int = 18001
    api_port: int = 8000

    # الطول الافتراضي للرد إذا الطلب ما حدد max_tokens.
    max_new_tokens: int = 512

    # ── وكيل الدفع والتحويل ─────────────────────────────────────────────────
    # قاعدة بيانات المحفظة الوهمية (SQLite) وملف البذرة اللي تنبني منه.
    # WALLET_DB_PATH من البيئة يسمح للاختبارات تستعمل قاعدة منفصلة مؤقتة.
    wallet_db_path: Path = field(default_factory=lambda: Path(
        os.environ.get("WALLET_DB_PATH", str(_ROOT / "data" / "wallet.sqlite3"))))
    wallet_seed_path: Path = _ROOT / "data" / "wallet_seed.json"
    # الوكيل يكلّم المحفظة عبر HTTP. فارغ = المحفظة المركّبة بنفس العملية (عبر
    # طبقة ASGI بدون منفذ شبكة). ضع رابطاً (مثل http://host:8100) لمحفظة مستقلة.
    wallet_base_url: str = field(default_factory=lambda: os.environ.get("WALLET_BASE_URL", ""))
    # المستخدم الافتراضي للجلسات (المحفظة الوهمية فيها أكثر من مستخدم).
    default_user_id: str = "u1"

    # بطاقة التأكيد تنتهي صلاحيتها بعد هذي المدة — تأكيد قديم ما ينفّذ.
    confirmation_ttl_seconds: int = 300
    # كم مرة نعيد طلب الدفع (بنفس مفتاح الـ idempotency) عند انقطاع الشبكة.
    wallet_retry_attempts: int = 3


# الشرح: نسخة واحدة مشتركة من الإعدادات يستوردها كل الكود:
#   from app.config import settings
settings = Settings()
