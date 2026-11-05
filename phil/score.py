"""성적 리포트: 손익, 코스피 대비 초과수익, 확률 정확도. PROTECTED."""
from collections import defaultdict

from . import policy
from .config import (CYCLES_LOG, FORECASTS, LEDGER, META, load_config, load_json, load_jsonl, now_kst,
                     parse_ts, save_json, KST)
from .ledger import cash, equity


def _trade_stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = sum(r["pnl_krw"] for r in rows)
    cost = sum(r["cost_krw"] for r in rows)
    wins = sum(1 for r in rows if r["pnl_krw"] > 0)
    return {"n": n, "wins": wins, "win_rate": round(wins / n, 3), "pnl_krw": pnl,
            "ret_on_cost_pct": round(pnl / cost * 100, 2) if cost else 0,
            "avg_ret_pct": round(sum(r["ret_pct"] for r in rows) / n, 2)}


def ensure_meta():
    """처음 실행 시 시작일과 코스피 기준값 기록 (벤치마크용)."""
    meta = load_json(META, {})
    if not meta.get("start_kospi"):
        try:
            from . import naver
            from .config import now_kst
            meta.update(start_date=now_kst().date().isoformat(),
                        start_kospi=naver.index_quote("KOSPI")["price"])
            save_json(META, meta)
        except Exception:  # noqa: BLE001
            pass
    return meta


def report(kospi_now=None):
    cfg = load_config()
    entries = load_jsonl(LEDGER)
    closed = [e for e in entries if e["status"] == "closed"]
    by_class = defaultdict(list)
    for e in closed:
        by_class[e["edge_class"]].append(e)
    start = cfg["sim_bankroll_krw"]
    curve, eq = [], start
    for e in sorted(closed, key=lambda r: r.get("exit_ts", "")):
        eq += e["pnl_krw"]
        curve.append({"ts": e.get("exit_ts"), "equity": eq})
    eq_now = equity(entries, cfg)
    meta = load_json(META, {})
    bench = None
    if kospi_now and meta.get("start_kospi"):
        bench = round((kospi_now / meta["start_kospi"] - 1) * 100, 2)
    my_ret = round((eq_now / start - 1) * 100, 2)
    fc = load_jsonl(FORECASTS)
    cost = claude_cost(cfg)
    net_after = eq_now - cost["total_krw"]
    return {
        "claude_cost": cost,
        "return_after_cost_pct": round((net_after / start - 1) * 100, 2),
        "pnl_after_cost_krw": net_after - start,
        "cash_krw": cash(entries, cfg),
        "equity_krw": eq_now,
        "bankroll_start_krw": start,
        "return_pct": my_ret,
        "kospi_return_pct": bench,
        "alpha_pct": round(my_ret - bench, 2) if bench is not None else None,
        "start_date": meta.get("start_date"),
        "trades": _trade_stats(closed),
        "by_edge_class": {k: _trade_stats(v) for k, v in sorted(by_class.items())},
        "calibration": policy.calibration_overall(),
        "calibration_by_edge_class": policy.calibration_by_class(),
        "banned_edge_classes": policy.banned_edge_classes(cfg),
        "verified_edge_classes": verified_edge_classes(by_class),
        "forecasts": {"total": len(fc), "open": sum(1 for r in fc if r["status"] == "open"),
                      "settled": sum(1 for r in fc if r["status"] in ("up", "down"))},
        "equity_curve": curve,
    }


def verified_edge_classes(by_class=None):
    """나중에 실거래를 붙일 때 기준: 청산 10건 이상, 수익, 기저율보다 정확."""
    if by_class is None:
        by_class = defaultdict(list)
        for e in load_jsonl(LEDGER):
            if e["status"] == "closed":
                by_class[e["edge_class"]].append(e)
    cal = policy.calibration_by_class()
    out = []
    for k, rows in by_class.items():
        s = _trade_stats(rows)
        c = cal.get(k)
        if s["n"] >= 10 and s["pnl_krw"] > 0 and c and c["brier_delta"] < 0:
            out.append(k)
    return sorted(out)


def claude_cost(cfg=None):
    """사이클 기록의 Claude 사용료 합계 (달러 → 원, 설정 환율)."""
    cfg = cfg or load_config()
    rate = cfg.get("usd_krw", 1450)
    today = now_kst().date().isoformat()
    total = today_usd = 0.0
    for c in load_jsonl(CYCLES_LOG):
        usd = c.get("cost_usd") or 0
        total += usd
        t = parse_ts(c.get("finished") or c.get("started"))
        if t and t.astimezone(KST).date().isoformat() == today:
            today_usd += usd
    return {"total_usd": round(total, 2), "today_usd": round(today_usd, 2), "usd_krw": rate,
            "total_krw": round(total * rate), "today_krw": round(today_usd * rate)}


def scorecard():
    """사람 트레이더의 성적표: 수익원별 손익, 최근 흐름, 비용. 매 사이클 에이전트가 본다."""
    from . import park, tuning
    entries = load_jsonl(LEDGER)
    closed = [e for e in entries if e["status"] == "closed"]

    def bucket(rows):
        n = len(rows)
        pnl = sum(r["pnl_krw"] for r in rows)
        wins = [r for r in rows if r["pnl_krw"] > 0]
        losses = [r for r in rows if r["pnl_krw"] <= 0]
        return {"n": n, "pnl_krw": pnl, "win_rate": round(len(wins) / n, 2) if n else None,
                "avg_win_pct": round(sum(r["ret_pct"] for r in wins) / len(wins), 2) if wins else None,
                "avg_loss_pct": round(sum(r["ret_pct"] for r in losses) / len(losses), 2) if losses else None}

    src = {
        "my_regular_buys": bucket([e for e in closed if e["edge_class"] != "rule" and not e.get("exploration")]),
        "my_exploration_buys": bucket([e for e in closed if e.get("exploration")]),
        "engine_rule_buys": bucket([e for e in closed if e["edge_class"] == "rule"]),
    }
    by_class = {}
    for e in closed:
        by_class.setdefault(e["edge_class"], []).append(e)
    exits = {}
    for e in closed:
        k = (e.get("exit_reason") or "?").split(" ")[0]
        exits.setdefault(k, []).append(e)
    p = park.summary()
    cost = claude_cost()
    cycles = load_jsonl(CYCLES_LOG)[-20:]
    paid = [c for c in cycles if c.get("cost_usd")]
    return {
        "question_to_ask_yourself": "지금 방식이 비용을 빼고 돈을 벌고 있나? 아니면 무엇을 바꿀 것인가?",
        "last_5_days": tuning.window(5), "last_10_days": tuning.window(10),
        "by_source": src,
        "by_edge_class": {k: bucket(v) for k, v in by_class.items()},
        "by_exit_reason": {k: bucket(v) for k, v in exits.items()},
        "park_etf": {"value_krw": p["value_krw"], "pnl_krw": p["pnl_krw"]},
        "claude_cost": cost,
        "avg_cost_per_paid_cycle_usd": round(sum(c["cost_usd"] for c in paid) / len(paid), 2) if paid else None,
        "cycles_last_20_with_no_buy_and_no_forecast": sum(1 for c in cycles if c.get("mode") and not c.get("new_bets")
                                                           and not c.get("new_forecasts")),
    }
