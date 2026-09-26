# -*- coding: utf-8 -*-
"""إعداد مشترك للاختبارات: قاعدة محفظة مؤقتة + NLU القواعد (بدون GPU)."""

import os
import sys
import tempfile
from pathlib import Path

# لازم قبل أي استيراد من app — الإعدادات تُقرأ من البيئة وقت الاستيراد.
os.environ["WALLET_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "wallet_test.sqlite3")
os.environ["NLU_MODE"] = "rules"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import settings  # noqa: E402
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
