# -*- coding: utf-8 -*-
"""مجموعة الاختبار الكاملة (data/test_requests.jsonl) كاختبار واحد: كل الحالات
لازم تنجح، وصفر تنفيذ غير آمن."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from eval.run_eval import check, run_case

CASES = [json.loads(l) for l in (Path(__file__).resolve().parent.parent / "data" / "test_requests.jsonl")
         .read_text(encoding="utf-8").splitlines() if l.strip()]


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


def test_test_set_has_at_least_50_cases():
    assert len(CASES) >= 50


def test_all_cases_pass_and_nothing_unsafe_executes():
    failures, unsafe = [], []
    with TestClient(app) as client:
        api = _Api(client)
        for case in CASES:
            transcript, executed = run_case(api, case)
            passed, problems, is_unsafe = check(case, transcript, executed)
            if not passed:
                failures.append((case["id"], problems))
            if is_unsafe:
                unsafe.append(case["id"])
    assert unsafe == []
    assert failures == []
