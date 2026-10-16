#!/usr/bin/env python3
"""에이전트(Claude Code)가 쓰는 유일한 명령줄 도구. PROTECTED.

에이전트의 Bash 권한은 `python tools.py ...` 하나로 제한된다.
실거래·사이클 실행 같은 운영자 기능은 여기 없다 (run.py / 웹앱 전용).

  python tools.py status                       # 현금, 오픈 포지션
  python tools.py score                        # 캘리브레이션 (brier_delta) / 엣지 클래스별 성적
  python tools.py history [--n 30]             # 최근 정산된 베팅 (회고용)
  python tools.py scan [--limit 40] [--query nba] [--days 3]
  python tools.py market <market_id>           # 설명 + 실시간 호가
  python tools.py bet --market-id ID --outcome Yes --est-prob 0.62 --stake 5 \
        --category sports --edge-class base-rate --rationale "근거 한 줄"
"""
import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from phil import ledger, scan, score
from phil.config import LEDGER, load_config, load_jsonl


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="tools.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("score")
    h = sub.add_parser("history")
    h.add_argument("--n", type=int, default=30)
    s = sub.add_parser("scan")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--query")
    s.add_argument("--days", type=float)
    m = sub.add_parser("market")
    m.add_argument("market_id")
    b = sub.add_parser("bet")
    b.add_argument("--market-id", required=True)
    b.add_argument("--outcome", required=True)
    b.add_argument("--est-prob", type=float, required=True)
    b.add_argument("--stake", type=float, required=True)
    b.add_argument("--category", required=True)
    b.add_argument("--edge-class", required=True, choices=cfg["edge_classes"])
    b.add_argument("--rationale", required=True)
    a = ap.parse_args()

    if a.cmd == "status":
        out(ledger.status())
    elif a.cmd == "score":
        r = score.report()
        r.pop("equity_curve", None)
        out(r)
    elif a.cmd == "history":
        rows = [e for e in load_jsonl(LEDGER) if e["status"] != "open"][-a.n:]
        out([{k: e.get(k) for k in ("id", "question", "outcome", "est_prob", "entry_price",
                                    "status", "pnl_usd", "edge_class", "category",
                                    "rationale")} for e in rows])
    elif a.cmd == "scan":
        out(scan.scan(limit=a.limit, query=a.query, max_days=a.days))
    elif a.cmd == "market":
        out(scan.market_detail(a.market_id))
    elif a.cmd == "bet":
        try:
            e = ledger.place(a.market_id, a.outcome, a.est_prob, a.stake, a.category,
                             a.edge_class, a.rationale)
        except ledger.Rejected as err:
            print(f"REJECTED: {err}")
            sys.exit(2)
        out({"placed": e["id"], "filled_at": e["entry_price"], "edge": e["edge"],
             "shares": e["shares"], "question": e["question"]})


if __name__ == "__main__":
    main()
