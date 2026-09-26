# -*- coding: utf-8 -*-
"""الوكيل: آلة حالات تحوّل جملة → طلب → أسئلة توضيح → بطاقة تأكيد → تنفيذ → نتيجة.

    رسالة ──► NLU ──► مسودة (Draft) ──► حل الغموض ──► quote ──► بطاقة تأكيد
                         ▲   │ ناقص شي؟                          │ "أكد"
                         │   ▼                                   ▼
                         └─ سؤال (awaiting) ◄── جواب      تنفيذ بمفتاح البطاقة
                                                                 │
                                          الطلب التالي بالطابور ◄─┘ نتيجة صادقة

قواعد السلامة (مفروضة هنا بالكود، مو بالبرومبت):
  1. ما كو تنفيذ بدون بطاقة تأكيد، والتأكيد لازم يكون صريح ("أكد"/زر) —
     أي جواب ثاني ما يُحسب موافقة.
  2. البطاقة مربوطة بحمولة (payload) ثابتة: اللي انعرض هو بالضبط اللي يتنفّذ.
  3. مفتاح الـ idempotency = رقم البطاقة، فالضغط مرتين أو إعادة المحاولة ما
     تخصم مرتين.
  4. أي غموض (اسمين، حسابين، مبلغ مو واضح) = سؤال، مو تخمين.
"""

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from app.agent import messages as M
from app.agent import nlu
from app.agent.amounts import AmountMention, parse_amounts
from app.agent.resolver import (CATEGORY_NAMES, STOPWORDS, find_categories, find_phone, mask_phone,
                                resolve_bill_account, resolve_contact)
from app.agent.textnorm import normalize, token_variants, tokens
from app.agent.wallet_client import WalletClient
from app.config import settings

# ---------------------------------------------------------------------------
# كلمات الموافقة والرفض
# ---------------------------------------------------------------------------

# الشرح: الموافقة لازم تكون رسالة قصيرة كلها كلمات موافقة ("اي اكد حبيبي").
# "اي بس خليها 30" مو موافقة — بيها "بس" يعني تعديل، فتعتبر تعديل مو تأكيد.
_YES = {"اكد", "اكدها", "تاكيد", "نعم", "اي", "ايه", "ايي", "اوك", "ok", "okay", "تمام", "موافق",
        "يلا", "نفذ", "نفذها", "كمل", "صح", "اكيد", "ماشي", "بلي", "yes", "confirm", "ثبت", "ثبتها"}
# فعل العملية نفسه كجواب على بطاقتها ("دزها" على بطاقة تحويل) = موافقة. بس
# "ادفع" على بطاقة تحويل مو موافقة، وبدون بطاقة أصلاً هو طلب جديد مو موافقة.
_YES_VERBS = {"transfer": {"دز", "دزها", "حول", "حولها"}, "bill_payment": {"ادفع", "ادفعها", "سدد", "سددها"}}
# كلمات تجي ويا الموافقة بدون ما تغيّر معناها ("اكد التحويل"، "اي نفذ العملية").
_FILLER = {"حبيبي", "عيوني", "رجاءا", "رجاء", "اخي", "خويه", "هسه", "بسرعه", "الله", "يخليك",
           "و", "من", "فضلك", "لطفا", "شكرا", "التحويل", "تحويل", "الحواله", "الدفع", "الدفعه",
           "العمليه", "عمليه", "الفاتوره", "القائمه", "القايمه", "هاي", "هذي", "هذا", "ها", "ياه"}
_NO = {"لا", "لاء", "لع", "الغي", "الغيها", "الغ", "الغاء", "كنسل", "cancel", "وكف", "وقف",
       "وقفها", "بطلت", "لغيها", "no", "ماريد", "ماابي"}
_NO_PHRASES = ("ما اريد", "لا تسوي", "لا تحول", "لا تدفع", "ما ابي", "ما ابغي")
_ALL_WORDS = {"الكل", "كلشي", "كلهن", "كلها", "الجميع"}
_ORDINALS = {"الاول": 0, "الاولي": 0, "اول": 0, "الثاني": 1, "الثانيه": 1, "ثاني": 1,
             "الثالث": 2, "الثالثه": 2, "ثالث": 2, "الرابع": 3, "الرابعه": 3, "رابع": 3}


def _is_yes(toks: List[str], kind: Optional[str] = None) -> bool:
    yes = _YES | _YES_VERBS.get(kind, set())
    return 0 < len(toks) <= 5 and all(t in yes | _FILLER for t in toks) and any(t in yes for t in toks)


def _is_no(norm: str, toks: List[str]) -> bool:
    return any(t in _NO for t in toks) or any(p in norm for p in _NO_PHRASES)


def _name_hint(text: str) -> str:
    """الاسم مثل ما كتبه المستخدم، بدون أفعال/أرقام/حروف جر ("دز 50 لكرار" → "كرار")."""
    words = []
    for t in tokens(normalize(text)):
        if t in STOPWORDS or any(ch.isdigit() for ch in t) or parse_amounts(t):
            continue
        for p in ("لل", "ل"):
            if t.startswith(p) and len(t) > len(p) + 1:
                t = t[len(p):]
                break
        words.append(t)
    return " ".join(words[:3])


def _summary(d) -> str:
    """وصف قصير مفهوم للطلب ("تحويل 50,000 دينار لـ «احمد»"، "دفع فاتورة المولدة")."""
    if d.intent == "pay_bill":
        return "دفع فاتورة " + CATEGORY_NAMES.get(d.category, "") if d.category else "دفع فاتورة"
    clear = [m.value for m in d.amount_mentions if m.value is not None and not m.issue]
    who = _name_hint(d.recipient_text or d.text)
    return "تحويل" + (f" {M.iqd(clear[0])}" if clear else "") + (f" لـ «{who}»" if who else "")


# ---------------------------------------------------------------------------
# الحالة
# ---------------------------------------------------------------------------

@dataclass
class Draft:
    """طلب واحد قيد التجهيز. الحقول تتعبّى تدريجياً لحد ما يكتمل."""
    intent: str                                   # transfer | pay_bill
    text: str                                     # كلام المستخدم الخاص بهذا الطلب
    category: Optional[str] = None
    recipient_text: Optional[str] = None
    phone: Optional[str] = None
    amount_mentions: List[AmountMention] = field(default_factory=list)
    amount: Optional[int] = None
    contact: Optional[dict] = None                # {id?, name, phone, relation}
    bill_account: Optional[dict] = None

    @classmethod
    def from_parsed(cls, r: nlu.ParsedRequest) -> "Draft":
        return cls(intent=r.intent, text=r.text, category=r.category,
                   recipient_text=r.recipient_text, phone=r.phone, amount_mentions=r.amounts)


@dataclass
class Confirmation:
    """بطاقة تأكيد. id = مفتاح الـ idempotency بالمحفظة.
    status: pending | completed | rejected | cancelled | expired
    attempted: هل حاولنا ننفّذها؟ (بعد أول محاولة ما نولّد مفتاح جديد أبداً)"""
    id: str
    kind: str
    body: dict
    card: dict
    draft: Draft
    expires_at: float
    status: str = "pending"
    attempted: bool = False
    result: Optional[dict] = None


@dataclass
class Session:
    id: str
    user_id: str
    draft: Optional[Draft] = None
    queue: List[Draft] = field(default_factory=list)
    awaiting: Optional[str] = None                # contact | recipient | bill_account | amount | amount_thousands | confirm
    options: List[dict] = field(default_factory=list)
    suggested_amount: Optional[int] = None
    retries: int = 0
    pending: Optional[Confirmation] = None
    confirmations: dict = field(default_factory=dict)
    parser: str = "rules"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    updated_at: float = field(default_factory=time.time)
    log: List[dict] = field(default_factory=list)


class SessionStore:
    """جلسات بالذاكرة (تكفي للنموذج الأولي؛ الإنتاج يحتاج Redis/قاعدة بيانات)."""

    def __init__(self, ttl_seconds: int = 3600):
        self._sessions: dict = {}
        self._ttl = ttl_seconds

    def get(self, session_id: Optional[str], user_id: str) -> Session:
        now = time.time()
        for sid in [k for k, s in self._sessions.items() if now - s.updated_at > self._ttl]:
            del self._sessions[sid]
        if session_id and session_id in self._sessions:
            s = self._sessions[session_id]
            s.updated_at = now
            return s
        s = Session(id=session_id or uuid.uuid4().hex[:12], user_id=user_id)
        self._sessions[s.id] = s
        return s

    def find(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)


# ---------------------------------------------------------------------------
# الوكيل
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self, wallet: Optional[WalletClient] = None):
        self.wallet = wallet or WalletClient()

    # ── أدوات صغيرة ─────────────────────────────────────────────────────────
    @staticmethod
    def _say(out: list, code: str, kind: str, text: str, **extra) -> None:
        """يضيف رسالة للرد. kind: question | confirmation | result | error | info."""
        out.append({"code": code, "kind": kind, "text": text, **{k: v for k, v in extra.items() if v is not None}})

    def _ask(self, s: Session, out: list, awaiting: str, code: str, text: str, options=None) -> None:
        s.awaiting = awaiting
        s.options = options or []
        opts = [{"id": o["id"], "label": o["label"]} for o in s.options] or None
        self._say(out, code, "question", text, options=opts)

    async def _finish_draft(self, s: Session, out: list) -> None:
        """الطلب الحالي خلص (نجح/فشل/انلغى) → نبدأ اللي بعده بالطابور إن وجد."""
        s.draft, s.awaiting, s.options, s.retries, s.suggested_amount = None, None, [], 0, None
        if s.queue:
            nxt = s.queue.pop(0)
            self._say(out, "next_request", "info", f"نكمل بالطلب التالي: {_summary(nxt)}")
            await self._advance(s, nxt, out)

    # ── نقطة الدخول: رسالة ─────────────────────────────────────────────────
    async def handle_message(self, s: Session, text: str = "", option_id: Optional[str] = None) -> list:
        out: list = []
        text = (text or "").strip()
        s.log.append({"role": "user", "text": text, "option_id": option_id})
        if option_id:
            await self._answer_option(s, option_id, out)
        elif not text:
            self._say(out, "not_understood", "info", M.NOT_UNDERSTOOD)
        elif s.awaiting == "confirm" and s.pending:
            await self._reply_to_confirmation(s, text, out)
        elif s.awaiting and s.draft:
            await self._reply_to_question(s, text, out)
        elif _is_yes(tokens(normalize(text))) or _is_no(normalize(text), tokens(normalize(text))):
            self._say(out, "nothing_pending", "info", M.NOTHING_PENDING)
        else:
            await self._new_request(s, text, out)
        s.log.append({"role": "agent", "codes": [m["code"] for m in out]})
        return out

    async def _new_request(self, s: Session, text: str, out: list) -> None:
        result = await nlu.parse(text)
        s.parser = result.parser
        reqs = result.requests
        actionable = [r for r in reqs if r.intent in ("transfer", "pay_bill")]

        for r in reqs:                                    # القراءة فقط — ما تحتاج تأكيد
            if r.intent == "balance":
                user = await self.wallet.user(s.user_id)
                self._say(out, "balance", "info", f"رصيدك {M.iqd(user['balance'])}.", balance=user["balance"])
            elif r.intent == "history":
                await self._history(s, out)
            elif r.intent == "unsupported":
                self._say(out, "unsupported", "info", M.UNSUPPORTED)
        if not out and not actionable:
            self._say(out, "not_understood", "info", M.NOT_UNDERSTOOD)
            return
        if not actionable:
            return

        drafts = [Draft.from_parsed(r) for r in actionable]
        if len(drafts) > 1:
            self._say(out, "multi_request", "info", M.multi_request(len(drafts), [_summary(d) for d in drafts]))
        s.queue = drafts[1:]
        await self._advance(s, drafts[0], out)

    async def _history(self, s: Session, out: list) -> None:
        txs = await self.wallet.transactions(s.user_id, limit=5)
        names = {"transfer_out": "حوّلت", "transfer_in": "استلمت", "bill_payment": "دفعت"}
        lines = [f"{names.get(t['type'], t['type'])} {M.iqd(t['amount'])} — {t['counterparty']} ({t['created_at'][:10]})"
                 for t in txs]
        self._say(out, "history", "info", "آخر عملياتك:\n" + "\n".join(lines) if lines else "ماكو عمليات بعد.",
                  transactions=txs)

    # ── تجهيز الطلب خطوة بخطوة ─────────────────────────────────────────────
    async def _advance(self, s: Session, d: Draft, out: list) -> None:
        """يكمّل المسودة: يحل المستلم/الفاتورة، ثم المبلغ، ثم يعرض التأكيد.
        إذا ناقص شي يسأل ويوقف (والجواب يرجّعنا هنا)."""
        s.draft = d
        if d.intent == "transfer":
            if d.contact is None and not await self._resolve_recipient(s, d, out):
                return
            if d.amount is None and not self._resolve_amount(s, d, out, required=True):
                return
        else:
            if d.bill_account is None and not await self._resolve_bill(s, d, out):
                return
            if d.amount is None:
                if d.amount_mentions:
                    if not self._resolve_amount(s, d, out, required=True):
                        return
                else:
                    d.amount = d.bill_account["due_amount"]      # الافتراضي: القائمة كاملة
                    if d.amount == 0:
                        a = d.bill_account
                        self._say(out, "nothing_due", "error", M.failure_text(
                            "NOTHING_DUE", biller=a["biller_name"], label=a["label"]))
                        await self._finish_draft(s, out)
                        return
        await self._quote_and_confirm(s, d, out)

    async def _resolve_recipient(self, s: Session, d: Draft, out: list) -> bool:
        contacts = await self.wallet.contacts(s.user_id)
        if d.phone:
            same = [c for c in contacts if c["phone"] == d.phone]
            if same:
                d.contact = same[0]
                return True
            found = await self.wallet.lookup_phone(d.phone)
            if found is None:
                self._say(out, "recipient_not_on_wallet", "error",
                          M.failure_text("RECIPIENT_NOT_ON_WALLET", name=f"الرقم {d.phone}"))
                await self._finish_draft(s, out)
                return False
            d.contact = {"id": None, "name": found["name"], "phone": d.phone, "relation": "رقم مو محفوظ عندك"}
            return True

        match = resolve_contact(d.recipient_text or d.text, contacts)
        if match.status == "exact":
            d.contact = match.candidates[0]
            return True
        options = [{"id": c["id"], "label": M.contact_label(c), "contact": c} for c in match.candidates]
        if match.status == "ambiguous":
            self._ask(s, out, "contact", "ask_contact_choice",
                      M.ask_contact_choice(_name_hint(d.recipient_text or d.text), len(options)), options)
        elif match.status == "fuzzy":
            self._ask(s, out, "contact", "ask_contact_fuzzy", M.ask_contact_fuzzy(len(options)), options)
        else:
            hint = _name_hint(d.recipient_text or d.text)
            if hint:
                self._ask(s, out, "recipient", "unknown_contact", M.unknown_contact(hint))
            else:
                self._ask(s, out, "recipient", "ask_recipient", M.ASK_RECIPIENT)
        return False

    async def _resolve_bill(self, s: Session, d: Draft, out: list) -> bool:
        accounts = await self.wallet.bill_accounts(s.user_id)
        match = resolve_bill_account(d.text, d.category, accounts)
        if match.status == "exact":
            d.bill_account = match.candidates[0]
            d.category = d.bill_account["category"]
            return True
        if match.status == "none":
            self._say(out, "no_bill_account", "error", M.no_bill_account(d.category))
            await self._finish_draft(s, out)
            return False
        options = [{"id": a["id"], "label": M.bill_label(a), "account": a} for a in match.candidates]
        self._ask(s, out, "bill_account", "ask_bill_account", M.ask_bill_account(d.category), options)
        return False

    def _resolve_amount(self, s: Session, d: Draft, out: list, required: bool) -> bool:
        clear = [m for m in d.amount_mentions if m.value is not None and not m.issue]
        issues = [m for m in d.amount_mentions if m.issue]
        values = sorted({m.value for m in clear})
        if len(values) > 1 or (clear and issues):
            listed = "، ".join(M.iqd(v) for v in values) or "، ".join(m.text for m in d.amount_mentions)
            self._ask(s, out, "amount", "ask_amount_multiple", M.ASK_AMOUNT_MULTIPLE.format(amounts=listed))
            return False
        if issues:
            m = issues[0]
            if m.issue == "maybe_thousands":
                s.suggested_amount = m.suggested
                self._ask(s, out, "amount_thousands", "ask_amount_thousands", M.ask_thousands(m.text, m.suggested))
            elif m.issue == "usd":
                self._ask(s, out, "amount", "ask_amount_usd", M.ASK_AMOUNT_USD)
            elif m.issue == "slang_unit":
                self._ask(s, out, "amount", "ask_amount_slang", M.ASK_AMOUNT_SLANG.format(text=m.text))
            elif m.issue == "all_balance":
                self._ask(s, out, "amount", "ask_amount_all_balance", M.ASK_AMOUNT_ALL)
            else:
                self._ask(s, out, "amount", "ask_amount_negative", M.ASK_AMOUNT_NEGATIVE)
            return False
        if clear:
            d.amount = clear[0].value
            return True
        if required:
            self._ask(s, out, "amount", "ask_amount", M.ASK_AMOUNT)
        return False

    # ── البطاقة ──────────────────────────────────────────────────────────────
    async def _quote_and_confirm(self, s: Session, d: Draft, out: list) -> None:
        """يسأل المحفظة "شنو راح يصير؟" (quote). إذا مرفوض → رسالة فشل صادقة
        **قبل** التأكيد. إذا تمام → بطاقة تأكيد بكل التفاصيل."""
        if d.intent == "transfer":
            body = {"amount": d.amount}
            body.update({"to_contact_id": d.contact["id"]} if d.contact.get("id") else {"to_phone": d.contact["phone"]})
            kind = "transfer"
        else:
            body = {"amount": d.amount, "bill_account_id": d.bill_account["id"]}
            kind = "bill_payment"
        q = await self.wallet.quote(s.user_id, {"kind": kind, **body})
        if not q["ok"]:
            code = q["error"]["code"]
            ctx = {"balance": q.get("balance"), "total": d.amount}
            if d.contact:
                ctx["name"] = d.contact["name"]
            if d.bill_account:
                ctx.update(biller=d.bill_account["biller_name"], label=d.bill_account["label"],
                           due=d.bill_account["due_amount"])
            self._say(out, code.lower(), "error", M.failure_text(code, **ctx), balance=q.get("balance"))
            await self._finish_draft(s, out)
            return

        warnings = []
        if kind == "transfer":
            history = await self.wallet.transactions(s.user_id, limit=100)
            if not any(t["type"] == "transfer_out" and t["counterparty_ref"] == d.contact["phone"] for t in history):
                warnings.append("أول مرة تحوّل لهذا الشخص — تأكد من الاسم والرقم.")
        elif d.amount < d.bill_account["due_amount"]:
            warnings.append(f"دفع جزئي — المستحق الكامل {M.iqd(d.bill_account['due_amount'])}.")
        if q["total"] > q["balance"] / 2:
            warnings.append("المبلغ أكثر من نص رصيدك.")

        conf_id = uuid.uuid4().hex
        expires = time.time() + settings.confirmation_ttl_seconds
        card = {
            "id": conf_id, "kind": kind, "amount": q["amount"], "fee": q["fee"], "total": q["total"],
            "balance": q["balance"], "balance_after": q["balance_after"], "warnings": warnings,
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds"),
            "expires_in_seconds": settings.confirmation_ttl_seconds,
        }
        if kind == "transfer":
            card.update(recipient_name=q["recipient"]["name"], recipient_relation=d.contact.get("relation"),
                        recipient_phone_masked=mask_phone(d.contact["phone"]))
        else:
            b = q["bill"]
            card.update(biller=b["biller"], label=b["label"], account_no=b["account_no"],
                        category=CATEGORY_NAMES.get(b["category"], b["category"]),
                        due_amount=b["due_amount"], is_full_due=q["amount"] == b["due_amount"])
        conf = Confirmation(conf_id, kind, body, card, copy.deepcopy(d), expires)
        s.pending = conf
        s.confirmations[conf_id] = conf
        s.awaiting, s.options, s.retries = "confirm", [], 0
        self._say(out, "confirm_transfer" if kind == "transfer" else "confirm_bill", "confirmation",
                  M.confirm_text(card), confirmation=card)

    # ── التنفيذ ──────────────────────────────────────────────────────────────
    async def confirm(self, s: Session, confirmation_id: str) -> list:
        out: list = []
        s.log.append({"role": "user", "confirm": confirmation_id})
        await self._execute(s, confirmation_id, out)
        s.log.append({"role": "agent", "codes": [m["code"] for m in out]})
        return out

    async def _execute(self, s: Session, conf_id: str, out: list) -> None:
        conf = s.confirmations.get(conf_id)
        if conf is not None and conf.status == "completed":
            # ضغطة ثانية/إعادة إرسال على عملية منفّذة → نرجّع نفس النتيجة، ما ننفّذ.
            self._say(out, "already_executed", "result",
                      M.ALREADY_EXECUTED.format(tx=conf.result["transaction"]["id"]),
                      transaction=conf.result["transaction"])
            return
        if conf is None or conf is not s.pending or conf.status != "pending":
            self._say(out, "stale_confirmation", "error", M.STALE_CONFIRMATION)
            return
        if time.time() > conf.expires_at and not conf.attempted:
            # بطاقة قديمة: الرصيد/المستحق ممكن تغيّر → بطاقة جديدة بأرقام جديدة.
            # (بعد أول محاولة تنفيذ ما نسوي هذا أبداً: لازم نفس المفتاح حتى ما يتكرر الخصم.)
            conf.status = "expired"
            s.pending = None
            self._say(out, "confirmation_expired", "info", M.EXPIRED)
            await self._quote_and_confirm(s, copy.deepcopy(conf.draft), out)
            return

        conf.attempted = True
        res = await self.wallet.execute(s.user_id, conf.kind, conf.body, key=conf.id)
        if res.outcome == "completed":
            conf.status, conf.result = "completed", res.data
            s.pending = None
            self._say(out, "executed", "result", M.executed_text(conf.card, res.data, res.replayed),
                      transaction=res.data["transaction"], balance=res.data.get("balance"))
            await self._finish_draft(s, out)
        elif res.outcome == "rejected":
            conf.status = "rejected"
            s.pending = None
            d = conf.draft
            ctx = {"total": conf.card["total"], "balance": conf.card["balance"]}
            if d.contact:
                ctx["name"] = d.contact["name"]
            if d.bill_account:
                ctx.update(biller=d.bill_account["biller_name"], label=d.bill_account["label"],
                           due=d.bill_account["due_amount"])
            self._say(out, res.error_code.lower(), "error", M.failure_text(res.error_code, **ctx))
            await self._finish_draft(s, out)
        elif res.outcome == "not_done":
            self._say(out, "wallet_unavailable", "error", M.NOT_DONE, confirmation=conf.card)
        else:
            self._say(out, "payment_status_unknown", "error", M.STATUS_UNKNOWN, confirmation=conf.card)

    async def cancel(self, s: Session, confirmation_id: Optional[str] = None, everything: bool = False) -> list:
        out: list = []
        await self._cancel(s, out, confirmation_id, everything)
        return out

    async def _cancel(self, s: Session, out: list, confirmation_id=None, everything=False) -> None:
        if s.pending and confirmation_id and s.pending.id != confirmation_id:
            self._say(out, "stale_confirmation", "error", M.STALE_CONFIRMATION)
            return
        if not s.pending and not s.draft:
            self._say(out, "nothing_pending", "info", M.NOTHING_PENDING)
            return
        if s.pending:
            s.pending.status = "cancelled"
            s.pending = None
        if everything:
            s.queue = []
        self._say(out, "cancelled", "info", M.CANCELLED_ALL if everything else M.CANCELLED)
        await self._finish_draft(s, out)

    # ── أجوبة المستخدم ──────────────────────────────────────────────────────
    async def _reply_to_confirmation(self, s: Session, text: str, out: list) -> None:
        norm = normalize(text)
        toks = tokens(norm)
        if _is_yes(toks, s.pending.kind):
            await self._execute(s, s.pending.id, out)
            return
        amounts = [m for m in parse_amounts(text) if m.value is not None and not m.issue]
        new_intent = nlu.parse_rules(text).requests[0].intent
        draft = s.pending.draft
        if _is_no(norm, toks):
            if amounts and new_intent not in ("transfer", "pay_bill"):
                # "لا، خليها 30 الف" → نلغي البطاقة ونسوي وحدة جديدة بالمبلغ الجديد.
                s.pending.status, s.pending = "cancelled", None
                self._say(out, "amount_changed", "info", "تمام، غيّرت المبلغ. هذا التأكيد الجديد:")
                d = copy.deepcopy(draft)
                d.amount = amounts[0].value
                await self._quote_and_confirm(s, d, out)
                return
            await self._cancel(s, out, everything=bool(set(toks) & _ALL_WORDS))
            return
        if new_intent in ("transfer", "pay_bill"):
            # طلب جديد بدل الرد على التأكيد → نلغي القديم بوضوح ونبدأ الجديد.
            s.pending.status, s.pending = "cancelled", None
            s.queue = []
            self._say(out, "previous_cancelled", "info", "لغيت العملية اللي جانت تنتظر التأكيد، وبدأت بطلبك الجديد.")
            s.draft, s.awaiting = None, None
            await self._new_request(s, text, out)
            return
        if new_intent in ("balance", "history"):
            await self._new_request(s, text, out)          # قراءة فقط، البطاقة تبقى تنتظر
            self._say(out, "ask_confirm_again", "question", M.CONFIRM_UNCLEAR, confirmation=s.pending.card)
            return
        if amounts:
            s.pending.status, s.pending = "cancelled", None
            self._say(out, "amount_changed", "info", "تمام، غيّرت المبلغ. هذا التأكيد الجديد:")
            d = copy.deepcopy(draft)
            d.amount = amounts[0].value
            await self._quote_and_confirm(s, d, out)
            return
        # أي شي ثاني ("يمكن"، "شنو يعني"، إيموجي...) مو موافقة.
        self._say(out, "ask_confirm_again", "question", M.CONFIRM_UNCLEAR, confirmation=s.pending.card)

    def _pick_option(self, s: Session, text: str) -> Optional[dict]:
        """يطابق جواب المستخدم على خيار: رقم ("2")، ترتيب ("الثاني")، أو اسم يميّز خياراً واحداً."""
        toks = tokens(normalize(text))
        for t in toks:
            if t.isdigit() and 1 <= int(t) <= len(s.options) and len(toks) <= 3:
                return s.options[int(t) - 1]
            for v in token_variants(t):
                if v in _ORDINALS and _ORDINALS[v] < len(s.options):
                    return s.options[_ORDINALS[v]]
        if s.awaiting == "contact":
            m = resolve_contact(text, [o["contact"] for o in s.options])
            if m.status == "exact":
                return next(o for o in s.options if o["contact"] is m.candidates[0])
        else:
            # فئة مذكورة بالجواب ("المولدة") تضيّق الخيارات، بعدها الوصف ("البيت").
            cats = find_categories(text)
            pool = [o for o in s.options if o["account"]["category"] in cats] if cats else s.options
            if len(pool) == 1:
                return pool[0]
            m = resolve_bill_account(text, None, [o["account"] for o in pool])
            if m.status == "exact":
                return next(o for o in pool if o["account"] is m.candidates[0])
        # احتياط: الجواب جزء من الاسم ("كريم") أو آخر أرقام الهاتف ("0002"). نختار
        # فقط إذا خيار واحد يطابق أكثر من غيره — وإلا نعيد السؤال.
        answer = {v for t in toks if t not in STOPWORDS for v in token_variants(t)}
        scores = []
        for o in s.options:
            label_tokens = {v for t in tokens(normalize(o["label"])) for v in token_variants(t)}
            phone = (o.get("contact") or {}).get("phone", "")
            score = len(answer & label_tokens) + sum(1 for t in toks if len(t) >= 4 and t.isdigit() and phone.endswith(t))
            scores.append(score)
        best = max(scores, default=0)
        if best > 0 and scores.count(best) == 1:
            return s.options[scores.index(best)]
        return None

    async def _apply_option(self, s: Session, opt: dict, out: list) -> None:
        d = s.draft
        if s.awaiting == "contact":
            d.contact = opt["contact"]
        else:
            d.bill_account = opt["account"]
            d.category = opt["account"]["category"]
        s.awaiting, s.options, s.retries = None, [], 0
        await self._advance(s, d, out)

    async def _answer_option(self, s: Session, option_id: str, out: list) -> None:
        opt = next((o for o in s.options if o["id"] == option_id), None)
        if opt is None or not s.draft:
            self._say(out, "stale_option", "error", "هذا الخيار مو للسؤال الحالي. ما نفّذت شي.")
            return
        await self._apply_option(s, opt, out)

    async def _reply_to_question(self, s: Session, text: str, out: list) -> None:
        norm = normalize(text)
        toks = tokens(norm)
        d = s.draft
        if _is_no(norm, toks) and not parse_amounts(text):
            await self._cancel(s, out, everything=bool(set(toks) & _ALL_WORDS))
            return

        if s.awaiting in ("contact", "bill_account"):
            # خيار واحد ("تقصد مرتضى؟") + "اي" = اختياره. أكثر من خيار + "اي" = مو جواب.
            opt = s.options[0] if (len(s.options) == 1 and _is_yes(toks)) else self._pick_option(s, text)
            if opt:
                await self._apply_option(s, opt, out)
                return
        elif s.awaiting == "recipient":
            phone = find_phone(text)
            new_intent = nlu.parse_rules(text).requests[0].intent
            if phone or new_intent == "unknown":
                d.phone, d.recipient_text, d.contact = phone, text, None
                if not phone:
                    match = resolve_contact(text, await self.wallet.contacts(s.user_id))
                    if match.status == "none":
                        s.retries += 1
                        if s.retries >= 2:
                            self._say(out, "gave_up", "error", M.GIVE_UP)
                            await self._finish_draft(s, out)
                            return
                s.awaiting = None
                await self._advance(s, d, out)
                return
        elif s.awaiting == "amount_thousands" and _is_yes(toks):
            d.amount = s.suggested_amount
            s.awaiting = None
            await self._advance(s, d, out)
            return
        elif s.awaiting in ("amount", "amount_thousands"):
            mentions = parse_amounts(text)
            if mentions and nlu.parse_rules(text).requests[0].intent not in ("transfer", "pay_bill"):
                d.amount_mentions, d.amount = mentions, None
                s.awaiting = None
                await self._advance(s, d, out)
                return

        # ما كان جواب للسؤال — يمكن طلب جديد؟
        if nlu.parse_rules(text).requests[0].intent in ("transfer", "pay_bill", "balance", "history"):
            if nlu.parse_rules(text).requests[0].intent in ("transfer", "pay_bill"):
                self._say(out, "previous_dropped", "info", "تركت الطلب السابق وبدأت بطلبك الجديد.")
                s.draft, s.awaiting, s.options, s.queue = None, None, [], []
            await self._new_request(s, text, out)
            return
        s.retries += 1
        if s.retries >= 3:
            self._say(out, "gave_up", "error", M.GIVE_UP)
            await self._finish_draft(s, out)
            return
        self._say(out, "ask_again", "question", "ما فهمت جوابك. " + (
            "اختار رقم من الخيارات." if s.options else "ممكن توضّح؟"),
            options=[{"id": o["id"], "label": o["label"]} for o in s.options] or None)
