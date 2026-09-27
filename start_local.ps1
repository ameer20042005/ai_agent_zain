# سكربت التشغيل المحلي على Windows: FastAPI فوق LM Studio بدل vLLM.
#
# vLLM ما يشتغل على Windows، و LM Studio يعرض نفس واجهة OpenAI
# (/v1/models و /v1/chat/completions)، فالمشروع يتصل بيه بدون تعديل الكود —
# بس نغيّر العنوان واسم الموديل عبر متغيرات البيئة.
#
# قبل التشغيل: افتح LM Studio → Developer → حمّل الموديل → Start Server.
# التشغيل:     powershell -ExecutionPolicy Bypass -File start_local.ps1
# مع خيارات:   powershell -ExecutionPolicy Bypass -File start_local.ps1 -Model gemma-4-e4b-it -Port 8000

# الشرح: خيارات السكربت مع قيم افتراضية تطابق LM Studio:
#   -BaseUrl : عنوان خادم LM Studio (منفذه الافتراضي 1234).
#   -Model   : الـ identifier اللي يظهر بـ LM Studio (مو اسم مستودع Hugging Face).
#   -Port    : منفذ FastAPI اللي تفتح عليه صفحة index.
param(
    [string]$BaseUrl = "http://localhost:1234/v1",
    [string]$Model = "gemma-4-e4b-it",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── 1) البيئة الافتراضية ─────────────────────────────────────────────────────
# الشرح: نستعمل python.exe من venv المشروع مباشرة (بدون activate). إذا venv
# مو موجود ننشئه ونثبّت المتطلبات مرة وحدة، والتشغيلات اللاحقة تتخطى الخطوة.
$python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "==> Creating venv and installing requirements..."
    python -m venv venv
    & $python -m pip install -q -r requirements-dev.txt
}

# ── 2) متغيرات البيئة ────────────────────────────────────────────────────────
# الشرح: app/config.py يقرأ هذه القيم بدل الافتراضية مالت vLLM:
#   VLLM_BASE_URL        : وين خادم الموديل (هنا LM Studio).
#   MODEL_NAME           : الاسم اللي يُرسل بحقل "model" بكل طلب.
#   LLM_REASONING_EFFORT : "none" يطفي تفكير Gemma 4 حتى ما ياكل max_tokens
#                          ويقطع الرد أو وسائط الأداة (JSON).
# المتغيرات تخص هذي الجلسة فقط، وتنمسح لما تسكّر الطرفية.
$env:VLLM_BASE_URL = $BaseUrl
$env:MODEL_NAME = $Model
$env:LLM_REASONING_EFFORT = "none"
$env:PYTHONIOENCODING = "utf-8"

# ── 3) فحص LM Studio ─────────────────────────────────────────────────────────
# الشرح: نسأل /models حتى نتأكد إن خادم LM Studio شغّال وإن الموديل المطلوب
# موجود عنده. إذا مو شغّال ما نوقف: FastAPI يشتغل والوكيل يرد "الموديل مو
# شغّال" لحد ما الموديل يجهز (فاحص الجاهزية بـ engine.py يلتقطه تلقائياً).
try {
    $models = Invoke-RestMethod -Uri "$BaseUrl/models" -TimeoutSec 5
    $ids = @($models.data | ForEach-Object { $_.id })
    Write-Host "==> LM Studio models: $($ids -join ', ')"
    if ($ids -notcontains $Model) {
        Write-Warning "Model '$Model' is not in LM Studio - load it, or pass -Model <id>"
    }
} catch {
    Write-Warning "LM Studio is not reachable at $BaseUrl - start its server (Developer tab). The agent replies 'model offline' until then."
}

# ── 4) تشغيل FastAPI ─────────────────────────────────────────────────────────
# الشرح: 127.0.0.1 = جهازك فقط. افتح http://localhost:8000/ لصفحة الاختبار
# و http://localhost:8000/docs لتوثيق الـ API. Ctrl+C للإيقاف.
Write-Host "==> Starting FastAPI on http://localhost:$Port/ (model: $Model via $BaseUrl)"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
