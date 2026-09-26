# -*- coding: utf-8 -*-
"""يشغّل مجموعة الاختبار على الوكيل ويكتب تقرير النتائج.

الاستعمال:
    python -m eval.run_eval                       # داخل العملية (بدون سيرفر)
    python -m eval.run_eval --nlu rules           # إجبار NLU القواعد
    python -m eval.run_eval --nlu llm --base-url https://<POD_ID>-8000.proxy.runpod.net
    python -m eval.run_eval --file judge.jsonl    # مدخلات الحكّام (بدون expect = نص المحادثة فقط)

كل حالة:
  turns   رسائل المستخدم بالترتيب، مع رموز خاصة:
          <CONFIRM> ضغط زر التأكيد | <CONFIRM_TWICE> ضغطتين (إعادة إرسال) |
          <CONFIRM_OLD> تأكيد بطاقة قديمة | <CANCEL> زر الإلغاء | <OPTION:id> زر خيار |
          <FAULT:mode:n> حقن عطل بالمحفظة | <EXPIRE> انتهاء مهلة البطاقة
  expect  codes (لازم تظهر بآخر دور)، kind (نوع آخر رسالة)، executed (كم عملية
          لازم تتنفّذ فعلاً بالمحفظة)، tx (المستلم/الجهة والمبلغ)، card_recipient.

كل حالة تبدأ من محفظة مُعادة لحالتها الأصلية، فالنتائج مستقلة وقابلة للتكرار.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default=str(ROOT / "data" / "test_requests.jsonl"))
    p.add_argument("--nlu", choices=["auto", "llm", "rules"], default=None,
                   help="NLU mode for in-process runs (ignored with --base-url)")
    p.add_argument("--base-url", default=None, help="run against a live server instead of in-process")
    p.add_argument("--out", default=str(ROOT / "eval" / "results"))
    p.add_argument("--only", default=None, help="comma-separated case ids")
    return p.parse_args()


# ---------------------------------------------------------------------------
# الاتصال: داخل العملية أو سيرفر حي — نفس الواجهة
# ---------------------------------------------------------------------------

class Remote:
    def __init__(self, base_url: str):
        import httpx
        self.c = httpx.Client(base_url=base_url.rstrip("/"), timeout=60)

    def post(self, path, body=None):
        r = self.c.post(path, json=body or {})
        r.raise_for_status()
        return r.json()

    def get(self, path, params=None):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def close(self):
        self.c.close()


class InProcess(Remote):
    def __init__(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self._cm = TestClient(app)
        self.c = self._cm.__enter__()

    def close(self):
        self._cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# تشغيل حالة
# ---------------------------------------------------------------------------

def outgoing(api, user="u1"):
    """الحركات الصادرة الجديدة فقط (مو حركات البذرة) — هذي "اللي تنفّذ فعلاً"."""
    txs = api.get(f"/wallet/users/{user}/transactions", {"limit": 100})
    return [t for t in txs if not t["id"].startswith("TX-SEED") and t["type"] in ("transfer_out", "bill_payment")]


def run_case(api, case):
    api.post("/wallet/_admin/reset")
    sid, last, last_conf = None, None, None
    transcript = []
    for turn in case["turns"]:
        replies = []
        if turn.startswith("<FAULT:"):
            mode, times = turn[7:-1].rsplit(":", 1)
            api.post("/wallet/_admin/faults", {"mode": mode, "times": int(times)})
            transcript.append({"user": turn, "agent": []})
            continue
        if turn == "<EXPIRE>":
            api.post("/agent/_debug/expire", {"session_id": sid})
            transcript.append({"user": turn, "agent": []})
            continue
        pending = (last or {}).get("state", {}).get("pending_confirmation_id")
        if turn in ("<CONFIRM>", "<CONFIRM_TWICE>"):
            for _ in range(2 if turn == "<CONFIRM_TWICE>" else 1):
                replies.append(api.post("/agent/confirm", {"session_id": sid, "confirmation_id": pending or last_conf}))
        elif turn == "<CONFIRM_OLD>":
            replies.append(api.post("/agent/confirm", {"session_id": sid, "confirmation_id": last_conf}))
        elif turn == "<CANCEL>":
            replies.append(api.post("/agent/cancel", {"session_id": sid}))
        elif turn.startswith("<OPTION:"):
            replies.append(api.post("/agent/message", {"session_id": sid, "option_id": turn[8:-1]}))
        else:
            replies.append(api.post("/agent/message", {"session_id": sid, "text": turn}))
        msgs = []
        for r in replies:
            sid, last = r["session_id"], r
            last_conf = r["state"]["pending_confirmation_id"] or last_conf
            msgs.extend(r["messages"])
        transcript.append({"user": turn, "agent": msgs, "parser": last.get("parser")})
    return transcript, outgoing(api)


def check(case, transcript, executed):
    """يقارن النتيجة بالمتوقع. يرجّع (نجح؟، أسباب الفشل، هل خرق أمان؟)."""
    exp = case.get("expect")
    if not exp:
        return None, [], False
    problems = []
    final = [m for t in transcript[-1:] for m in t["agent"]]
    codes = [m["code"] for m in final]
    for code in exp.get("codes", []):
        if code not in codes:
            problems.append(f"expected code {code}, got {codes}")
    if exp.get("kind") and (not final or final[-1]["kind"] != exp["kind"]):
        problems.append(f"expected final kind {exp['kind']}, got {final[-1]['kind'] if final else None}")
    if exp.get("card_recipient"):
        cards = [m.get("confirmation") for t in transcript for m in t["agent"] if m.get("confirmation")]
        if not cards or exp["card_recipient"] not in cards[-1].get("recipient_name", ""):
            problems.append(f"card recipient should be {exp['card_recipient']}")

    # الشرح: فحص الأمان — أهم فحص. أي عملية زايدة، أو مبلغ/مستلم غلط، = خرق.
    unsafe = False
    if len(executed) != exp.get("executed", 0):
        problems.append(f"expected {exp.get('executed', 0)} executed transaction(s), wallet shows {len(executed)}")
        unsafe = len(executed) > exp.get("executed", 0)
    remaining = list(executed)
    for want in exp.get("tx", []):
        hit = next((t for t in remaining if want["counterparty"] in t["counterparty"]
                    and t["amount"] == want["amount"]), None)
        if hit is None:
            problems.append(f"missing transaction {want}")
            unsafe = unsafe or bool(executed)
        else:
            remaining.remove(hit)
    return not problems, problems, unsafe


# ---------------------------------------------------------------------------
# التقرير
# ---------------------------------------------------------------------------

def write_report(out_dir: Path, results: list, meta: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"meta": meta, "results": results},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    scored = [r for r in results if r["passed"] is not None]
    by_cat = defaultdict(lambda: [0, 0])
    for r in scored:
        by_cat[r["category"]][1] += 1
        by_cat[r["category"]][0] += r["passed"]
    must_not = [r for r in scored if r["expected_executed"] == 0]
    lines = [
        "# Evaluation report", "",
        f"- Date: {meta['date']}  ",
        f"- Target: {meta['target']}  ",
        f"- NLU parser(s) used: {', '.join(sorted(meta['parsers'])) or '-'}  ",
        f"- Test file: `{meta['file']}`", "",
        "## Summary", "",
        f"| Metric | Value |", "|---|---|",
        f"| Cases passed | **{sum(r['passed'] for r in scored)} / {len(scored)}** |",
        f"| Unsafe outcomes (extra, wrong-amount or wrong-recipient executions) | **{sum(r['unsafe'] for r in scored)}** |",
        f"| Requests that must NOT execute | {len(must_not)} — correctly blocked: "
        f"{sum(1 for r in must_not if r['executed_count'] == 0)} |",
        f"| Total wallet transactions executed across all cases | {sum(r['executed_count'] for r in scored)} |",
        "", "## By category", "", "| Category | Passed |", "|---|---|",
    ]
    lines += [f"| {c} | {p}/{n} |" for c, (p, n) in sorted(by_cat.items())]
    lines += ["", "## Cases", "", "| ID | Category | First message | Expected | Result | Executed |", "|---|---|---|---|---|---|"]
    for r in results:
        exp = r.get("expect") or {}
        want = ", ".join(exp.get("codes", [])) or exp.get("kind", "-")
        status = "—" if r["passed"] is None else ("✅" if r["passed"] else "❌" + (" ⚠️UNSAFE" if r["unsafe"] else ""))
        first = r["turns"][0].replace("|", "/")
        lines.append(f"| {r['id']} | {r['category']} | {first} | {want} | {status} | {r['executed_count']} |")
    failed = [r for r in results if r["passed"] is False]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append(f"### {r['id']}")
            lines += [f"- {p}" for p in r["problems"]]
            lines.append("")
    lines += ["", "## Transcripts", ""]
    for r in results:
        lines.append(f"<details><summary>{r['id']} — {r['turns'][0]}</summary>\n")
        for t in r["transcript"]:
            lines.append(f"- 👤 `{t['user']}`")
            for m in t["agent"]:
                lines.append(f"  - 🤖 `{m['code']}` {m['text']}")
        lines.append("\n</details>\n")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    args = parse_args()
    if args.nlu:
        os.environ["NLU_MODE"] = args.nlu
    if not args.base_url:
        # قاعدة بيانات مستقلة للتقييم حتى ما تلمس بيانات العرض.
        os.environ.setdefault("WALLET_DB_PATH", str(ROOT / "eval" / "results" / "eval_wallet.sqlite3"))
    sys.path.insert(0, str(ROOT))

    cases = [json.loads(l) for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    api = Remote(args.base_url) if args.base_url else InProcess()
    results, parsers = [], set()
    try:
        for case in cases:
            transcript, executed = run_case(api, case)
            passed, problems, unsafe = check(case, transcript, executed)
            parsers.update(t.get("parser") for t in transcript if t.get("parser"))
            results.append({"id": case["id"], "category": case.get("category", "judge"), "turns": case["turns"],
                            "expect": case.get("expect"), "passed": passed, "problems": problems,
                            "unsafe": unsafe, "executed_count": len(executed),
                            "expected_executed": (case.get("expect") or {}).get("executed"),
                            "transcript": transcript})
            mark = "·" if passed is None else ("✓" if passed else "✗")
            print(f"{mark} {case['id']:<5} {case['turns'][0]}")
            for p in problems:
                print(f"      {p}")
    finally:
        api.close()

    meta = {"date": time.strftime("%Y-%m-%d %H:%M"), "target": args.base_url or "in-process",
            "parsers": [p for p in parsers if p], "file": Path(os.path.relpath(args.file, ROOT)).as_posix()}
    write_report(Path(args.out), results, meta)
    scored = [r for r in results if r["passed"] is not None]
    print(f"\n{sum(r['passed'] for r in scored)}/{len(scored)} passed · "
          f"unsafe: {sum(r['unsafe'] for r in scored)} · report: {Path(args.out) / 'report.md'}")
    return 0 if all(r["passed"] for r in scored) else 1


if __name__ == "__main__":
    sys.exit(main())
