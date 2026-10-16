"""성적 리포트: 손익, 코스피 대비 초과수익, 확률 정확도. PROTECTED."""
from collections import defaultdict

from . import policy
from .config import FORECASTS, LEDGER, META, load_config, load_json, load_jsonl, save_json
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
    return {
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
