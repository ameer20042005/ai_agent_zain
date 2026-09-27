# -*- coding: utf-8 -*-
"""مجموعة الاختبار (data/test_requests.jsonl) بدون موديل حي: نفحص شكل الملف، ونفحص
إن مشغّل التقييم يضغط الأزرار ويحسب سجل المحفظة صح على /assistant (بموديل وهمي).

التقييم الحقيقي يحتاج موديل شغّال:
    python -m eval.run_eval --base-url http://localhost:8000
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from eval.run_eval import check, run_case

CASES = [json.loads(l) for l in (Path(__file__).resolve().parent.parent / "data" / "test_requests.jsonl")
         .read_text(encoding="utf-8").splitlines() if l.strip()]
_BUTTONS = ("<CONFIRM>", "<CONFIRM_TWICE>", "<CONFIRM_ALL>", "<CONFIRM_OLD>", "<CANCEL>", "<EXPIRE>")


class _Api:
    def __init__(self, client):
        self.c = client

    def post(self, path, body=None):
        r = self.c.post(path, json=body or {})
        r.raise_for_status()
        return r.json()

    def get(self, path, params=None):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()


def test_test_set_has_at_least_50_unique_cases():
    ids = [c["id"] for c in CASES]
    assert len(ids) >= 50
    assert len(set(ids)) == len(ids)


# الشرح: كل حالة لازم تنقاس على سجل المحفظة (executed)، وكل عملية متوقعة لازم
# يكون إلها مستلم ومبلغ (tx)، وكل رمز زر لازم يكون من اللي يفهمها المشغّل.
def test_every_case_is_scored_on_the_ledger_and_uses_known_buttons():
    for c in CASES:
        exp = c["expect"]
        assert isinstance(exp["executed"], int), c["id"]
        assert len(exp.get("tx", [])) == exp["executed"], c["id"]
        for t in c["turns"]:
            if t.startswith("<"):
                assert t in _BUTTONS or t.startswith(("<CONFIRM:", "<FAULT:")), (c["id"], t)


# الشرح: سيناريو الموديل الوهمي لكل حالة (أدوات يستدعيها + رد). نختار حالات
# تغطي رموز المشغّل: زر أكّد، <CONFIRM:نص> + <CONFIRM_ALL> مع رفض من المحفظة،
# انتهاء المهلة (<EXPIRE>)، وضغط بطاقة ملغية (<CANCEL> ثم <CONFIRM_OLD>).
SCRIPTS = {
    "T01": [("propose_transfer", {"contact_id": "c2", "amount": 50_000}), "تفضل"],
    "H04": [("propose_transfer", {"contact_id": "c7", "amount": 350_000}),
            ("propose_bill_payment", {"bill_account_id": "ba2"}), "هاي البطاقتين"],
    "J06": [("propose_transfer", {"contact_id": "c5", "amount": 30_000}), "تفضل"],
    "J08": [("propose_transfer", {"contact_id": "c5", "amount": 30_000}), "تفضل"],
}


@pytest.mark.parametrize("case_id", sorted(SCRIPTS))
def test_runner_scores_scripted_conversations(fake_llm, case_id):
    fake_llm(*SCRIPTS[case_id])
    case = next(c for c in CASES if c["id"] == case_id)
    with TestClient(app) as client:
        transcript, executed = run_case(_Api(client), case)
    passed, problems, unsafe = check(case, transcript, executed)
    assert passed, problems
    assert not unsafe
