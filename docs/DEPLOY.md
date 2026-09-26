# النشر على RunPod

دليل خطوة بخطوة لرفع المشروع على **Pod بصورة Ubuntu 22.04 خام** — ما تحتاج تبني أي صورة Docker: `start.sh` يثبّت كل شي لازم من الصفر بأول إقلاع.
يشتغل بنفس الطريقة على أي سيرفر Linux فيه GPU من NVIDIA (مو بس RunPod) — تخطّى خطوات لوحة RunPod وابدأ من الخطوة 3.

---

## قبل البدء

### 1. توكن Hugging Face

مطلوب إذا الموديل gated (مثل Gemma) أو المستودع خاص:

1. افتح صفحة الموديل الأساس على Hugging Face واضغط **Agree and access repository** (للموديلات gated).
2. تأكد إن حسابك يقدر يفتح مستودع الموديل المحدد بـ `model_name` في `app/config.py`.
3. من https://huggingface.co/settings/tokens ولّد **Access Token** بصلاحية **Read**.

بدون توكن صحيح، خطوة تنزيل الموديل تفشل بخطأ `401` / `403`.

### 2. اختيار الـ GPU

| الموديل | الأوزان (bf16) | GPU موصى به |
|---|---|---|
| 12B (الافتراضي) | ~24GB | **40GB+** — A40 (48GB) خيار ممتاز سعر/أداء |

الباقي من الذاكرة بعد الأوزان يروح لـ KV cache، وهو اللي يحدد عدد الطلبات المتزامنة.

---

## 🚀 التشغيل من الصفر

### 1) أنشئ الـ Pod

من لوحة RunPod: **Deploy** → **GPU Pod**:

| الحقل | القيمة |
|---|---|
| **GPU** | A40 (48GB) أو أي GPU بـ 40GB+ |
| **Container Image** | `ubuntu:22.04` |
| **Container Disk** | 60GB+ |
| **Expose HTTP Ports** | `8000` |
| **Environment Variables** (اختياري) | `HF_TOKEN` = توكنك — هيچ ما تحتاج تصدّره يدوياً كل مرة |

اضغط **Deploy** وانتظر لحد ما الحالة تصير **Running**.

### 2) افتح الطرفية

**Connect** → **Start Web Terminal** (أو SSH إذا فعّلته).

### 3) نزّل المشروع

```bash
apt-get update -qq && apt-get install -y -qq git tmux
cd /workspace
git clone <REPO_URL> app
cd /workspace/app
```

> بقية الدليل يفترض المسار `/workspace/app`. إذا استنسخت باسم ثاني بدّله بكل الأوامر.

### 4) التوكن

إذا ما ضفته كـ Environment Variable بالخطوة 1:

```bash
export HF_TOKEN=hf_xxx
```

### 5) شغّل (بـ tmux حتى يضل شغال بعد غلق الطرفية)

```bash
cd /workspace/app
export HF_HUB_ENABLE_HF_TRANSFER=0
tmux kill-session -t api 2>/dev/null
tmux new-session -d -s api 'bash start.sh > /tmp/api.log 2>&1'
```

- **`new-session -d`** تبدأ الجلسة منفصلة أصلاً، فما تحتاج اختصار `Ctrl+B` ثم `D` (أكثر خطوة تنكسر بالعادة).
- **`HF_HUB_ENABLE_HF_TRANSFER=0`**: بعض قوالب RunPod تضبطه على `1` بدون ما تكون حزمة `hf_transfer` مثبتة، فيفشل أي تنزيل من Hugging Face. تصفيره يرجّع التنزيل العادي.
- جلسة tmux ترث متغيرات البيئة من الطرفية اللي أنشأتها، فـ `HF_TOKEN` يوصل لـ `start.sh`.

### 6) راقب الإقلاع

```bash
tail -f /tmp/api.log          # خطوات start.sh + لوق FastAPI
tail -f /tmp/vllm_boot.log    # لوق vLLM نفسه (تحميل الأوزان)
```

`Ctrl+C` يوقف `tail` فقط، ما يمس السيرفر.

**أول تشغيل ياخذ وقت أطول** — تنصيب Python/pip + vLLM nightly + torch، ثم تنزيل الموديل (~24GB)، ثم تحميله على الـ GPU. التشغيلات اللاحقة أسرع بكثير (كل شي مثبّت ومنزّل).

الترتيب اللي راح تشوفه باللوق:

```
==> vLLM not found — installing the nightly wheel...        (أول مرة فقط)
==> Downloading model ... (skipped if already cached)
==> Starting vLLM on port 18001 ...
==> vLLM running (PID ...), loading weights — watch /tmp/vllm_boot.log
==> Starting FastAPI on port 8000...
...
✅ vLLM ready at http://127.0.0.1:18001/v1
```

### 7) تأكد إنه جاهز

```bash
curl -s http://127.0.0.1:8000/status
```

انتظر لحد ما يصير `"model_ready": true`.

### 8) جرّب أول رد

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"شلونك؟"}'
```

### 9) افتح من المتصفح

```
https://<POD_ID>-8000.proxy.runpod.net/        صفحة الحالة
https://<POD_ID>-8000.proxy.runpod.net/docs    توثيق الإيند بوينتات التفاعلي
```

تلقى `<POD_ID>` بزر **Connect** → **HTTP Service [Port 8000]**.

### 10) اغلق الطرفية

السيرفر يضل شغال داخل tmux. للتأكد: افتح طرفية جديدة وشغّل `tmux ls` و `curl -s http://127.0.0.1:8000/health`.

---

## ⚡ أوامر يومية (Pod شغّال أصلاً)

| تريد | الأمر |
|---|---|
| تتأكد إن السيرفر شغّال | `tmux ls` و `curl -s http://127.0.0.1:8000/health` |
| تتأكد إن الموديل جاهز | `curl -s http://127.0.0.1:8000/status` |
| تشوف اللوق حي | `tail -f /tmp/api.log` |
| تدخل الجلسة | `tmux attach -t api` (تطلع بدون إيقاف: `Ctrl+B` ثم `D`، بالتتابع مو سوية) |
| توقف السيرفر | `tmux kill-session -t api` |
| تعيد التشغيل | `cd /workspace/app && tmux kill-session -t api 2>/dev/null; tmux new-session -d -s api 'bash start.sh > /tmp/api.log 2>&1'` |
| تشوف استهلاك الـ GPU | `nvidia-smi` |

> ⚠️ لا تضغط `Ctrl+C` ولا تكتب `exit` وأنت داخل جلسة tmux — ينهون السيرفر.

> **حدود tmux**: يحمي من غلق الطرفية فقط، **مو** من إيقاف الـ Pod. إذا أوقفت الـ Pod وشغّلته من جديد لازم تعيد أمر الخطوة 5.
> الموديل المنزّل يبقى بالكاش إذا كان على قرص دائم؛ على Container Disk قد ينمسح مع إعادة إنشاء الـ Pod.

### تحديث الكود

```bash
cd /workspace/app
git pull
tmux kill-session -t api 2>/dev/null
tmux new-session -d -s api 'bash start.sh > /tmp/api.log 2>&1'
```

`start.sh` يقتل أي عملية vLLM / uvicorn قديمة بنفسه قبل ما يبدأ، فما يصير تعارض منافذ.

---

## مشاكل شائعة

| المشكلة | السبب المحتمل | الحل |
|---|---|---|
| خطأ `401` / `403` بخطوة تنزيل الموديل | `HF_TOKEN` مفقود أو غلط، أو ما قبلت ترخيص الموديل الـ gated | راجع [توكن Hugging Face](#1-توكن-hugging-face). تأكد: `echo $HF_TOKEN` |
| أي تنزيل يفشل بخطأ `hf_transfer` | القالب ضابط `HF_HUB_ENABLE_HF_TRANSFER=1` بدون الحزمة | `export HF_HUB_ENABLE_HF_TRANSFER=0` قبل التشغيل |
| `🛑 vLLM died immediately` | خطأ استيراد، CUDA، أو معمارية الموديل مو مدعومة | اقرأ آخر اللوق المطبوع. إذا ذكر `not supported` / `No module named` أعد تثبيت vLLM nightly (أمر الخطوة 3 بـ `start.sh`) |
| `libcudart.so: cannot open shared object file` | مسار مكتبات CUDA المثبّتة كحزم pip مو مضاف | `start.sh` يصلحه تلقائياً (`_fix_cuda_lib_path`). إذا استمر، تأكد إن vLLM و torch مثبّتين بنفس نسخة CUDA (أعد التثبيت بـ `--force-reinstall`) |
| `model_ready` يبقى `false` لفترة طويلة | vLLM بعده ينزّل/يحمّل الأوزان، أو انهار بعد الإقلاع | `tail -f /tmp/vllm_boot.log` — إذا شفت خطأ، هو السبب |
| نفاد ذاكرة GPU (`CUDA out of memory`) | الـ GPU صغير، أو `gpu_memory_utilization` / `max_model_len` / `max_num_seqs` عالية | GPU أكبر، أو قلّل القيم بـ `app/config.py` |
| `Address already in use` على 18001 | عملية ثانية ماسكة المنفذ | `ss -ltnp \| grep 18001` لمعرفة العملية. غيّر `vllm_port` **و** `vllm_base_url` بـ `config.py` معاً |
| السيرفر يموت أول ما تغلق الطرفية | ما اشتغل داخل tmux فعلياً | استعمل أمر الخطوة 5 (`tmux new-session -d`) |
| `bash start.sh` يطبع Nginx / Jupyter / «Pod is ready» | شغّلت سكربت إقلاع RunPod مو سكربت المشروع (مسار غلط) | `cd /workspace/app` أولاً |
| `POST /chat` يرجع `503` | الموديل بعده يتحمّل | انتظر `model_ready: true` |
| `POST /chat` يرجع `502` | vLLM رفض الطلب أو انهار (مثلاً الطلب أطول من `max_model_len`) | تفاصيل الخطأ بحقل `detail` بالرد، و `/tmp/vllm_boot.log` |
| أول طلب بطيء | طبيعي — تسخين الكيرنلات والكاش | الطلبات اللاحقة أسرع |
