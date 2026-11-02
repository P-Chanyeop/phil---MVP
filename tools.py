#!/usr/bin/env python3
"""에이전트(Claude Code)가 쓰는 유일한 명령줄 도구. PROTECTED.

에이전트의 Bash 권한은 `python tools.py ...` 하나로 제한된다.

  python tools.py market                       # 코스피/코스닥 지수, 지금 매매 가능한지
  python tools.py status                       # 현금, 평가액, 보유 종목
  python tools.py score                        # 손익, 코스피 대비, 확률 정확도
  python tools.py policy                       # 엔진 규칙, 근거 유형별 정확도, 금지 목록
  python tools.py history [--n 30]             # 최근 청산·정산 기록 (회고용)
  python tools.py scan [--sort value|up|down|rank] [--market KOSPI|KOSDAQ] [--query 이름] [--limit 40]
  python tools.py stock <종목코드>              # 시세, 수익률, 기저율, 최근 10일 봉, 지표
  python tools.py news <종목코드> [--n 10]      # 최근 뉴스 제목
  python tools.py forecast --code 005930 --horizon 3 --est-prob 0.58 --edge-class news-flow --note "근거"
  python tools.py buy --code 005930 --horizon 3 --est-prob 0.62 --stop 0.04 [--target 0.06] \\
        [--amount 500000] --edge-class earnings --rationale "출처와 숫자가 든 한 줄"
  python tools.py sell --id <포지션id> --reason "이유"
"""
import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from phil import forecast, ledger, market, naver, policy, score  # noqa: E402
from phil.config import FORECASTS, LEDGER, load_config, load_jsonl  # noqa: E402


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def fail(msg):
    print(f"REJECTED: {msg}")
    sys.exit(2)


def main():
    cfg = load_config()
    classes = cfg["policy"]["edge_classes"]
    ap = argparse.ArgumentParser(prog="tools.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("market", "status", "score", "policy"):
        sub.add_parser(c)
    h = sub.add_parser("history")
    h.add_argument("--n", type=int, default=30)
    s = sub.add_parser("scan")
    s.add_argument("--sort", default="value", choices=["value", "up", "down", "rank"])
    s.add_argument("--market", choices=["KOSPI", "KOSDAQ"])
    s.add_argument("--query")
    s.add_argument("--limit", type=int, default=40)
    st = sub.add_parser("stock")
    st.add_argument("code")
    nw = sub.add_parser("news")
    nw.add_argument("code")
    nw.add_argument("--n", type=int, default=10)
    f = sub.add_parser("forecast")
    f.add_argument("--code", required=True)
    f.add_argument("--horizon", type=int, required=True)
    f.add_argument("--est-prob", type=float, required=True)
    f.add_argument("--edge-class", required=True, choices=classes)
    f.add_argument("--note", default="")
    b = sub.add_parser("buy")
    b.add_argument("--code", required=True)
    b.add_argument("--horizon", type=int, required=True)
    b.add_argument("--est-prob", type=float, required=True)
    b.add_argument("--stop", type=float, required=True, help="손절폭 (0.04 = -4%%)")
    b.add_argument("--target", type=float, help="목표 수익률 (0.06 = +6%%)")
    b.add_argument("--amount", type=float, help="매수 금액(원). 생략 시 한도까지")
    b.add_argument("--edge-class", required=True, choices=classes)
    b.add_argument("--rationale", required=True)
    se = sub.add_parser("sell")
    se.add_argument("--id", required=True)
    se.add_argument("--reason", required=True)
    a = ap.parse_args()

    try:
        if a.cmd == "market":
            out(market.market_overview())
        elif a.cmd == "status":
            out(ledger.status())
        elif a.cmd == "score":
            r = score.report(naver.index_quote("KOSPI")["price"])
            r.pop("equity_curve", None)
            out(r)
        elif a.cmd == "policy":
            out(policy.summary())
        elif a.cmd == "history":
            trades = [e for e in load_jsonl(LEDGER) if e["status"] == "closed" or "calib" in e][-a.n:]
            fcs = [r for r in load_jsonl(FORECASTS) if r["status"] != "open"][-a.n:]
            out({"trades": [{k: e.get(k) for k in ("id", "name", "code", "est_prob", "base_rate",
                                                   "edge_class", "status", "exit_reason", "ret_pct",
                                                   "pnl_krw", "calib", "rationale")} for e in trades],
                 "forecasts": [{k: r.get(k) for k in ("id", "name", "code", "horizon_days", "est_prob",
                                                      "base_rate", "edge_class", "status", "note")}
                               for r in fcs]})
        elif a.cmd == "scan":
            out(market.scan(a.sort, a.query, a.limit, a.market))
        elif a.cmd == "stock":
            out(market.stock_detail(a.code))
        elif a.cmd == "news":
            out(naver.news(a.code, a.n))
        elif a.cmd == "forecast":
            r = forecast.record(a.code, a.horizon, a.est_prob, a.edge_class, a.note)
            out({"recorded": r["id"], "name": r["name"], "ref_price": r["ref_price"],
                 "base_rate": r["base_rate"], "edge_vs_base": round(r["est_prob"] - r["base_rate"], 4)})
        elif a.cmd == "buy":
            e = ledger.buy(a.code, a.est_prob, a.horizon, a.stop, a.target, a.edge_class,
                           a.rationale, amount_krw=a.amount)
            out({"bought": e["id"], "name": e["name"], "qty": e["qty"], "fill": e["entry_price"],
                 "cost_krw": e["cost_krw"], "stop": e["stop_price"], "target": e["target_price"],
                 "base_rate": e["base_rate"], "edge": e["edge"],
                 "type": "탐색 매수 (작게)" if e.get("exploration") else "정규 매수"})
        elif a.cmd == "sell":
            e = ledger.sell(a.id, a.reason)
            out({"sold": e["id"], "name": e["name"], "ret_pct": e["ret_pct"], "pnl_krw": e["pnl_krw"]})
    except (ledger.Rejected, forecast.Rejected) as err:
        fail(err)
    except naver.DataError as err:
        fail(f"시세 조회 실패: {err}")


if __name__ == "__main__":
    main()
