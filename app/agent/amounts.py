# -*- coding: utf-8 -*-
"""استخراج المبالغ من كلام عراقي: "50 الف"، "خمسين الف"، "ربع مليون"، "مليون ونص"،
"خمس تلاف"، "٢٥٠٠٠"، "50,000".

المبلغ أخطر حقل بالطلب، لذلك **يُحسب بالكود دائماً** — حتى لما الموديل يستخرج
النية، الرقم النهائي يطلع من هنا (والموديل يعطينا فقط النص الحرفي للمبلغ).
الحالات الغامضة ما نخمّنها؛ نرجعها كـ issue والوكيل يسأل عنها.
"""

from dataclasses import dataclass
from typing import List, Optional

from app.agent.textnorm import normalize, tokens

# الشرح: قاموس كلمات الأرقام بعد التطبيع (ة→ه، أ→ا). يشمل الصيغ العراقية
# المحكية مثل "خمسطعش" (15)، "ميتين" (200)، "تلاف" (آلاف).
_UNITS = {
    "صفر": 0, "واحد": 1, "وحده": 1, "اثنين": 2, "ثنين": 2, "اثنان": 2,
    "ثلاث": 3, "ثلاثه": 3, "تلاث": 3, "تلاثه": 3, "اربع": 4, "اربعه": 4,
    "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7,
    "ثمان": 8, "ثمانيه": 8, "ثمنيه": 8, "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10,
    "احدعش": 11, "حدعش": 11, "اثنعش": 12, "ثنعش": 12, "ثلطعش": 13, "تلطعش": 13,
    "اربعطعش": 14, "خمسطعش": 15, "ستطعش": 16, "سبعطعش": 17, "ثمنطعش": 18, "تسعطعش": 19,
    "عشرين": 20, "ثلاثين": 30, "تلاثين": 30, "اربعين": 40, "خمسين": 50,
    "ستين": 60, "سبعين": 70, "ثمانين": 80, "ثمنين": 80, "تسعين": 90,
    "ميه": 100, "مئه": 100, "مايه": 100, "ميت": 100, "ميتين": 200, "مئتين": 200, "ميتان": 200,
}
for _stem, _v in (("ثلاث", 3), ("ثلث", 3), ("تلث", 3), ("اربع", 4), ("ربع", 4), ("خمس", 5),
                  ("ست", 6), ("سبع", 7), ("ثمن", 8), ("ثمان", 8), ("تسع", 9)):
    for _suffix in ("ميه", "مئه", "مايه"):
        _UNITS[_stem + _suffix] = _v * 100

_MULTIPLIERS = {
    "الف": 1_000, "الاف": 1_000, "تلاف": 1_000, "تالاف": 1_000, "k": 1_000,
    "مليون": 1_000_000, "ملايين": 1_000_000, "ملاين": 1_000_000,
}
# كلمات هي رقم كامل بنفسها (مثنى): الفين = 2000، مليونين = 2,000,000.
_DUALS = {"الفين": 2_000, "مليونين": 2_000_000}
_FRACTIONS = {"ربع": 0.25, "نص": 0.5, "نصف": 0.5}

# الشرح: كلمات خطرة نرفض تفسيرها تلقائياً:
#   - "ورقة/دفتر": عامية عراقية (ورقة = 100 دولار غالباً، دفتر = 10 آلاف دولار)
#     لكن الاستعمال يختلف بين الناس → نسأل.
#   - الدولار: المحفظة بالدينار فقط → ما نحوّل عملة ضمنياً.
#   - "كل رصيدي": نطلب رقماً صريحاً بدل ما نفرّغ المحفظة.
_SLANG_UNITS = {"ورقه", "ورقات", "اوراق", "ورقتين", "دفتر", "دفاتر", "دفترين"}
_USD_WORDS = {"دولار", "دولارات", "$", "usd"}
_ALL_BALANCE = ("كل رصيدي", "كل فلوسي", "كل الرصيد", "كل المبلغ", "كل اللي عندي")
_NEGATIVE_WORDS = {"سالب", "ناقص"}


@dataclass
class AmountMention:
    """مبلغ واحد مذكور بالنص.

    value: المبلغ بالدينار (None إذا ما نكدر نحدده بأمان).
    issue: None إذا المبلغ واضح، وإلا رمز المشكلة اللي لازم نسأل عنها:
        maybe_thousands  رقم صغير بلا "الف" (خمسين = 50 أو 50 الف؟)
        slang_unit       ورقة/دفتر
        usd              العملة دولار
        all_balance      "كل رصيدي"
        negative         مبلغ سالب
    suggested: اقتراح نعرضه بالسؤال (مثلاً 50000 لحالة maybe_thousands).
    """
    value: Optional[int]
    text: str
    issue: Optional[str] = None
    suggested: Optional[int] = None


def _strip_joiner(tok: str) -> str:
    """"وخمسين" → "خمسين"، "ب50" → "50": نزع و/ب الملتصقة برقم."""
    for p in ("و", "ب"):
        rest = tok[len(p):]
        if tok.startswith(p) and (rest in _UNITS or rest in _MULTIPLIERS or rest in _DUALS
                                  or rest in _FRACTIONS or _is_digit(rest)):
            return rest
    return tok


def _is_digit(tok: str) -> bool:
    t = tok[1:] if tok.startswith("-") else tok
    return bool(t) and t.replace(",", "").replace(".", "", 1).isdigit()


def _is_phone(tok: str) -> bool:
    """أرقام الهواتف العراقية (07xxxxxxxxx / 9647...) مو مبالغ."""
    t = tok.replace(",", "")
    return t.isdigit() and ((t.startswith("07") and len(t) == 11) or (t.startswith("9647") and len(t) >= 12))


def _is_numberish(tok: str) -> bool:
    t = _strip_joiner(tok)
    return (t in _UNITS or t in _MULTIPLIERS or t in _DUALS or t in _FRACTIONS
            or (_is_digit(t) and not _is_phone(t)))


def is_number_token(tok: str) -> bool:
    """هل الكلمة جزء من مبلغ؟ (رقم، كلمة رقم، الف/مليون...) — يستعمله تقسيم الطلبات."""
    return _is_numberish(tok)


def _evaluate(span: List[str]) -> tuple:
    """يحسب قيمة سلسلة كلمات رقمية. يرجّع (القيمة، هل بيها مضاعِف مثل الف/مليون).

    الفكرة: نجمع "المجموعة الحالية" (مثلاً مية وخمسين = 150)، ولما يجي مضاعِف
    (الف) نضربها بيه ونضيفها للمجموع. هيچ "مليون وخمسمية الف" = 1,000,000 + 500×1000."""
    total, current = 0.0, 0.0
    pending_fraction = None
    last_mult = None
    has_mult = False
    for raw in span:
        tok = _strip_joiner(raw)
        if _is_digit(tok):
            current += float(tok.replace(",", ""))
        elif tok in _UNITS:
            current += _UNITS[tok]
        elif tok in _DUALS:
            total += _DUALS[tok]
            has_mult = True
        elif tok in _FRACTIONS:
            if current == 0 and last_mult:          # "مليون ونص" → نص المضاعِف السابق
                total += _FRACTIONS[tok] * last_mult
            else:                                   # "ربع مليون" → ينتظر المضاعِف
                pending_fraction = _FRACTIONS[tok]
        elif tok in _MULTIPLIERS:
            mult = _MULTIPLIERS[tok]
            has_mult = True
            if pending_fraction is not None:
                total += pending_fraction * mult
                pending_fraction = None
            else:
                total += (current or 1) * mult
            current = 0.0
            last_mult = mult
    total += current
    return total, has_mult


def _max_mult(words: List[str]) -> int:
    mults = [_MULTIPLIERS.get(_strip_joiner(w)) or _DUALS.get(_strip_joiner(w)) or 0 for w in words]
    return max(mults, default=0)


def _split_groups(span: list, joins: List[int]) -> List[list]:
    """يقطع سلسلة عند "و" إذا المجموعة اللي بعدها **مو أصغر** رتبةً من اللي قبلها:
    "مليون | وخمسمية الف" → مبلغ واحد (الف < مليون)،
    "20 الف | و 30 الف" → مبلغين (الف = الف)،
    "مية | وخمسين الف" → مبلغ واحد (المجموعة الأولى بلا مضاعِف تشارك الف اللي بعدها)."""
    bounds = [0] + joins + [len(span)]
    groups = [span[bounds[k]:bounds[k + 1]] for k in range(len(bounds) - 1)]
    out = [groups[0]]
    for g in groups[1:]:
        prev_mult = _max_mult([t for _, t in out[-1]])
        if prev_mult == 0 or _max_mult([t for _, t in g]) < prev_mult:
            out[-1] = out[-1] + g
        else:
            out.append(g)
    return out


def parse_amounts(text: str) -> List[AmountMention]:
    """يرجّع كل المبالغ المذكورة بالنص (عادة وحد). أكثر من مبلغ = غموض يُسأل عنه."""
    norm = normalize(text)
    toks = tokens(norm)
    mentions: List[AmountMention] = []

    # الشرح: نجمع الكلمات الرقمية المتتالية بسلاسل (spans). كلمة "و" المنفصلة
    # تبقى داخل السلسلة فقط إذا بعدها رقم ("مية و خمسين"). نسجّل وين تبدأ كل
    # "مجموعة" بعد و، حتى نفرّق "مليون وخمسمية الف" (مبلغ واحد) عن
    # "20 الف و 30 الف" (مبلغين).
    spans, cur, joins = [], [], []
    for i, tok in enumerate(toks):
        if _is_numberish(tok):
            if cur and (toks[i - 1] == "و" or (tok.startswith("و") and _strip_joiner(tok) != tok)):
                joins.append(len(cur))
            cur.append((i, tok))
        elif tok == "و" and cur and i + 1 < len(toks) and _is_numberish(toks[i + 1]):
            continue
        else:
            if cur:
                spans.extend(_split_groups(cur, joins))
            cur, joins = [], []
    if cur:
        spans.extend(_split_groups(cur, joins))

    for span in spans:
        words = [t for _, t in span]
        # كلمة رقم مفردة صغيرة (ست، خمس...) بلا مضاعِف غالباً مو مبلغ ("ست زينب").
        if len(words) == 1 and _strip_joiner(words[0]) in _UNITS and 0 < _UNITS[_strip_joiner(words[0])] <= 10:
            continue
        # كلمة "ربع/نص" وحدها بلا مضاعِف مو مبلغ.
        if all(_strip_joiner(w) in _FRACTIONS for w in words):
            continue
        value, has_mult = _evaluate(words)
        first_idx, last_idx = span[0][0], span[-1][0]
        before = toks[first_idx - 1] if first_idx > 0 else ""
        after = toks[last_idx + 1] if last_idx + 1 < len(toks) else ""
        span_text = " ".join(words)

        if before in _NEGATIVE_WORDS or value < 0 or any(w.startswith("-") for w in words):
            mentions.append(AmountMention(None, span_text, "negative"))
        elif after in _USD_WORDS or before in _USD_WORDS:
            mentions.append(AmountMention(None, span_text, "usd"))
        elif after in _SLANG_UNITS:
            mentions.append(AmountMention(None, span_text + " " + after, "slang_unit"))
        elif not has_mult and 0 < value < 1000 and after not in ("دينار", "د.ع"):
            # "دز خمسين لأحمد": بالعراقي غالباً يقصد خمسين الف، بس ما نفترض → نسأل.
            mentions.append(AmountMention(None, span_text, "maybe_thousands", int(value * 1000)))
        else:
            mentions.append(AmountMention(int(round(value)), span_text))

    # حالات بدون أي رقم لكنها تخص المبلغ.
    if any(p in norm for p in _ALL_BALANCE):
        mentions.append(AmountMention(None, "كل الرصيد", "all_balance"))
    if not mentions and any(w in toks for w in _SLANG_UNITS):
        mentions.append(AmountMention(None, "ورقة/دفتر", "slang_unit"))
    return mentions
