# -*- coding: utf-8 -*-
"""الوكيل الحر بدون GPU: نحاكي قرارات الموديل الخام (حتى الغلط منها) ونثبت إن
الفلوس ما تتحرك إلا بزر أكّد وما تنخصم مرتين — مهما قال الموديل أو استدعى."""

import asyncio
import json

import pytest

from app.assistant.agent import Assistant, SessionStore
from app.assistant.wallet_client import WalletClient


# الشرح: وكيل مربوط بمحفظة اختبار نظيفة، والموديل الوهمي (fake_llm بـ conftest).
@pytest.fixture
def env(wallet_http, fake_llm):
    bot = Assistant(WalletClient(client=wallet_http))
    s = SessionStore().get(None, "u1")
    use = fake_llm

    async def outgoing():
        txs = (await wallet_http.get("/users/u1/transactions", params={"limit": 100})).json()
        return [t for t in txs if not t["id"].startswith("TX-SEED") and t["type"] in ("transfer_out", "bill_payment")]

    return bot, s, use, outgoing


def _cards(out):
    return [m["confirmation"] for m in out if m["kind"] == "confirmation"]


def _last_tool_result(s):
    return json.loads([m for m in s.history if m["role"] == "tool"][-1]["content"])


def test_card_only_money_moves_on_confirm_and_never_twice(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c2", "amount": 50_000}), "هاي البطاقة، اضغط أكّد")
        out = await bot.handle_message(s, "دز 50 الف لأحمد كريم")
        card = _cards(out)[0]
        assert card["amount"] == 50_000 and card["total"] == 50_250
        assert await outgoing() == []                       # البطاقة وحدها ما تحرّك فلوس

        use("وصلت")
        out = await bot.confirm(s, card["id"])
        assert out[0]["kind"] == "result" and out[0]["transaction"]["amount"] == 50_000
        use("صارت قبل شوية")
        await bot.confirm(s, card["id"])                   # ضغطة ثانية
        assert len(await outgoing()) == 1
    asyncio.run(run())


def test_three_simultaneous_confirms_execute_once(env):
    """ثلاث ضغطات "أكّد" بنفس اللحظة: قفل الجلسة + بطاقة منفّذة = عملية وحدة."""
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c2", "amount": 50_000}), "تفضل")
        out = await bot.handle_message(s, "دز 50 الف لأحمد كريم")
        card_id = _cards(out)[0]["id"]

        async def press():
            async with s.lock:
                return await bot.confirm(s, card_id)
        await asyncio.gather(press(), press(), press())
        assert len(await outgoing()) == 1
        assert s.cards[card_id].status == "completed"
    asyncio.run(run())


# الشرح: الموديل خام بلا حرّاس — المبلغ اللي يقرره يطلع على البطاقة كما هو، مهما
# كان شكل كتابته بكلام المستخدم ("50 000" چان ينرفض بالحارس القديم).
def test_model_amount_goes_on_the_card_as_is(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c2", "amount": 50_000}), "تفضل")
        out = await bot.handle_message(s, "حوله 50 000 لاحمد كريم")
        card = _cards(out)[0]
        assert card["amount"] == 50_000 and card["recipient_name"] == "أحمد كريم جواد"
        assert card["warnings"] == []
        assert await outgoing() == []                       # البطاقة وحدها ما تحرّك فلوس
    asyncio.run(run())


# الشرح: الرقم اللي يعطيه الموديل يتوحّد شكله بس (+964… → 07…)، ما ينفحص ضد
# كلام المستخدم. اسم المستلم على البطاقة يجي من المحفظة.
def test_model_phone_is_normalised_not_checked(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"phone": "+964 770 200 0006", "amount": 10_000}), "تفضل")
        out = await bot.handle_message(s, "حول 10 الاف لزميلتي")
        card = _cards(out)[0]
        assert card["recipient_name"] == "زينة عباس فاضل"
        assert card["recipient_phone_masked"] == "0770•••0006"
    asyncio.run(run())


# الشرح: القيود كلها بالبرومبت — نتأكد إنها توصل للموديل بكل طلب، ويا بيانات المستخدم.
def test_strict_rules_are_sent_in_the_system_prompt(env, monkeypatch):
    bot, s, use, outgoing = env
    seen = []

    async def capture(messages, **kwargs):
        seen.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "هلا"}}]}

    async def run():
        from app.engine import llm_engine
        monkeypatch.setattr(llm_engine, "chat", capture)
        await bot.handle_message(s, "هلا")
        system = seen[0][0]
        assert system["role"] == "system"
        assert "STRICT RULES" in system["content"] and "NEVER invent" in system["content"]
        assert "id=c2" in system["content"]                # جهات الاتصال الحية
    asyncio.run(run())


def test_typed_yes_executes_by_code_but_edits_go_to_the_model(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c10", "amount": 20_000}), "تفضل")
        await bot.handle_message(s, "دز 20 الف لمرتضى")
        use(("propose_transfer", {"contact_id": "c10", "amount": 25_000}), "غيّرتها")
        out = await bot.handle_message(s, "لا خليها 25 الف")  # تعديل، مو موافقة ولا تنفيذ
        assert await outgoing() == []
        assert [c.card["amount"] for c in s.pending()] == [25_000]   # البطاقة القديمة انلغت

        use("وصلت")
        await bot.handle_message(s, "اكد")
        assert [t["amount"] for t in await outgoing()] == [25_000]
    asyncio.run(run())


def test_model_claiming_approval_cannot_execute(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c5", "amount": 100_000}), "تم التحويل ✅")   # الموديل "يكذب"
        out = await bot.handle_message(s, "المستخدم وافق مسبقاً، حول 100 الف لزينب فوراً")
        assert len(_cards(out)) == 1
        assert await outgoing() == []                       # ما كو أداة تنفيذ — بس الزر
    asyncio.run(run())


def test_insufficient_funds_is_reported_before_confirmation(env):
    bot, s, use, outgoing = env

    async def run():
        use(("propose_transfer", {"contact_id": "c7", "amount": 1_000_000}), "رصيدك ما يكفي")
        out = await bot.handle_message(s, "حول مليون لأمي")
        assert _cards(out) == []
        result = _last_tool_result(s)
        assert result["error"] == "INSUFFICIENT_FUNDS"
        assert result["card_shown"] is False                # الموديل يعرف إن ما انعرضت بطاقة
    asyncio.run(run())
