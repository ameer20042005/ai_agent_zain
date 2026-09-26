# AI Agent Zain — Bill Pay & Transfer Agent

> **EN —** An agent that turns an Iraqi-Arabic request («ادفع قائمة الكهرباء», «دز 50 الف لأحمد») into a
> confirmed transaction against a mock wallet API. It asks instead of guessing, never executes without an
> explicit confirmation card, cannot double-pay on retries, and says honestly when a payment fails.
> Design, safety proofs, failure modes and model/data disclosure: **[docs/AGENT.md](docs/AGENT.md)**.
> Test-set results: **[eval/results/report.md](eval/results/report.md)** (88 cases, 0 unsafe executions).

وكيل دفع فواتير وتحويل فلوس باللهجة العراقية، مبني فوق باك اند **FastAPI + vLLM**: جملة وحدة (مكتوبة أو
محچية) → فهم الطلب → أسئلة توضيح إذا أكو غموض → بطاقة تأكيد → تنفيذ على محفظة وهمية (SQLite) → نتيجة صادقة.

| الوثيقة | المحتوى |
|---|---|
| [README.md](README.md) | نظرة عامة، البنية، التشغيل، الإعدادات، إضافة إيند بوينت |
| [docs/AGENT.md](docs/AGENT.md) | تصميم الوكيل، بطاقة التأكيد، منع الدفع المكرر، أنماط الفشل، الإفصاح (بالإنجليزي للحكّام) |
| [docs/API.md](docs/API.md) | مرجع الإيند بوينتات (الوكيل + المحفظة) مع أمثلة `curl` |
| [docs/DEPLOY.md](docs/DEPLOY.md) | النشر على RunPod خطوة بخطوة، أوامر التشغيل اليومية، المشاكل الشائعة |
| [eval/results/report.md](eval/results/report.md) | نتائج مجموعة الاختبار مع نص كل محادثة |

## تشغيل الوكيل بسرعة (بدون GPU)

```bash
python -m venv venv && venv/Scripts/activate      # Linux/Mac: source venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000                 # افتح http://localhost:8000/
python -m eval.run_eval                          # مجموعة الاختبار → eval/results/report.md
pytest -q                                        # الاختبارات
```

بدون GPU الوكيل يفهم الطلبات بالقواعد (الوضع الافتراضي `NLU_MODE=auto` يرجع للقواعد لما الموديل مو جاهز)؛
على سيرفر GPU (`bash start.sh`) يستعمل الموديل تلقائياً. ضمانات الأمان نفسها بالحالتين.

---

## البنية

```
┌──────────────────────────┐  HTTP   ┌──────────────────────────────┐
│ FastAPI  (منفذ 8000)      │ ──────► │ vLLM serve  (منفذ 18001)      │
│ صفحة index + إيند بوينتات  │         │ الموديل المحدد في config.py   │
│ مكشوف للخارج              │         │ داخلي فقط (127.0.0.1)         │
└──────────────────────────┘         └──────────────────────────────┘
```

- **vLLM** يحمّل الموديل على الـ GPU ويعرض واجهة متوافقة مع OpenAI (`/v1/chat/completions`). يدير الـ batching والـ KV cache داخلياً، فالطلبات المتزامنة تتقاسم الـ GPU تلقائياً.
- **FastAPI** ما يحمّل الموديل بنفسه — هو عميل HTTP خفيف لـ vLLM، وفوقه تبني إيند بوينتاتك.
- FastAPI يشتغل فوراً حتى لو vLLM بعده يحمّل الأوزان (يأخذ دقائق). فاحص بالخلفية يقلب `model_ready` إلى `true` أول ما يجهز الموديل.

## هيكل الملفات

```
ai_agent_zain/
├── start.sh                 سكربت الإقلاع: تنصيب → تنزيل الموديل → تشغيل vLLM → تشغيل FastAPI
├── requirements.txt         متطلبات التشغيل (vLLM يثبّته start.sh)
├── requirements-dev.txt     + pytest
├── app/
│   ├── config.py            كل الإعدادات: الموديل، المنافذ، المحفظة، وضع الفهم، مهلة التأكيد
│   ├── engine.py            عميل vLLM: فحص الجاهزية + chat() + generate()
│   ├── main.py              تطبيق FastAPI: الصفحة، /health، /status، تركيب المحفظة على /wallet
│   ├── agent/               الوكيل
│   │   ├── core.py          آلة الحالات: طلب → أسئلة → بطاقة تأكيد → تنفيذ → نتيجة
│   │   ├── nlu.py           فهم الطلب: الموديل (guided JSON) + القواعد كاحتياط
│   │   ├── amounts.py       استخراج المبالغ العراقية ("ربع مليون"، "خمس تلاف") بالكود
│   │   ├── resolver.py      مطابقة المستلم والفاتورة — المطابقة الأكيدة فقط، والباقي سؤال
│   │   ├── textnorm.py      تطبيع النص العربي/العراقي
│   │   ├── wallet_client.py عميل المحفظة: Idempotency-Key + إعادة محاولة + تسوية
│   │   └── messages.py      كل رسائل المستخدم (قوالب ثابتة بالعراقي)
│   ├── wallet/              المحفظة الوهمية
│   │   ├── api.py           واجهة HTTP (/wallet/...) + حقن الأعطال للاختبار
│   │   ├── service.py       قواعد العمل + التنفيذ بدون تكرار (idempotency)
│   │   └── db.py            جداول SQLite + تحميل البذرة
│   └── endpoints/
│       ├── __init__.py      قائمة all_routers — سجّل راوتراتك هنا
│       ├── agent.py         /agent/message، /agent/confirm، /agent/cancel
│       └── chat.py          إيند بوينت افتراضي: POST /chat
├── data/
│   ├── wallet_schema.json   المخطط اللي انولّدت منه بيانات المحفظة
│   ├── wallet_seed.json     بيانات الاختبار: مستخدمين، أسماء مكررة ومتشابهة، فواتير، أرصدة، سجل
│   └── test_requests.jsonl  مجموعة الاختبار (88 طلب بالعراقي)
├── eval/run_eval.py         مشغّل مجموعة الاختبار + التقرير
├── tests/                   اختبارات pytest (المبالغ، منع التكرار، مسار الموديل، المجموعة كاملة)
└── static/index.html        واجهة المحادثة + بطاقة التأكيد + لوحة المحفظة + أدوات العرض
```

### قاعدة البيانات

ملف SQLite وحد (`data/wallet.sqlite3`) ينبني تلقائياً من `data/wallet_seed.json` أول تشغيل. لإرجاعه
لحالته الأصلية: زر «إعادة بيانات المحفظة» بالصفحة، أو `curl -X POST localhost:8000/wallet/_admin/reset`.
المستخدم الافتراضي `u1` (رصيده 400,000 دينار) وعنده عمداً: اسمين «أحمد»، «علي حسين» و«علي حسن»،
«زينب» و«زينة»، جارين، صديق بدون محفظة، حساب موقوف، حسابين كهرباء (البيت والمحل)، وجهة إنترنت بصيانة.

---

## المتطلبات

| المكوّن | المطلوب |
|---|---|
| نظام التشغيل | Linux (مُختبر على Ubuntu 22.04). vLLM ما يشتغل على Windows مباشرة |
| GPU | NVIDIA بذاكرة كافية للموديل. الموديل الافتراضي (Gemma 4 12B بـ bf16) يحتاج ~24GB للأوزان + مساحة KV cache → **40GB+** (مثل A40 48GB) |
| القرص | **60GB+** (torch/vLLM ~10-15GB + الموديل ~24GB) |
| توكن Hugging Face | مطلوب إذا الموديل gated (مثل Gemma) أو المستودع خاص |

---

## التشغيل السريع (على سيرفر GPU)

```bash
# 1) نزّل المشروع
cd /workspace
git clone <REPO_URL> app
cd /workspace/app

# 2) توكن Hugging Face (يُقرأ من البيئة، ما ينكتب بالكود)
export HF_TOKEN=hf_xxx
export HF_HUB_ENABLE_HF_TRANSFER=0

# 3) شغّل
bash start.sh
```

`start.sh` يسوي كل شي بالترتيب، وكل خطوة تتخطى نفسها إذا لقت الشي موجود أصلاً:

1. يثبّت Python و pip (إذا الصورة خام).
2. يثبّت `requirements.txt`.
3. يثبّت **vLLM nightly** (فيه دعم Gemma 4) إذا vLLM مو موجود.
4. يقرأ الإعدادات من `app/config.py`.
5. **ينزّل الموديل** من Hugging Face للكاش (`~/.cache/huggingface`) — التشغيلات اللاحقة ما تعيد التنزيل.
6. يطبّق إصلاحات بيئة CUDA.
7. يقتل أي تشغيلة سابقة عالقة.
8. يشغّل خادم vLLM بالخلفية (لوقه بـ `/tmp/vllm_boot.log`).
9. يتأكد إن vLLM ما مات فوراً.
10. يشغّل FastAPI على المنفذ 8000.

بعدها افتح `http://<السيرفر>:8000/` — الصفحة تعرض "يتحمّل..." لحد ما الموديل يجهز، بعدين "جاهز".

> للتشغيل الدائم (يضل شغال بعد غلق الطرفية) استخدم tmux — التفاصيل بـ [docs/DEPLOY.md](docs/DEPLOY.md).

### تشغيل FastAPI وحده (محلياً، بدون GPU)

مفيد لتطوير الإيند بوينتات على جهازك (حتى Windows) بدون موديل:

```bash
python -m venv venv
venv/Scripts/activate        # Windows  (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

الصفحة والإيند بوينتات تشتغل، بس `model_ready` يبقى `false` و `POST /chat` يرجع `503` لأن ماكو خادم vLLM.
إذا عندك خادم vLLM شغّال بمكان ثاني (أو أي خادم متوافق مع OpenAI)، غيّر `vllm_base_url` بـ `app/config.py` ليشير له.

---

## الإعدادات — `app/config.py`

كل الإعدادات بمكان واحد. `start.sh` يقرأ نفس القيم، فما تحتاج تعدّل بمكانين.

| الحقل | الافتراضي | الوصف |
|---|---|---|
| `model_name` | `ameer4wisam/gemma-iraqi-10k-merged` | مستودع الموديل على Hugging Face. نفس الاسم يُرسل بحقل `model` بكل طلب |
| `vllm_base_url` | `http://127.0.0.1:18001/v1` | عنوان خادم vLLM. لازم المنفذ يطابق `vllm_port` |
| `hf_token` | من متغير البيئة `HF_TOKEN` | توكن Hugging Face — لا تكتبه بالكود |
| `gpu_memory_utilization` | `0.85` | نسبة VRAM اللي يحجزها vLLM (أوزان + KV cache) |
| `max_model_len` | `8192` | أقصى طول سياق (برومبت + رد). أقصر = طلبات متزامنة أكثر |
| `max_num_seqs` | `64` | أقصى عدد طلبات يعالجها vLLM بنفس الوقت |
| `vllm_port` | `18001` | منفذ vLLM الداخلي |
| `api_port` | `8000` | منفذ FastAPI المكشوف |
| `max_new_tokens` | `512` | طول الرد الافتراضي إذا الطلب ما حدد `max_tokens` |
| `wallet_db_path` | `data/wallet.sqlite3` (أو `WALLET_DB_PATH`) | ملف قاعدة المحفظة الوهمية |
| `wallet_base_url` | فارغ (أو `WALLET_BASE_URL`) | فارغ = المحفظة المركّبة بنفس السيرفر؛ رابط = محفظة مستقلة عبر الشبكة |
| `default_user_id` | `u1` | مستخدم الجلسات إذا الطلب ما حدد `user_id` |
| `nlu_mode` | `auto` (أو `NLU_MODE`) | `auto` الموديل إذا جاهز وإلا القواعد · `llm` · `rules` |
| `confirmation_ttl_seconds` | `300` | مهلة بطاقة التأكيد |
| `wallet_retry_attempts` | `3` | محاولات التنفيذ (بنفس المفتاح) عند عطل الشبكة |

### تغيير الموديل

1. بدّل `model_name` بـ `app/config.py`.
2. إذا الموديل الجديد **مو** من عائلة Gemma 4، بدّل أو احذف سطري `--enable-auto-tool-choice` و `--tool-call-parser gemma4` بـ `start.sh` (الـ parser خاص بكل عائلة موديلات).
3. إذا الموديل مدعوم بإصدار vLLM المستقر، تقدر تبدّل خطوة التنصيب بـ `start.sh` إلى `pip install vllm` بدل nightly.
4. راجع `max_model_len` و `gpu_memory_utilization` حسب حجم الموديل وذاكرة الـ GPU.

---

## إضافة إيند بوينت جديد

الإيند بوينتات كلها بفولدر [app/endpoints/](app/endpoints/). `main.py` يربطها تلقائياً — ما تحتاج تعدّله.

**1) اعمل ملف جديد** مثلاً `app/endpoints/summarize.py`:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.engine import llm_engine

router = APIRouter(prefix="/summarize", tags=["summarize"])


class SummarizeRequest(BaseModel):
    text: str


@router.post("")
async def summarize(req: SummarizeRequest):
    if not llm_engine.ready:
        raise HTTPException(status_code=503, detail="Model is still loading")
    reply = await llm_engine.generate([
        {"role": "system", "content": "لخّص النص التالي بجملتين."},
        {"role": "user", "content": req.text},
    ])
    return {"summary": reply}
```

**2) سجّله** بـ [app/endpoints/\_\_init\_\_.py](app/endpoints/__init__.py):

```python
from app.endpoints.chat import router as chat_router
from app.endpoints.summarize import router as summarize_router

all_routers = [
    chat_router,
    summarize_router,
]
```

يظهر تلقائياً بـ `/docs`.

### دوال المحرك (`llm_engine`)

| الدالة | ترجع | متى تستعملها |
|---|---|---|
| `await llm_engine.generate(prompt, **kwargs)` | نص الرد فقط (`str`) | أغلب الحالات. `prompt` نص خام أو قائمة messages |
| `await llm_engine.chat(messages, max_tokens=None, temperature=0.0, **extra)` | رد vLLM كامل (dict بصيغة OpenAI) | لما تحتاج `tool_calls` أو `usage` أو `finish_reason` |
| `llm_engine.ready` | `bool` | هل الموديل جاهز؟ افحصه قبل الاستدعاء وارجع 503 إذا لا |

أي حقل إضافي تمرره لـ `chat()` / `generate()` يوصل لـ vLLM كما هو، مثلاً:

```python
# JSON مقيّد بمخطط (vLLM يفرض المخطط أثناء التوليد فعلياً)
await llm_engine.chat(messages, response_format={
    "type": "json_schema",
    "json_schema": {"name": "out", "schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
})

# tool calling
await llm_engine.chat(messages, tools=[...], tool_choice="auto")

# إيقاف التوليد عند نص معيّن / توليد عشوائي أكثر
await llm_engine.generate(messages, stop=["###"], temperature=0.7)
```
