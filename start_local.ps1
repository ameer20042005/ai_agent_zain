# سكربت التشغيل المحلي على Windows: FastAPI فوق LM Studio بدل vLLM.
#
# vLLM ما يشتغل على Windows، و LM Studio يعرض نفس واجهة OpenAI
# (/v1/models و /v1/chat/completions)، فالمشروع يتصل بيه بدون تعديل الكود —
# بس نغيّر العنوان واسم الموديل عبر متغيرات البيئة.
#
# قبل التشغيل: افتح LM Studio → Developer → حمّل أي موديل تريده → Start Server.
# التشغيل:     powershell -ExecutionPolicy Bypass -File start_local.ps1
# مع خيارات:   powershell -ExecutionPolicy Bypass -File start_local.ps1 -Model google/gemma-4-e4b -Port 8000

# الشرح: خيارات السكربت مع قيم افتراضية تطابق LM Studio:
#   -BaseUrl         : عنوان خادم LM Studio (منفذه الافتراضي 1234).
#   -Model           : فارغ = السكربت ياخذ الموديل المحمّل حالياً بـ LM Studio (حر، مو مربوط
#                      بموديل معيّن). تمرير identifier (مثل google/gemma-4-e4b) يفرضه بدل الاكتشاف.
#   -ReasoningEffort : قيمة reasoning_effort بكل طلب؛ "none" يطفي التفكير، و "" ما يرسل الحقل أصلاً.
#   -Port            : منفذ FastAPI اللي تفتح عليه صفحة index.
param(
    [string]$BaseUrl = "http://localhost:1234/v1",
    [string]$Model = "",
    [string]$ReasoningEffort = "none",
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

# ── 2) اكتشاف الموديل المحمّل بـ LM Studio ───────────────────────────────────
# الشرح: /v1/models ما يفيدنا هنا لأن LM Studio (مع JIT loading) يرجّع كل
# الموديلات المنزّلة، وإذا أرسلنا اسم موديل مو محمّل يحمّله تلقائياً — هيچ
# كان السكربت يفرض Gemma. الـ endpoint الأصلي /api/v0/models يرجّع لكل موديل
# حقل state، فناخذ اللي state = "loaded" ونوعه llm أو vlm (نستبعد embeddings).
# $apiRoot = نفس العنوان بدون /v1 في آخره.
$apiRoot = $BaseUrl -replace '/v1/?$', ''
$loaded = @()
try {
    $catalog = Invoke-RestMethod -Uri "$apiRoot/api/v0/models" -TimeoutSec 5
    $loaded = @($catalog.data | Where-Object { $_.state -eq "loaded" -and $_.type -in @("llm", "vlm") } | ForEach-Object { $_.id })
    $reachable = $true
} catch {
    $reachable = $false
}

# الشرح: نقرر أي موديل نستعمل:
#   - مررت -Model بنفسك  → نحترمه ونحذّر فقط إذا مو محمّل (LM Studio راح يحمّله عند أول طلب).
#   - -Model فارغ         → ناخذ المحمّل حالياً. إذا أكثر من واحد ناخذ الأول ونطبع الباقي.
#   - ماكو موديل محمّل أو LM Studio مطفي → نوقف برسالة واضحة، لأن ما نعرف شنو نختار
#     وما نريد نرجع نفرض موديل من عندنا.
if ($Model) {
    if (-not $reachable) {
        Write-Warning "LM Studio is not reachable at $BaseUrl - start its server (Developer tab). The agent replies 'model offline' until then."
    } elseif ($loaded -notcontains $Model) {
        Write-Warning "Model '$Model' is not loaded in LM Studio - it will be JIT-loaded on the first request. Loaded now: $($loaded -join ', ')"
    }
} elseif (-not $reachable) {
    Write-Error "LM Studio is not reachable at $BaseUrl - start its server (Developer tab), load a model, then run this script again (or pass -Model <id>)."
} elseif ($loaded.Count -eq 0) {
    Write-Error "No model is loaded in LM Studio - load any model you want, then run this script again (or pass -Model <id>)."
} else {
    $Model = $loaded[0]
    if ($loaded.Count -gt 1) {
        Write-Warning "Several models are loaded ($($loaded -join ', ')) - using '$Model'. Pass -Model <id> to pick another."
    }
}
Write-Host "==> Model: $Model"

# ── 3) متغيرات البيئة ────────────────────────────────────────────────────────
# الشرح: app/config.py يقرأ هذه القيم بدل الافتراضية مالت vLLM:
#   VLLM_BASE_URL        : وين خادم الموديل (هنا LM Studio).
#   MODEL_NAME           : الموديل اللي اكتشفناه/اخترناه فوق — يُرسل بحقل "model" بكل طلب.
#   LLM_REASONING_EFFORT : "none" يطفي تفكير الموديلات اللي تفكر (Gemma 4، gpt-oss...)
#                          حتى ما ياكل max_tokens ويقطع الرد أو وسائط الأداة (JSON).
# المتغيرات تخص هذي الجلسة فقط، وتنمسح لما تسكّر الطرفية.
$env:VLLM_BASE_URL = $BaseUrl
$env:MODEL_NAME = $Model
$env:LLM_REASONING_EFFORT = $ReasoningEffort
$env:PYTHONIOENCODING = "utf-8"

# ── 4) تشغيل FastAPI ─────────────────────────────────────────────────────────
# الشرح: 127.0.0.1 = جهازك فقط. افتح http://localhost:8000/ لصفحة الاختبار
# و http://localhost:8000/docs لتوثيق الـ API. Ctrl+C للإيقاف.
Write-Host "==> Starting FastAPI on http://localhost:$Port/ (model: $Model via $BaseUrl)"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
