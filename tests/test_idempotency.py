# -*- coding: utf-8 -*-
"""إثبات إن إعادة المحاولة ما تسبب دفعاً مكرراً — على مستوى المحفظة والوكيل."""

import asyncio

import pytest

from app.agent.core import Agent, SessionStore
from app.agent.wallet_client import WalletClient

TRANSFER = {"amount": 50_000, "to_contact_id": "c1"}


async def _outgoing(client):
    txs = (await client.get("/users/u1/transactions", params={"limit": 100})).json()
    return [t for t in txs if not t["id"].startswith("TX-SEED") and t["type"] == "transfer_out"]


def test_same_key_twice_executes_once(wallet_http):
    async def run():
        h = {"Idempotency-Key": "k-1"}
        r1 = await wallet_http.post("/users/u1/transfers", json=TRANSFER, headers=h)
        r2 = await wallet_http.post("/users/u1/transfers", json=TRANSFER, headers=h)
        assert r1.status_code == r2.status_code == 201
        assert r1.json()["transaction"]["id"] == r2.json()["transaction"]["id"]
        assert r2.json()["replayed"] is True
        assert len(await _outgoing(wallet_http)) == 1
        assert (await wallet_http.get("/users/u1")).json()["balance"] == 400_000 - 50_250
    asyncio.run(run())


def test_same_key_different_body_is_rejected(wallet_http):
    async def run():
        h = {"Idempotency-Key": "k-2"}
        await wallet_http.post("/users/u1/transfers", json=TRANSFER, headers=h)
        r = await wallet_http.post("/users/u1/transfers", json={**TRANSFER, "amount": 60_000}, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert len(await _outgoing(wallet_http)) == 1
    asyncio.run(run())


def test_missing_key_is_refused(wallet_http):
    async def run():
        r = await wallet_http.post("/users/u1/transfers", json=TRANSFER)
        assert r.status_code == 400
        assert await _outgoing(wallet_http) == []
    asyncio.run(run())


def test_concurrent_requests_with_same_key_execute_once(wallet_http):
    async def run():
        h = {"Idempotency-Key": "k-3"}
        results = await asyncio.gather(*[
            wallet_http.post("/users/u1/transfers", json=TRANSFER, headers=h) for _ in range(5)])
        assert all(r.status_code == 201 for r in results)
        assert len({r.json()["transaction"]["id"] for r in results}) == 1
        assert len(await _outgoing(wallet_http)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode, times, outcome, executed", [
    ("drop_response_after_commit", 1, "completed", 1),   # نفّذت والرد ضاع → إعادة ترجع نفس النتيجة
    ("drop_response_after_commit", 3, "completed", 1),   # كل الردود ضاعت → التسوية تلگى الحركة
    ("fail_before_commit", 2, "completed", 1),           # عطل مؤقت → المحاولة الثالثة تنجح
    ("fail_before_commit", 5, "not_done", 0),            # عطل مستمر → "ما تمت" بعد التأكد من السجل
])
def test_client_retries_never_double_charge(wallet_http, mode, times, outcome, executed):
    async def run():
        await wallet_http.post("/_admin/faults", json={"mode": mode, "times": times})
        res = await WalletClient(wallet_http).execute("u1", "transfer", TRANSFER, key="card-123")
        assert res.outcome == outcome
        assert len(await _outgoing(wallet_http)) == executed
    asyncio.run(run())


def test_double_click_confirm_through_agent_executes_once(wallet_http):
    """ضغطتين "أكّد" بنفس اللحظة: قفل الجلسة + بطاقة منفّذة = عملية وحدة."""
    async def run():
        agent, store = Agent(WalletClient(wallet_http)), SessionStore()
        s = store.get(None, "u1")
        out = await agent.handle_message(s, "دز 50 الف لأحمد كريم")
        conf_id = out[-1]["confirmation"]["id"]

        async def press():
            async with s.lock:
                return await agent.confirm(s, conf_id)
        replies = await asyncio.gather(press(), press(), press())
        codes = sorted(r[-1]["code"] for r in replies)
        assert codes == ["already_executed", "already_executed", "executed"]
        assert len(await _outgoing(wallet_http)) == 1
    asyncio.run(run())
