# مخطط عمل وكيل زين

```mermaid
sequenceDiagram
    autonumber
    actor user as المستخدم
    participant ui as واجهة المحادثة
    participant api as FastAPI
    participant agent as Assistant
    participant model as LM Studio أو vLLM
    participant wallet as Wallet API
    participant db as SQLite

    user->>ui: طلب تحويل أو دفع فاتورة
    ui->>api: POST /assistant/message
    api->>agent: قفل الجلسة وتمرير الرسالة
    agent->>wallet: قراءة الرصيد والجهات والفواتير
    wallet->>db: استعلام
    db-->>wallet: بيانات المحفظة
    wallet-->>agent: بيانات المستخدم
    agent->>model: السياق وتعريف الأدوات
    model-->>agent: اقتراح تحويل أو دفع فاتورة
    agent->>wallet: POST /quotes
    wallet->>db: فحص الرصيد والحدود
    db-->>wallet: بيانات التسعير
    wallet-->>agent: المبلغ والعمولة والرصيد بعد العملية
    agent-->>ui: بطاقة تأكيد من بيانات المحفظة
    ui-->>user: عرض المستلم والمبلغ والعمولة

    alt المستخدم يؤكد
        user->>ui: ضغط أكّد
        ui->>api: POST /assistant/confirm
        api->>agent: قفل الجلسة وفحص البطاقة
        agent->>wallet: تنفيذ بمفتاح البطاقة نفسه
        wallet->>db: معاملة ذرية وتسجيل مفتاح منع التكرار
        db-->>wallet: نتيجة العملية
        wallet-->>agent: نجاح أو رفض أو حالة غير مؤكدة
        agent->>model: حدث التطبيق بنتيجة المحفظة
        model-->>agent: صياغة رد للمستخدم
        agent-->>ui: النتيجة والرصيد إن توفر
        ui-->>user: عرض النتيجة
    else المستخدم يلغي
        user->>ui: ضغط إلغاء
        ui->>api: POST /assistant/cancel
        api->>agent: إلغاء البطاقة المعلقة
        agent-->>ui: تأكيد الإلغاء دون تنفيذ
    end
```

**حدود المسؤولية:** الموديل يدير الحوار ويطلب إنشاء بطاقة فقط. المحفظة تحسب الأرقام وتفرض قواعد الدفع. لا تبدأ عملية الخصم إلا عند تأكيد بطاقة فعالة. يستخدم التنفيذ رقم البطاقة مفتاحًا لمنع التكرار؛ وعند انقطاع الاتصال يعيد المحاولة بالمفتاح نفسه ويفحص سجل العمليات قبل إعلان النتيجة.
