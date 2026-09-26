# -*- coding: utf-8 -*-
"""مسار الموديل بدون GPU: نحاكي ردود الموديل ونتأكد إن الكود يتحقق منها."""

import asyncio
import json

import pytest

from app.agent import nlu
from app.engine import llm_engine


def _fake_llm(payload):
    async def chat(messages, **kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"   # guided decoding مطلوب
        return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    return chat


@pytest.fixture
def llm_mode(monkeypatch):
    monkeypatch.setattr(nlu.settings.__class__, "nlu_mode", property(lambda self: "llm"), raising=False)
    yield monkeypatch


def test_llm_splits_and_code_extracts_amounts(llm_mode):
    llm_mode.setattr(llm_engine, "chat", _fake_llm({"requests": [
        {"intent": "pay_bill", "segment": "ادفع قائمة الكهرباء", "bill_category": "electricity", "recipient": None},
        {"intent": "transfer", "segment": "دز 50 الف لأحمد", "bill_category": None, "recipient": "أحمد"},
    ]}))
    r = asyncio.run(nlu.parse("ادفع قائمة الكهرباء ودز 50 الف لأحمد"))
    assert r.parser == "llm"
    assert [q.intent for q in r.requests] == ["pay_bill", "transfer"]
    assert r.requests[0].category == "electricity"
    assert [m.value for m in r.requests[1].amounts] == [50_000]


def test_hallucinated_segment_and_recipient_are_discarded(llm_mode):
    # الموديل "اخترع" مبلغاً ومستلماً مو موجودين بالجملة.
    llm_mode.setattr(llm_engine, "chat", _fake_llm({"requests": [
        {"intent": "transfer", "segment": "دز 900 الف لحيدر", "bill_category": None, "recipient": "حيدر"},
    ]}))
    r = asyncio.run(nlu.parse("دز فلوس لأخوي"))
    req = r.requests[0]
    assert req.text == "دز فلوس لأخوي"          # رجعنا لكلام المستخدم الحرفي
    assert "حيدر" not in (req.recipient_text or "")
    assert req.amounts == []                   # ما كو مبلغ بكلام المستخدم → الوكيل راح يسأل


def test_invalid_llm_output_falls_back_to_rules(llm_mode):
    async def broken(messages, **kwargs):
        return {"choices": [{"message": {"content": "not json"}}]}
    llm_mode.setattr(llm_engine, "chat", broken)
    r = asyncio.run(nlu.parse("دز 50 الف لأحمد"))
    assert r.parser == "rules_fallback"
    assert r.requests[0].intent == "transfer"


def test_unknown_intent_and_category_values_are_sanitised(llm_mode):
    llm_mode.setattr(llm_engine, "chat", _fake_llm({"requests": [
        {"intent": "delete_account", "segment": "ادفع قائمة المولدة", "bill_category": "gas", "recipient": None},
    ]}))
    r = asyncio.run(nlu.parse("ادفع قائمة المولدة"))
    assert r.requests[0].intent == "unknown"
