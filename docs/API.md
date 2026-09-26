# مرجع الـ API

العنوان الأساسي:

| البيئة | الرابط |
|---|---|
| محلياً / داخل الـ Pod | `http://127.0.0.1:8000` |
| RunPod من الخارج | `https://<POD_ID>-8000.proxy.runpod.net` |

التوثيق التفاعلي (تجرب الطلبات من المتصفح مباشرة): `GET /docs` — ومخطط OpenAPI الخام: `GET /openapi.json`.

> ما كو مصادقة (API key) حالياً — أي أحد يعرف الرابط يقدر يستدعي الإيند بوينتات. ضيف حماية قبل النشر العام.

---

## `GET /`

واجهة المحادثة مع الوكيل: بطاقة التأكيد، لوحة الرصيد وآخر العمليات، وأدوات العرض (إعادة البيانات وحقن الأعطال).

---

## `GET /health`

هل FastAPI نفسه شغّال؟ يرجع `200` دائماً ما دام السيرفر حي — **ما يعني** إن الموديل جاهز (لهذا استعمل `/status`).

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"status": "healthy"}
```

---

## `GET /status`

حالة الموديل.

```bash
curl -s http://127.0.0.1:8000/status
```

```json
{
  "model": "ameer4wisam/gemma-iraqi-10k-merged",
  "vllm_base_url": "http://127.0.0.1:18001/v1",
  "model_ready": true
}
```

| الحقل | الوصف |
|---|---|
| `model` | اسم الموديل من `app/config.py` |
| `vllm_base_url` | عنوان خادم vLLM الداخلي |
| `model_ready` | `true` بعد ما vLLM يكمّل تحميل الأوزان ويرد على `/v1/models`. يُفحص كل 10 ثواني، فيرجع `false` تلقائياً إذا vLLM وقف |

---

## وكيل الدفع والتحويل — `/agent`

كل رد من الوكيل بهذا الشكل:

```json
{
  "session_id": "a1b2c3d4e5f6",
  "user_id": "u1",
  "parser": "llm | rules | rules_fallback",
  "state": {"awaiting": "confirm", "pending_confirmation_id": "9f2c…", "queued_requests": 0},
  "messages": [
    {"code": "confirm_transfer", "kind": "confirmation", "text": "راح أحوّل …", "confirmation": {"id": "9f2c…", "…": "…"}}
  ]
}
```

- `messages` قائمة لأن الدور الواحد ممكن يرجّع أكثر من رسالة (نتيجة عملية + بطاقة الطلب التالي).
- `kind`: `question` (يحتاج جواب؛ ممكن ويا `options`) · `confirmation` (بطاقة) · `result` · `error` · `info`.
- `code`: رمز ثابت تعتمد عليه الواجهة والاختبارات. الأسئلة: `ask_contact_choice`، `ask_contact_fuzzy`،
  `ask_recipient`، `unknown_contact`، `ask_amount`، `ask_amount_thousands`، `ask_amount_usd`، `ask_amount_slang`،
  `ask_amount_all_balance`، `ask_amount_multiple`، `ask_bill_account`، `ask_confirm_again`. البطاقات:
  `confirm_transfer`، `confirm_bill`. النتائج: `executed`، `already_executed`. الرفض: `insufficient_funds`،
  `limit_exceeded`، `recipient_not_on_wallet`، `recipient_unavailable`، `self_transfer`، `biller_unavailable`،
  `nothing_due`، `amount_exceeds_due`، `invalid_amount`، `no_bill_account`، `wallet_unavailable`،
  `payment_status_unknown`، `stale_confirmation`. معلومات: `balance`، `history`، `multi_request`، `cancelled`،
  `unsupported`، `not_understood`، `nothing_pending`.

### `POST /agent/message`

| الحقل | الوصف |
|---|---|
| `text` | رسالة المستخدم |
| `option_id` | بدل `text`: اختيار زر من `options` السؤال |
| `session_id` | فارغ = جلسة جديدة؛ بعدها أرسل نفس الرقم |
| `user_id` | فارغ = `u1` |

```bash
curl -s -X POST localhost:8000/agent/message -H 'Content-Type: application/json'   -d '{"text": "دز 50 الف لأحمد"}'
# → ask_contact_choice مع خيارين (c1, c2)

curl -s -X POST localhost:8000/agent/message -H 'Content-Type: application/json'   -d '{"session_id": "<SID>", "option_id": "c2"}'
# → confirm_transfer مع بطاقة: المستلم، المبلغ، العمولة، المجموع، الرصيد بعدها، التنبيهات
```

بطاقة التأكيد (`confirmation`):

| الحقل | الوصف |
|---|---|
| `id` | رقم البطاقة = مفتاح الـ Idempotency بالمحفظة |
| `kind` | `transfer` أو `bill_payment` |
| `amount`، `fee`، `total` | المبلغ، العمولة، المجموع المخصوم |
| `balance`، `balance_after` | الرصيد قبل وبعد |
| `recipient_name`، `recipient_relation`، `recipient_phone_masked` | للتحويل (الاسم المسجّل بالمحفظة) |
| `biller`، `label`، `account_no`، `due_amount`، `is_full_due` | للفاتورة |
| `warnings` | تنبيهات: أول تحويل لهذا الشخص، أكثر من نص الرصيد، دفع جزئي |
| `expires_at`، `expires_in_seconds` | مهلة البطاقة |

### `POST /agent/confirm`

```bash
curl -s -X POST localhost:8000/agent/confirm -H 'Content-Type: application/json'   -d '{"session_id": "<SID>", "confirmation_id": "<CARD_ID>"}'
```

- ينفّذ **حمولة البطاقة بالضبط**. إرسال نفس الطلب مرة ثانية يرجّع `already_executed` بدون تنفيذ.
- بطاقة قديمة أو ملغية → `stale_confirmation`. منتهية وما انحاولت → `confirmation_expired` + بطاقة جديدة.
- كتابة «اكد» / «اي» بـ `/agent/message` تسوي نفس الشي للبطاقة الحالية.

### `POST /agent/cancel`

`{"session_id": "<SID>", "confirmation_id": "<اختياري>", "everything": false}` — `everything: true` يلغي كل
الطلبات بالطابور (مثل كتابة «الغي الكل»).

### `GET /agent/session/{session_id}`

حالة الجلسة: السؤال الحالي، البطاقة المعلّقة، الطلبات بالطابور، وسجل المحادثة.

---

## المحفظة الوهمية — `/wallet`

توثيق تفاعلي كامل: `GET /wallet/docs`. كل المبالغ أعداد صحيحة بالدينار العراقي.

| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/wallet/users/{uid}` | المستخدم ورصيده |
| GET | `/wallet/users/{uid}/contacts` | جهات الاتصال (فيها `wallet_user_id` = null لمن ما عنده محفظة) |
| GET | `/wallet/users/{uid}/bill-accounts` | حسابات الفواتير مع الجهة والمستحق |
| GET | `/wallet/users/{uid}/transactions?limit=&idempotency_key=` | السجل؛ أو البحث عن حركة بمفتاح (للتسوية) |
| GET | `/wallet/directory/{phone}` | هل الرقم عنده محفظة؟ |
| POST | `/wallet/users/{uid}/quotes` | "شنو راح يصير؟" بدون تنفيذ: العمولة، المجموع، الرصيد بعدها، أو سبب الرفض |
| POST | `/wallet/users/{uid}/transfers` | تحويل — **يتطلب هيدر `Idempotency-Key`** |
| POST | `/wallet/users/{uid}/bill-payments` | دفع فاتورة — **يتطلب هيدر `Idempotency-Key`** |
| POST | `/wallet/_admin/reset` | إرجاع البيانات لحالة البذرة |
| POST | `/wallet/_admin/faults` | حقن عطل: `{"mode": "drop_response_after_commit" \| "fail_before_commit" \| null, "times": 1}` |

```bash
# نفس المفتاح مرتين = عملية وحدة؛ الرد الثاني فيه "replayed": true
for i in 1 2; do
  curl -s -X POST localhost:8000/wallet/users/u1/transfers -H 'Content-Type: application/json'     -H 'Idempotency-Key: demo-1' -d '{"amount": 50000, "to_contact_id": "c1"}'; echo
done
```

أخطاء المحفظة: `{"error": {"code": "INSUFFICIENT_FUNDS", "message": "..."}}` — الرموز: `INVALID_AMOUNT`،
`LIMIT_EXCEEDED`، `DAILY_LIMIT_EXCEEDED`، `INSUFFICIENT_FUNDS`، `RECIPIENT_NOT_ON_WALLET`، `RECIPIENT_UNAVAILABLE`،
`SELF_TRANSFER`، `BILLER_UNAVAILABLE`، `NOTHING_DUE`، `AMOUNT_EXCEEDS_DUE`، `ACCOUNT_FROZEN`،
`IDEMPOTENCY_KEY_REQUIRED` (400)، `IDEMPOTENCY_KEY_REUSED` (409).

---

## `POST /chat`

الإيند بوينت الافتراضي: يرسل رسالة للموديل ويرجع الرد. الكود بـ [app/endpoints/chat.py](../app/endpoints/chat.py) — عدّله أو انسخه حسب حاجتك.

### الطلب

`Content-Type: application/json`

| الحقل | النوع | إجباري | الوصف |
|---|---|---|---|
| `message` | string | ✅ | رسالة المستخدم الحالية |
| `system_prompt` | string | — | تعليمات تحدد شخصية/سلوك الموديل |
| `history` | array | — | رسائل سابقة بصيغة OpenAI حتى الموديل يتذكر المحادثة. الافتراضي `[]` |
| `max_tokens` | integer | — | أقصى طول للرد. الافتراضي `max_new_tokens` من `config.py` (512) |

ترتيب الرسائل اللي توصل للموديل: `system_prompt` (إن وجد) ← `history` ← `message`.

التوليد حتمي (`temperature = 0.0`): نفس المدخل يعطي نفس الرد.

### الرد

```json
{"reply": "هلا بيك! آني بخير، شلون أكدر أساعدك؟"}
```

### أمثلة

رسالة وحدة:

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"شلونك؟"}'
```

مع تعليمات نظام وسجل محادثة:

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "system_prompt": "انت مساعد مبيعات لطيف، جاوب باللهجة العراقية وباختصار.",
    "history": [
      {"role": "user", "content": "عندكم لابتوبات؟"},
      {"role": "assistant", "content": "إي عدنا، شنو الميزانية مالتك؟"}
    ],
    "message": "حدود 800 دولار",
    "max_tokens": 200
  }'
```

من JavaScript:

```js
const res = await fetch("/chat", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ message: "شلونك؟" }),
});
const { reply } = await res.json();
```

> السجل ما ينحفظ بالسيرفر — الواجهة مسؤولة تحتفظ بـ `history` وترسله مع كل طلب (تضيف رسالة المستخدم ورد الموديل بعد كل دورة).

### الأخطاء

| الكود | المعنى | مثال `detail` |
|---|---|---|
| `422` | جسم الطلب غلط (مثلاً `message` ناقص) — FastAPI يتحقق تلقائياً | تفاصيل الحقل الغلط |
| `503` | الموديل بعده يتحمّل (`model_ready: false`) — أعد المحاولة بعد شوي | `"Model is still loading"` |
| `502` | vLLM رفض الطلب أو فشل الاتصال بيه (مثلاً الطلب أطول من `max_model_len`) | `"vLLM error: ..."` |

```json
{"detail": "Model is still loading"}
```

---

## الوصول المباشر لـ vLLM (داخل السيرفر فقط)

خادم vLLM مربوط على `127.0.0.1:18001` فقط، فما ينوصل من خارج الـ Pod. داخل الـ Pod تقدر تستدعيه مباشرة بصيغة OpenAI للتجربة أو التشخيص:

```bash
# الموديلات المحمّلة
curl -s http://127.0.0.1:18001/v1/models

# طلب مباشر
curl -s http://127.0.0.1:18001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ameer4wisam/gemma-iraqi-10k-merged",
    "messages": [{"role": "user", "content": "شلونك؟"}],
    "max_tokens": 100
  }'

# إحصاءات المحرك (KV cache، طلبات نشطة...)
curl -s http://127.0.0.1:18001/metrics
```
