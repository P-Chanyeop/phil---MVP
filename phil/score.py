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
    net_after = eq_now - cost["billed_krw"]
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
        "by_model": policy.by_model(),
        "days": day_results(),
        "buy_gap": buy_gap(),
        "forecast_gap": forecast_gap(),
        "banned_edge_classes": policy.banned_edge_classes(cfg),
        "verified_edge_classes": verified_edge_classes(by_class),
        "forecasts": {"total": len(fc), "open": sum(1 for r in fc if r["status"] == "open"),
                      "settled": sum(1 for r in fc if r["status"] in ("up", "down"))},
        "equity_curve": curve,
    }


def day_results():
    """장 마감마다 계좌가 전날보다 늘었나 (이긴 날). 대기 자금·보유 평가 포함 전체 자산 기준, 실제 청구 비용 차감."""
    from .config import DAILY
    cfg = load_config()
    rows = load_jsonl(DAILY)
    prev, prev_b = cfg["sim_bankroll_krw"], 0.0
    rate = cfg.get("usd_krw", 1450)
    sub = claude_cost(cfg)["subscription"]
    days = []
    for r in rows:
        billed = r.get("billed_usd_total", 0 if sub else r.get("cost_usd_total", 0)) or 0
        pnl = r["equity"] - prev - (billed - prev_b) * rate
        days.append({"date": r["date"], "pnl_krw": round(pnl), "pct": round(pnl / prev * 100, 2)})
        prev, prev_b = r["equity"], billed
    wins = [d for d in days if d["pnl_krw"] > 0]
    streak = 0
    for d in reversed(days):
        if (d["pnl_krw"] > 0) == (days[-1]["pnl_krw"] > 0):
            streak += 1
        else:
            break
    return {"n": len(days), "win_days": len(wins),
            "win_day_rate": round(len(wins) / len(days), 2) if days else None,
            "avg_day_krw": round(sum(d["pnl_krw"] for d in days) / len(days)) if days else None,
            "best": max(days, key=lambda d: d["pnl_krw"]) if days else None,
            "worst": min(days, key=lambda d: d["pnl_krw"]) if days else None,
            "streak": {"kind": "win" if days and days[-1]["pnl_krw"] > 0 else "loss", "n": streak} if days else None,
            "recent": days[-10:]}


def buy_gap():
    """에이전트가 마지막으로 산 뒤 지난 거래일 수 (장 마감 기록 기준). 하락장이 아닌데 길면 소심해진 것."""
    from .config import DAILY
    from . import market
    buys = [e for e in load_jsonl(LEDGER) if e["edge_class"] != "rule"]
    last = market.kst_date(buys[-1]["ts"]) if buys else None
    days = [r["date"] for r in load_jsonl(DAILY)]
    gap = sum(1 for d in days if last is None or d > last)
    rg = market.regime_now()
    limit = load_config()["policy"].get("inactivity_alert_days", 3)
    bear = bool(rg and rg["label"] == "하락장")
    return {"last_buy_date": last, "trading_days_since": gap, "bear_market": bear,
            "alert": gap >= limit and not bear, "limit": limit}


def forecast_gap(now=None):
    """오늘 장중(FULL) 사이클 수 대비 새 예측 수. 예측이 너무 적으면 실력을 잴 수 없다."""
    from . import market
    today = (now or now_kst()).date().isoformat()
    full = [c for c in load_jsonl(CYCLES_LOG) if c.get("mode") == "FULL" and c.get("ok")
            and c.get("started") and market.kst_date(c["started"]) == today]
    n_fc = sum(c.get("new_forecasts") or 0 for c in full)
    target = load_config()["policy"].get("forecasts_per_full_cycle", 8)
    return {"full_cycles_today": len(full), "forecasts_today": n_fc, "target_per_cycle": target,
            "alert": len(full) >= 2 and n_fc < target * len(full) / 2}


def breakdown(prices=None):
    """손익 분해 — 어디서 벌고 잃었나. 합계 = 자산 − 시작 자금.
    종목 실현 + 종목 평가 + 대기 자금(ETF, 실현 포함) = 비용 전 손익. 거기서 Claude 비용을 뺀 것이 진짜."""
    from . import park
    from .ledger import sell_proceeds
    cfg = load_config()
    entries = load_jsonl(LEDGER)
    closed = [e for e in entries if e["status"] == "closed"]
    opens = [e for e in entries if e["status"] == "open"]
    realized = sum(e["pnl_krw"] for e in closed)
    unreal = 0
    for e in opens:
        p = (prices or {}).get(e["code"]) or e.get("last_price") or e["entry_price"]
        unreal += sell_proceeds(p, e["qty"], cfg["costs"])[1] - e["cost_krw"]
    pk = park.summary((prices or {}).get(cfg["park"]["code"]))
    park_pnl = pk.get("total_pnl_krw", 0)
    cc = claude_cost(cfg)
    cost = cc["billed_krw"]
    pre = realized + unreal + park_pnl
    return {"stock_realized_krw": realized, "stock_unrealized_krw": unreal, "stock_total_krw": realized + unreal,
            "park_krw": park_pnl, "pre_cost_krw": pre, "claude_cost_krw": cost, "net_krw": pre - cost,
            "claude_usage_usd": cc["total_usd"], "claude_subscription": cc["subscription"],
            "closed_n": len(closed), "open_n": len(opens), "park_value_krw": pk.get("value_krw", 0)}


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
    """사이클 기록의 Claude 사용량 (달러 → 원, 설정 환율).

    total_*  : 전체 사용량을 API 요금으로 환산한 값 (참고용).
    billed_* : 실제로 청구되는 몫 — API 키로 돈 사이클만. 구독 로그인(auth=login) 사이클은
               구독 사용량에서 차감될 뿐 추가 결제가 없으므로 0 으로 본다.
    수익률·손익 분해·비용 절감 장치는 billed 를 쓴다."""
    cfg = cfg or load_config()
    rate = cfg.get("usd_krw", 1450)
    today = now_kst().date().isoformat()
    total = today_usd = billed = billed_today = 0.0
    rows = load_jsonl(CYCLES_LOG)
    last_auth = next((c["auth"] for c in reversed(rows) if c.get("auth")), None)
    for c in rows:
        usd = c.get("cost_usd") or 0
        # 인증 방식이 기록되지 않은 옛 사이클은 지금 방식(구독이면 청구 없음)을 따른다
        paid = c.get("auth") == "api_key" or (not c.get("auth") and last_auth != "login")
        total += usd
        billed += usd if paid else 0
        t = parse_ts(c.get("finished") or c.get("started"))
        if t and t.astimezone(KST).date().isoformat() == today:
            today_usd += usd
            billed_today += usd if paid else 0
    return {"total_usd": round(total, 2), "today_usd": round(today_usd, 2), "usd_krw": rate,
            "total_krw": round(total * rate), "today_krw": round(today_usd * rate),
            "billed_usd": round(billed, 2), "billed_krw": round(billed * rate),
            "billed_today_krw": round(billed_today * rate),
            "subscription": last_auth == "login"}


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
        "question_to_ask_yourself": "오늘 장이 끝났을 때 계좌가 늘었나? 지금 방식이 이긴 날을 늘리고 있나, 아니면 무엇을 바꿀 것인가?",
        "day_results": day_results(),
        "buy_gap": buy_gap(),
        "forecast_gap": forecast_gap(),
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
