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

  ── 연구 (규칙) ──
  python tools.py features                     # 규칙에 쓸 수 있는 지표 목록
  python tools.py backtest --spec '{"conditions":[["ret_1d","<",-5],["vol_ratio",">",2]],"horizon":3,"stop":0.05,"target":0.06}'
  python tools.py rule propose --name "이름" --spec '<위와 같은 JSON>' --why "가설 한 줄"
  python tools.py rule list
  python tools.py rule retire --id <규칙id> --why "이유"
  python tools.py audit                        # 주간 감사 자료 (전략 버전별 성적, 수정 내역)
  python tools.py disclosures [--code 005930]  # 최근 주요 공시 (DART)
  python tools.py disclosure --rcept 20261105000123 [--chars 6000]   # 공시 원문 읽기 (DART API)
  python tools.py discoveries                  # 엔진 자동 탐색 결과 (매일 장 마감 후)
"""
import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from phil import audit, dart, forecast, ledger, market, naver, policy, research, rules, score  # noqa: E402
from phil.config import FORECASTS, LEDGER, load_config, load_jsonl  # noqa: E402


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def fail(msg):
    print(f"REJECTED: {msg}")
    sys.exit(2)


def main():
    cfg = load_config()
    classes = [c for c in cfg["policy"]["edge_classes"] if c != "rule"]
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
    sub.add_parser("features")
    sub.add_parser("audit")
    sub.add_parser("discoveries")
    bt = sub.add_parser("backtest")
    bt.add_argument("--spec", required=True)
    rl = sub.add_parser("rule")
    rl.add_argument("action", choices=["propose", "list", "retire"])
    rl.add_argument("--name")
    rl.add_argument("--spec")
    rl.add_argument("--why", default="")
    rl.add_argument("--id")
    dd = sub.add_parser("disclosure")
    dd.add_argument("--rcept", required=True)
    dd.add_argument("--chars", type=int, default=6000)
    ds = sub.add_parser("disclosures")
    ds.add_argument("--code")
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
        elif a.cmd == "features":
            out({"features": research.FEATURES, "operators": list(research.OPS),
                 "spec_fields": {"conditions": "[[지표, 연산자, 값], ...] 1~6개 (모두 만족)",
                                 "horizon": "보유 거래일 1~5", "stop": "손절폭 0.02~0.10",
                                 "target": "목표 수익률 또는 null", "max_per_day": "하루 최대 신호 1~5 (기본 3)",
                                 "rank_by": "신호가 많을 때 고르는 기준 지표 (큰 순, 앞에 - 면 작은 순)"},
                 "entry": "신호일 종가 매수 (실전은 15:05 현재가), 비용 포함"})
        elif a.cmd == "backtest":
            r = research.backtest(a.spec)
            out(r)
        elif a.cmd == "rule":
            if a.action == "list":
                out(rules.summary())
            elif a.action == "propose":
                if not (a.name and a.spec and len(a.why.strip()) >= 10):
                    fail("--name, --spec, --why(10자 이상) 가 필요")
                r = rules.propose(a.name, a.spec, a.why)
                out({"id": r["id"], "status": r["status"], "history": r["history"][-1],
                     "test": r["backtest"]["test"], "baseline_test": r["backtest"]["baseline_test"]})
            else:
                if not (a.id and len(a.why.strip()) >= 5):
                    fail("--id 와 --why 가 필요")
                r = rules.retire(a.id, a.why)
                out({"retired": r["id"], "name": r["name"]})
        elif a.cmd == "discoveries":
            from phil import discover
            out(discover.last_report() or {"note": "아직 자동 탐색 결과 없음 (매일 16:10 이후 실행)"})
        elif a.cmd == "audit":
            out(audit.report())
        elif a.cmd == "disclosure":
            out(dart.document_text(a.rcept, min(max(a.chars, 500), 20000)))
        elif a.cmd == "disclosures":
            out(dart.recent(a.code))
    except (research.SpecError, rules.RuleError, dart.DartError) as err:
        fail(err)
    except (ledger.Rejected, forecast.Rejected) as err:
        fail(err)
    except naver.DataError as err:
        fail(f"시세 조회 실패: {err}")


if __name__ == "__main__":
    main()
