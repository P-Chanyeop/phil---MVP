"""캘리브레이션·손익 리포트. PROTECTED.

정직한 지표는 brier_delta: (내 확률의 Brier) - (내가 지불한 시장가격의 Brier).
음수면 에이전트가 시장보다 더 좋은 예측을 했다는 뜻.
"""
from collections import defaultdict

from . import policy
from .config import FORECASTS, load_config, load_jsonl, LEDGER, REAL_LEDGER
from .ledger import cash


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = sum(r.get("pnl_usd", 0) for r in rows)
    stake = sum(r["stake_usd"] for r in rows)
    wins = sum(1 for r in rows if r["status"] == "won")
    ba = sum((r["est_prob"] - (r["status"] == "won")) ** 2 for r in rows) / n
    bm = sum((r["entry_price"] - (r["status"] == "won")) ** 2 for r in rows) / n
    return {"n": n, "wins": wins, "win_rate": round(wins / n, 3),
            "pnl": round(pnl, 2), "roi": round(pnl / stake, 3) if stake else 0,
            "brier_agent": round(ba, 4), "brier_market": round(bm, 4),
            "brier_delta": round(ba - bm, 4)}


def report():
    cfg = load_config()
    entries = load_jsonl(LEDGER)
    settled = [e for e in entries if e["status"] in ("won", "lost")]
    by_class, by_cat = defaultdict(list), defaultdict(list)
    for e in settled:
        by_class[e["edge_class"]].append(e)
        by_cat[e.get("category") or "?"].append(e)
    # 누적 자산 곡선 (정산 순서)
    curve, eq = [], cfg["sim_bankroll_usd"]
    for e in sorted(settled, key=lambda r: r.get("settled_ts", "")):
        eq += e.get("pnl_usd", 0)
        curve.append({"ts": e.get("settled_ts"), "equity": round(eq, 2)})
    return {
        "cash": cash(entries, cfg),
        "bankroll_start": cfg["sim_bankroll_usd"],
        "overall": _stats(settled),
        "by_edge_class": {k: _stats(v) for k, v in sorted(by_class.items())},
        "by_category": {k: _stats(v) for k, v in sorted(by_cat.items())},
        "qualified_edge_classes": qualified_edge_classes(cfg, by_class),
        "forecasts": forecast_summary(),
        "category_calibration": policy.category_calibration(),
        "banned_categories": policy.banned_categories(cfg),
        "equity_curve": curve,
        "real": real_summary(),
    }


def qualified_edge_classes(cfg=None, by_class=None):
    """실거래 복제가 허용되는 엣지 클래스: 정산 증거로 입증된 것만."""
    cfg = cfg or load_config()
    q = cfg["real"]["edge_qualification"]
    if by_class is None:
        by_class = defaultdict(list)
        for e in load_jsonl(LEDGER):
            if e["status"] in ("won", "lost"):
                by_class[e["edge_class"]].append(e)
    out = []
    for k, rows in by_class.items():
        s = _stats(rows)
        if s["n"] < q["min_settled"]:
            continue
        if q.get("require_positive_pnl") and s["pnl"] <= 0:
            continue
        if q.get("require_negative_brier_delta") and s["brier_delta"] >= 0:
            continue
        out.append(k)
    return sorted(out)


def real_summary():
    rows = load_jsonl(REAL_LEDGER)
    last = {}
    for r in rows:
        last[r["id"]] = r
    vals = list(last.values())
    filled = [r for r in vals if r["status"] in ("filled", "won", "lost")]
    return {
        "orders": len(vals),
        "filled": len(filled),
        "open": sum(1 for r in vals if r["status"] == "filled"),
        "staked": round(sum(r.get("usd", 0) for r in filled), 2),
        "pnl": round(sum(r.get("pnl_usd", 0) for r in vals if r["status"] in ("won", "lost")), 2),
        "rejected": sum(1 for r in vals if r["status"] in ("rejected", "failed")),
    }


def forecast_summary():
    rows = load_jsonl(FORECASTS)
    st = [r for r in rows if r["status"] in ("won", "lost")]
    out = {"total": len(rows), "open": sum(1 for r in rows if r["status"] == "open"),
           "settled": len(st)}
    if st:
        y = lambda r: r["status"] == "won"  # noqa: E731
        a = sum((r["est_prob"] - y(r)) ** 2 for r in st) / len(st)
        m = sum((r["market_prob_at_record"] - y(r)) ** 2 for r in st) / len(st)
        out.update(brier_agent=round(a, 4), brier_market=round(m, 4), brier_delta=round(a - m, 4))
    return out
