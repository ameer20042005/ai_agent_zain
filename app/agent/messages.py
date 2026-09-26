# -*- coding: utf-8 -*-
"""كل النصوص اللي يشوفها المستخدم — قوالب ثابتة بالعراقي يكتبها الكود.

ليش مو الموديل يكتب الرد؟ لأن رسائل الدفع لازم تكون **صادقة ودقيقة حرفياً**:
المبلغ، اسم المستلم، والنتيجة (تمت / ما تمت / ما نعرف). موديل لغة ممكن
يصيغ "تمت العملية" بثقة وهي ما تمت. القوالب ما تكذب.
"""

from app.agent.resolver import CATEGORY_NAMES, mask_phone


def iqd(n: int) -> str:
    return f"{n:,} دينار"


HELP = ("أكدر أساعدك بـ: تحويل فلوس لشخص (مثلاً «دز 50 الف لأحمد»)، دفع فاتورة "
        "(«ادفع قائمة الكهرباء»)، معرفة رصيدك، أو آخر عملياتك.")

# الشرح: رسالة لكل رمز فشل من المحفظة. كل وحدة تقول: شنو صار، هل انخصم شي،
# وشنو يكدر يسوي المستخدم بعدها.
FAILURES = {
    "INSUFFICIENT_FUNDS": "رصيدك {balance} وما يكفي لـ {total} مع العمولة. ما نفّذت شي.",
    "LIMIT_EXCEEDED": "المبلغ أكبر من الحد المسموح للعملية الوحدة (2,000,000 دينار). ما نفّذت شي — تكدر تقسمه على أكثر من عملية.",
    "DAILY_LIMIT_EXCEEDED": "هذي العملية تعدّي حدك اليومي (3,000,000 دينار). ما نفّذت شي — تكدر تكملها باچر.",
    "RECIPIENT_NOT_ON_WALLET": "{name} ما عنده محفظة، فما أكدر أحوّله. ما انخصم منك شي.",
    "RECIPIENT_UNAVAILABLE": "حساب {name} موقوف حالياً وما يستلم فلوس. ما انخصم منك شي.",
    "SELF_TRANSFER": "هذا رقمك إنت — ما أكدر أحوّل لنفس المحفظة.",
    "BILLER_UNAVAILABLE": "{biller} ما تستقبل دفعات هسه (صيانة). ما انخصم منك شي — جرّب بعد شوي.",
    "NOTHING_DUE": "ماكو فاتورة مستحقة على {biller} ({label}) — ما عليك شي تدفعه.",
    "AMOUNT_EXCEEDS_DUE": "المبلغ أكبر من المستحق على {biller} ({label}) — المستحق {due} بس. ما نفّذت شي.",
    "INVALID_AMOUNT": "المبلغ لازم يكون 250 دينار أو أكثر. ما نفّذت شي.",
    "ACCOUNT_FROZEN": "حسابك موقوف حالياً، فما أكدر أنفّذ أي دفعة. راجع خدمة الزبائن.",
    "CONTACT_NOT_FOUND": "ما لگيت هذا الشخص بجهات اتصالك. ما نفّذت شي.",
    "BILL_ACCOUNT_NOT_FOUND": "ما لگيت حساب الفاتورة هذا. ما نفّذت شي.",
}
GENERIC_FAILURE = "صار خطأ من جهة المحفظة ({code}) وما تمت العملية. ما انخصم منك شي."


def failure_text(code: str, **ctx) -> str:
    template = FAILURES.get(code)
    if template is None:
        return GENERIC_FAILURE.format(code=code)
    for k in ("balance", "total", "due"):
        if isinstance(ctx.get(k), int):
            ctx[k] = iqd(ctx[k])
    try:
        return template.format(**ctx)
    except KeyError:
        return template.split(" — ")[0].split("{")[0].strip() + " ما نفّذت شي."


def contact_label(c: dict) -> str:
    rel = f" ({c['relation']})" if c.get("relation") else ""
    return f"{c['name']}{rel} — {mask_phone(c['phone'])}"


def bill_label(a: dict) -> str:
    due = f"المستحق {iqd(a['due_amount'])}" if a["due_amount"] else "ما عليها شي"
    return f"{a['biller_name']} — {a['label']} ({due})"


def ask_contact_choice(name_hint: str, n: int) -> str:
    return f"عندك {n} أشخاص يطابقون «{name_hint}». لمن تقصد؟ اختار رقم أو اكتب الاسم الكامل."


def ask_contact_fuzzy(n: int) -> str:
    return ("ما لگيت الاسم بالضبط، بس أكو اسم يشبهه. تقصد:" if n == 1
            else "ما لگيت الاسم بالضبط، بس أكو أسماء تشبهه. تقصد وحد منهم؟")


def unknown_contact(name: str) -> str:
    return (f"ما لگيت «{name}» بجهات اتصالك. اكتب رقم هاتفه (مثل 07701234567) "
            "وأتأكد إذا عنده محفظة، أو اكتب «الغي».")


ASK_RECIPIENT = "لمن تريد تحوّل؟ اكتب الاسم أو رقم الهاتف."
ASK_AMOUNT = "شكد تريد تحوّل؟ اكتب المبلغ بالدينار (مثلاً 50 الف)."
ASK_AMOUNT_MULTIPLE = "ذكرت أكثر من مبلغ ({amounts}). يا مبلغ تقصد؟"
ASK_AMOUNT_USD = "المحفظة بالدينار العراقي بس. شكد المبلغ بالدينار؟"
ASK_AMOUNT_SLANG = "«{text}» تختلف من شخص لشخص. اكتب المبلغ بالدينار بالضبط حتى ما يصير غلط."
ASK_AMOUNT_ALL = "ما أحوّل «كل الرصيد» بدون رقم صريح. اكتب المبلغ اللي تريده بالضبط بالدينار."
ASK_AMOUNT_NEGATIVE = "المبلغ لازم يكون رقم موجب. شكد تريد بالضبط؟"


def ask_thousands(text: str, suggested: int) -> str:
    return f"قصدك «{text}» يعني {iqd(suggested)}؟ اكتب «اي» للتأكيد أو اكتب المبلغ كامل."


def ask_bill_account(category: str) -> str:
    what = CATEGORY_NAMES.get(category, "")
    return f"عندك أكثر من حساب {what}. يا واحد تريد تدفع؟" if category else "يا فاتورة تريد تدفع؟"


def no_bill_account(category: str) -> str:
    return f"ما عندك حساب {CATEGORY_NAMES.get(category, 'فاتورة')} مسجّل بالمحفظة. ما نفّذت شي."


def multi_request(n: int, summaries: list) -> str:
    items = "، ".join(f"({i}) {s}" for i, s in enumerate(summaries, 1))
    return f"طلبت {n} عمليات: {items}. نسويهن وحدة وحدة، وكل وحدة تحتاج تأكيدك."


CONFIRM_HINT = "اضغط «أكّد» أو اكتب «أكد» حتى أنفّذ، أو «الغي»."
CONFIRM_UNCLEAR = "ما نفّذت شي. العملية بعدها تنتظر تأكيدك: " + CONFIRM_HINT
CANCELLED = "لغيت العملية. ما انخصم منك شي."
CANCELLED_ALL = "لغيت كل العمليات اللي جانت تنتظر. ما انخصم منك شي."
NOTHING_PENDING = "ماكو عملية تنتظر تأكيد."
STALE_CONFIRMATION = "هذا التأكيد مو للعملية الحالية (قديم أو ملغي). ما نفّذت شي."
EXPIRED = "انتهت مهلة التأكيد، فما نفّذت العملية. جهّزتلك تأكيد جديد بالأرقام المحدّثة:"
NOT_UNDERSTOOD = "ما فهمت الطلب. " + HELP
UNSUPPORTED = "هذي العملية ما أكدر أسويها هنا. " + HELP
GIVE_UP = "ما گدرت أحدد المطلوب، فلغيت هذا الطلب حتى ما يصير غلط. ما انخصم منك شي."


def confirm_text(card: dict) -> str:
    """نص التأكيد (للي يقرأ أو يسمع بدل ما يشوف البطاقة)."""
    if card["kind"] == "transfer":
        rel = f"{card['recipient_relation']}، " if card.get("recipient_relation") else ""
        head = f"راح أحوّل {iqd(card['amount'])} إلى {card['recipient_name']} ({rel}{card['recipient_phone_masked']})."
    else:
        head = (f"راح أدفع {iqd(card['amount'])} لـ {card['biller']} — {card['label']} "
                f"(حساب {card['account_no']}).")
    fee = f"العمولة {iqd(card['fee'])}، " if card["fee"] else "بدون عمولة، "
    tail = f"{fee}ينخصم منك {iqd(card['total'])} ويبقى رصيدك {iqd(card['balance_after'])}."
    warn = (" تنبيه: " + " ".join(card["warnings"])) if card.get("warnings") else ""
    return f"{head} {tail}{warn} {CONFIRM_HINT}"


def executed_text(card: dict, data: dict, replayed: bool) -> str:
    tx = data["transaction"]
    what = (f"حوّلت {iqd(tx['amount'])} إلى {card.get('recipient_name', tx['counterparty'])}"
            if card["kind"] == "transfer" else f"دفعت {iqd(tx['amount'])} لـ {tx['counterparty']}")
    extra = ""
    if data.get("remaining_due"):
        extra = f" باقي على الفاتورة {iqd(data['remaining_due'])}."
    note = " (صار تأخير بالشبكة وأعدنا المحاولة — العملية انحسبت مرة وحدة بس.)" if replayed else ""
    return f"✅ تمت: {what}. رقم العملية {tx['id']}. رصيدك هسه {iqd(data['balance'])}.{extra}{note}"


ALREADY_EXECUTED = "هذي العملية منفّذة من قبل (رقم {tx}) — ما انخصم شي جديد."
NOT_DONE = ("❌ ما گدرت أوصل للمحفظة وما تمت العملية — تأكدت من سجلك وما انخصم شي. "
            "تكدر تضغط «أكّد» مرة ثانية للمحاولة (نفس الطلب ما ينخصم مرتين).")
STATUS_UNKNOWN = ("⚠️ ما گدرت أتأكد إذا العملية تمت بسبب مشكلة بالاتصال. لا تدفعها من مكان ثاني. "
                  "اضغط «أكّد» بعد شوي — نفس الطلب ما ينخصم مرتين، وراح أبلغك بالنتيجة الأكيدة.")
