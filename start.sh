#!/bin/bash
# سكربت الإقلاع: تنصيب المتطلبات + vLLM، تنزيل الموديل، تشغيل خادم vLLM
# (منفذ داخلي 18001 بالخلفية)، ثم تشغيل FastAPI (منفذ 8000، المكشوف للخارج).
#
# مصمَّم لـ Ubuntu 22.04 خام (مثل Pod على RunPod): كل خطوة تتخطى نفسها
# إذا لقت الأداة مثبّتة أصلاً، فيشتغل كذلك على صورة فيها Python/vLLM مسبقاً.
#
# التشغيل:  bash start.sh

set -e
cd "$(dirname "$0")"

# ── 1) تجهيز Python و pip ─────────────────────────────────────────────────────
# الشرح: صورة Ubuntu الخام ما بيها Python ولا pip. نثبّتهم مع build-essential
# (مترجم C تحتاجه بعض الحزم وقت التثبيت) و git. الفحص بالبداية يخلي الخطوة
# تتخطى نفسها إذا Python و pip موجودين أصلاً.
if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then
    echo "==> Installing Python/pip (bare Ubuntu image)..."
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python3-pip python3-dev build-essential git
fi

# ── 2) متطلبات FastAPI ────────────────────────────────────────────────────────
# الشرح: fastapi + uvicorn + httpx من requirements.txt (خفيفة، بدون torch).
python3 -m pip install --no-cache-dir -q -r requirements.txt

# ── 3) تنصيب vLLM ─────────────────────────────────────────────────────────────
# الشرح: نفحص إذا vLLM موجود بمحاولة استيراده. إذا مو موجود نثبّت نسخة
# nightly لأن دعم معمارية Gemma 4 موجود بيها (قد لا يكون بالإصدار المستقر).
# vLLM يجيب معه torch المتوافق مع CUDA تلقائياً.
# --force-reinstall: يجبر pip يعيد تثبيت كل الشجرة بنفس نسخة CUDA (cu129)،
# وإلا حزم قديمة مبنية على CUDA ثاني تبقى وتسبب أخطاء "libcudart.so".
# ملاحظة: إذا غيّرت الموديل لموديل مدعوم بالإصدار المستقر، يكفيك: pip install vllm
if ! python3 -c "import vllm.entrypoints.openai.api_server" 2>/dev/null; then
    echo "==> vLLM not found — installing the nightly wheel..."
    python3 -m pip install -U --force-reinstall vllm torch torchvision --pre \
        --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
        --extra-index-url https://download.pytorch.org/whl/cu129
fi

# ── 4) قراءة الإعدادات من app/config.py ──────────────────────────────────────
# الشرح: بدل ما نكتب اسم الموديل والمنافذ مرتين (بالبايثون وبالسكربت)،
# نطلب من بايثون يطبعها سطر سطر، و mapfile يحطها بمصفوفة. هيچ config.py
# يبقى المصدر الوحيد للقيم.
mapfile -t CFG < <(python3 -c '
from app.config import settings
for v in (settings.model_name, settings.vllm_port, settings.api_port,
          settings.max_model_len, settings.gpu_memory_utilization,
          settings.max_num_seqs, settings.hf_token or ""):
    print(v)
')
MODEL_NAME="${CFG[0]}"
VLLM_PORT="${CFG[1]}"
API_PORT="${CFG[2]}"
MAX_MODEL_LEN="${CFG[3]}"
GPU_MEMORY_UTILIZATION="${CFG[4]}"
MAX_NUM_SEQS="${CFG[5]}"
export HF_TOKEN="${CFG[6]}"
VLLM_LOG="/tmp/vllm_boot.log"

# ── 5) تنزيل الموديل ─────────────────────────────────────────────────────────
# الشرح: مستودع GGUF الافتراضي يحتوي عدة نسخ، لذلك إضافة vLLM GGUF
# تختار Q4_K_M فقط وتنزّلها عند تشغيل الخادم. الموديلات الأخرى تستعمل
# تنزيل snapshot المعتاد قبل الإقلاع. الملفات تبقى في كاش Hugging Face.
VLLM_MODEL="${MODEL_NAME}"
VLLM_EXTRA_ARGS=()
if [ "${MODEL_NAME}" = "lmstudio-community/gemma-4-E4B-it-GGUF" ]; then
    # The GGUF plugin selects and downloads only Q4_K_M (and its projector).
    if ! python3 -c "import vllm_gguf_plugin" 2>/dev/null; then
        echo "==> Installing vLLM GGUF plugin..."
        python3 -m pip install -q 'setuptools>=77,<81' wheel ninja
        python3 -m pip install --no-build-isolation \
            'git+https://github.com/vllm-project/vllm-gguf-plugin.git'
    fi
    VLLM_MODEL="${MODEL_NAME}:Q4_K_M"
    VLLM_EXTRA_ARGS=(--tokenizer google/gemma-4-E4B-it --served-model-name "${MODEL_NAME}")
    echo "==> vLLM will download ${VLLM_MODEL} to the Hugging Face cache..."
else
    echo "==> Downloading model ${MODEL_NAME} (skipped if already cached)..."
    MODEL_NAME="${MODEL_NAME}" python3 -c '
import os
from huggingface_hub import snapshot_download
snapshot_download(os.environ["MODEL_NAME"], token=os.environ.get("HF_TOKEN") or None)
'
fi

# ── 6) إصلاحات بيئة CUDA ─────────────────────────────────────────────────────
# الشرح: flashinfer (يستعمله vLLM للـ sampling) يحتاج nvcc لتجميع كيرنلات
# وقت التشغيل، ومو موجود بأغلب الصور → ينهار المحرك متأخراً. نعطّله ونستعمل
# المسار العادي المبني بـ PyTorch.
export VLLM_USE_FLASHINFER_SAMPLER=0

# الشرح: مكتبات CUDA runtime أحياناً تنثبّت كحزم pip (nvidia-*) بدون ما
# يُضاف مسارها لـ LD_LIBRARY_PATH، فيظهر خطأ "libcudart.so: cannot open
# shared object file" رغم أن الملف موجود. نسأل بايثون وين حزمة nvidia
# ونضيف كل مجلدات lib بيها للمسار.
_fix_cuda_lib_path() {
    local nvidia_pkg_dir dirs
    nvidia_pkg_dir=$(python3 -c "import nvidia, os; print(os.path.dirname(nvidia.__path__[0]))" 2>/dev/null)
    [ -z "${nvidia_pkg_dir}" ] && return
    dirs=$(find "${nvidia_pkg_dir}/nvidia" -maxdepth 2 -type d -name lib 2>/dev/null | paste -sd: -)
    if [ -n "${dirs}" ]; then
        export LD_LIBRARY_PATH="${dirs}:${LD_LIBRARY_PATH}"
        echo "==> Added CUDA library paths to LD_LIBRARY_PATH"
    fi
}
_fix_cuda_lib_path

# ── 7) تنظيف تشغيلة سابقة ────────────────────────────────────────────────────
# الشرح: إذا شغّلت السكربت مرة ثانية بنفس الجلسة، عمليات vLLM/uvicorn القديمة
# تبقى ماسكة المنافذ والـ GPU. نقتلها قبل ما نبدأ.
pkill -9 -f "vllm.entrypoints.openai.api_server" 2>/dev/null || true
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1

# ── 8) تشغيل خادم vLLM ───────────────────────────────────────────────────────
# الشرح: يشغّل خادم OpenAI-متوافق بالخلفية (&) ويكتب اللوق بملف.
#   --host 127.0.0.1           : داخلي فقط، الوصول من الخارج يمر عبر FastAPI.
#   --max-model-len             : أقصى طول سياق (من config.py).
#   --gpu-memory-utilization    : نسبة VRAM المحجوزة.
#   --max-num-seqs              : أقصى طلبات متزامنة.
#   --enable-prefix-caching     : يعيد استعمال حسابات البادئة المشتركة (مثل system prompt) بين الطلبات — تسريع كبير.
#   --enable-auto-tool-choice / --tool-call-parser gemma4 : تفعيل tool calling لموديلات Gemma 4.
#       ⚠️ الوكيل كله يشتغل بالأدوات، فلا تحذف السطرين. إذا غيّرت الموديل لعائلة ثانية، بدّل الـ parser.
# $! يحفظ رقم العملية (PID) حتى نراقبها.
echo "==> Starting vLLM on port ${VLLM_PORT} (model: ${MODEL_NAME})..."
vllm serve "${VLLM_MODEL}" \
    "${VLLM_EXTRA_ARGS[@]}" \
    --host 127.0.0.1 \
    --port "${VLLM_PORT}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --max-num-seqs "${MAX_NUM_SEQS}" \
    --enable-prefix-caching \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    > "${VLLM_LOG}" 2>&1 &
VLLM_PID=$!

# ── 9) التأكد أن vLLM ما مات فوراً ───────────────────────────────────────────
# الشرح: ننتظر 8 ثواني ونفحص إذا العملية بعدها حيّة (kill -0 ما يقتل، بس يفحص).
# إذا ماتت (خطأ استيراد، CUDA، معمارية غير مدعومة) نطبع آخر اللوق ونوقف.
# تحميل الأوزان نفسه يكمّل بالخلفية لدقائق بعد هذا الفحص.
# trap يقتل vLLM إذا السكربت (أو uvicorn) انطفى، حتى ما يبقى ماسك الـ GPU.
sleep 8
if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "🛑 vLLM died immediately — boot log:"
    tail -n 40 "${VLLM_LOG}"
    exit 1
fi
echo "==> vLLM running (PID ${VLLM_PID}), loading weights — watch ${VLLM_LOG}"
trap 'kill "${VLLM_PID}" 2>/dev/null || true' EXIT

# ── 10) تشغيل FastAPI ────────────────────────────────────────────────────────
# الشرح: uvicorn يشغّل app/main.py على 0.0.0.0 (مكشوف للخارج).
# FastAPI يشتغل فوراً، وصفحة index تعرض "الموديل يتحمّل" لحد ما vLLM يجهز.
echo "==> Starting FastAPI on port ${API_PORT}..."
uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1
