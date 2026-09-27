# -*- coding: utf-8 -*-
"""إعداد مشترك للاختبارات: قاعدة محفظة مؤقتة + موديل وهمي (بدون GPU ولا LM Studio)."""

import json
import os
import sys
import tempfile
from pathlib import Path

# لازم قبل أي استيراد من app — الإعدادات تُقرأ من البيئة وقت الاستيراد.
os.environ["WALLET_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "wallet_test.sqlite3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.engine import llm_engine  # noqa: E402
from app.wallet import db  # noqa: E402


@pytest.fixture
def fresh_wallet():
    db.reset(settings.wallet_db_path, settings.wallet_seed_path)
    from app.wallet.api import _faults
    _faults.update(mode=None, remaining=0)
    yield


@pytest.fixture
def wallet_http(fresh_wallet):
    """عميل httpx غير متزامن يكلّم المحفظة عبر ASGI (نفس طبقة HTTP)."""
    from app.wallet.api import wallet_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wallet_app), base_url="http://wallet")


# الشرح: موديل وهمي يمشي على سيناريو ثابت. كل خطوة إما نص (رد نهائي) أو
# (اسم أداة، وسائط) = الموديل طلب أداة. هيچ نختبر الحلقة والأدوات بدون LM Studio.
# بعد ما يخلص السيناريو يرد "تمام".
def _script(*steps):
    steps = list(steps)

    async def chat(messages, **kwargs):
        assert kwargs.get("tools"), "الوكيل لازم يرسل تعريف الأدوات"
        step = steps.pop(0) if steps else "تمام"
        if isinstance(step, str):
            return {"choices": [{"message": {"role": "assistant", "content": step}}]}
        name, args = step
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}
    return chat


# الشرح: يخلّي المحرك "جاهز" (حتى فاحص الجاهزية بالخلفية يرجّع True لما
# TestClient يشغّل التطبيق كامل)، ويرجّع use(*steps) يبدّل سيناريو الموديل.
@pytest.fixture
def fake_llm(monkeypatch):
    async def always_ready():
        return True
    monkeypatch.setattr(llm_engine, "_ready", True)
    monkeypatch.setattr(llm_engine, "_probe", always_ready)

    def use(*steps):
        monkeypatch.setattr(llm_engine, "chat", _script(*steps))
    use()
    return use
