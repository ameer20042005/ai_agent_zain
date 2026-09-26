# -*- coding: utf-8 -*-
"""مطابقة "لمن؟" و"أي فاتورة؟" مع بيانات المستخدم الحقيقية.

القاعدة الذهبية: **نقبل فقط المطابقة الأكيدة**. أي شي ثاني (اسمين أحمد، اسم
مكتوب غلط، حسابين كهرباء) يرجع كخيارات والوكيل يسأل — ما نختار عشوائياً.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.agent.textnorm import normalize, similarity, token_variants, tokens

# كلمات ما تكون أسماء أشخاص أبداً — نستثنيها من البحث عن اسم مكتوب غلط.
STOPWORDS = {
    "دز", "حول", "حولي", "ارسل", "ابعث", "بعث", "انطي", "اعطي", "نطي", "ادفع", "سدد", "اسدد",
    "خلص", "دفع", "الف", "الاف", "تلاف", "مليون", "دينار", "على", "الى", "الي", "من", "فضلك",
    "رجاء", "رجاءا", "لو", "سمحت", "اريد", "ابي", "ابغي", "بدي", "خلي", "هسه", "هسع", "اليوم",
    "حبيبي", "عيوني", "بسرعه", "ممكن", "تكدر", "تكرم", "الله", "يخليك", "ياريت", "حساب",
    "محفظه", "رقم", "فلوس", "مبلغ", "قائمه", "فاتوره", "اشتراك", "حق", "مال", "مالت", "مالتي",
    "كهرباء", "كهربا", "مولده", "ماي", "ماء", "انترنت", "نت", "البيت", "بيت", "المحل", "محل",
    "اكد", "نعم", "اي", "لا", "الغي", "و", "بس", "هم", "كمان", "بعدين", "شكد", "كم", "اخوي",
}


def mask_phone(phone: str) -> str:
    """0770•••0002 — يكفي للتمييز بين شخصين بدون كشف الرقم كامل."""
    return f"{phone[:4]}•••{phone[-4:]}" if phone and len(phone) >= 8 else (phone or "")


def _phrase_in(phrase: str, seg_tokens: List[str]) -> int:
    """هل عبارة (اسم/لقب) موجودة بالنص ككلمات متتالية؟ يرجّع عدد كلماتها المتطابقة
    من البداية (0 = ولا كلمة). يطابق مع نزع السوابق: "لاحمد" = "احمد"."""
    words = tokens(normalize(phrase))
    best = 0
    for start in range(len(seg_tokens)):
        n = 0
        while (n < len(words) and start + n < len(seg_tokens)
               and token_variants(seg_tokens[start + n]) & token_variants(words[n])):
            n += 1
        best = max(best, n)
    return best


@dataclass
class ContactMatch:
    """نتيجة البحث عن مستلم.
    status: exact (واحد أكيد) | ambiguous (أكثر من واحد) | fuzzy (يشبه — نسأل "تقصد؟")
            | none (ما موجود)"""
    status: str
    candidates: List[dict] = field(default_factory=list)
    unknown_text: Optional[str] = None


def resolve_contact(text: str, contacts: List[dict]) -> ContactMatch:
    seg = tokens(normalize(text))
    scored = []
    for c in contacts:
        name_words = tokens(normalize(c["name"]))
        matched = _phrase_in(c["name"], seg)
        # الشرح: مطابقة الاسم لازم تبدأ من الاسم الأول ("علي حسين" ما يطابق
        # "أحمد علي حسين" لأن أحمد مو مذكور). النتيجة = عدد الكلمات المتطابقة.
        score = matched if matched and any(token_variants(t) & token_variants(name_words[0]) for t in seg) else 0
        if c.get("nickname") and _phrase_in(c["nickname"], seg) == len(tokens(normalize(c["nickname"]))):
            score = max(score, 10)                       # لقب كامل ("حمودي"، "ابو حسين")
        rel = c.get("relation")
        if rel and (_phrase_in(rel, seg) == len(tokens(normalize(rel)))
                    or _phrase_in(rel + "ي", seg) == len(tokens(normalize(rel)))):
            score = max(score, 10)                       # صلة ("امي"، "اخوي"، "جاري")
        if score:
            scored.append((score, c))

    if scored:
        top = max(s for s, _ in scored)
        best = [c for s, c in scored if s == top]
        return ContactMatch("exact" if len(best) == 1 else "ambiguous", best)

    # الشرح: ولا مطابقة أكيدة → نبحث عن أسماء تشبه كلمة بالنص (خطأ إملائي مثل
    # "مرتظى"). حتى لو لگينا شبيه واحد **ما نختاره** — نسأل "تقصد فلان؟".
    fuzzy = []
    candidates_words = [t for t in seg if t not in STOPWORDS and not re.search(r"\d", t) and len(t) >= 3]
    for c in contacts:
        first = tokens(normalize(c["name"]))[0]
        if any(max(similarity(v, first) for v in token_variants(w)) >= 0.75 for w in candidates_words):
            fuzzy.append(c)
    if fuzzy:
        return ContactMatch("fuzzy", fuzzy)
    return ContactMatch("none", [], " ".join(candidates_words) or None)


def find_phone(text: str) -> Optional[str]:
    """رقم هاتف عراقي بالنص (07XXXXXXXXX أو +9647...) بصيغة 07 موحّدة."""
    norm = normalize(text).replace(" ", "").replace("-", "")
    m = re.search(r"(?:\+?964|00964)(7\d{9})|(07\d{9})", norm)
    if not m:
        return None
    return "0" + m.group(1) if m.group(1) else m.group(2)


# ---------------------------------------------------------------------------
# الفواتير
# ---------------------------------------------------------------------------

# الشرح: كلمات كل فئة فاتورة بالعامية العراقية. "قائمة" = فاتورة بالعراقي.
CATEGORY_WORDS = {
    "electricity": ["كهرباء", "كهربا", "كهربه", "الكهربائيه", "الوطنيه"],
    "generator": ["مولده", "مولد", "امبير", "امبيرات", "الامبيرات"],
    "water": ["ماء", "ماي", "مي", "مياه"],
    "internet": ["انترنت", "انترنيت", "نت", "وايفاي", "واي فاي", "فايبر"],
}
CATEGORY_NAMES = {"electricity": "الكهرباء", "generator": "المولدة", "water": "الماء", "internet": "الإنترنت"}


def find_categories(text: str) -> List[str]:
    """كل فئات الفواتير المذكورة بالنص، بترتيب ظهورها."""
    seg = tokens(normalize(text))
    found = []
    for i, tok in enumerate(seg):
        variants = token_variants(tok)
        for cat, words in CATEGORY_WORDS.items():
            if any(w in variants for w in words) and cat not in found:
                found.append(cat)
    return found


@dataclass
class BillMatch:
    status: str                       # exact | ambiguous | none
    candidates: List[dict] = field(default_factory=list)


def resolve_bill_account(text: str, category: Optional[str], accounts: List[dict]) -> BillMatch:
    """يختار حساب الفاتورة: حسب الفئة، وإذا أكثر من حساب بنفس الفئة حسب الوصف
    ("البيت"/"المحل"). بدون فئة = كل الحسابات خيارات."""
    pool = [a for a in accounts if a["category"] == category] if category else list(accounts)
    if not pool:
        return BillMatch("none")
    if len(pool) == 1 and category:
        return BillMatch("exact", pool)
    seg = tokens(normalize(text))
    by_label = [a for a in pool if _phrase_in(a["label"], seg) == len(tokens(normalize(a["label"])))]
    if len(by_label) == 1 and (category or len({a["category"] for a in by_label}) == 1):
        return BillMatch("exact", by_label)
    return BillMatch("ambiguous", by_label if len(by_label) > 1 else pool)
