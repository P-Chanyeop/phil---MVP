"""엔진이 강제하는 베팅 정책 (금지 카테고리, 수수료 차감 후 최소 엣지, 엣지 비례 베팅 한도). PROTECTED."""
import re
from collections import defaultdict

from .config import FORECASTS, LEDGER, load_config, load_jsonl


def _brier_delta(rows):
    """에이전트 확률 vs 기록 시점 시장 확률. 음수 = 에이전트가 더 정확."""
    n = len(rows)
    a = sum((r["est_prob"] - (r["status"] == "won")) ** 2 for r in rows) / n
    m = sum((r["mkt"] - (r["status"] == "won")) ** 2 for r in rows) / n
    return round(a - m, 4)


def category_calibration():
    """카테고리별 정확도. 예측 기록과 정산된 베팅을 모두 쓴다."""
    g = defaultdict(list)
    for r in load_jsonl(FORECASTS):
        if r["status"] in ("won", "lost"):
            g[r["category"]].append({"est_prob": r["est_prob"], "status": r["status"],
                                     "mkt": r["market_prob_at_record"]})
    for r in load_jsonl(LEDGER):
        if r["status"] in ("won", "lost") and not r.get("forecast_id"):
            g[r["category"]].append({"est_prob": r["est_prob"], "status": r["status"],
                                     "mkt": r.get("mid_at_entry") or r["entry_price"]})
    return {k: {"n": len(v), "brier_delta": _brier_delta(v)} for k, v in g.items()}


def banned_categories(cfg=None):
    """{카테고리: 이유}"""
    cfg = cfg or load_config()
    pol = cfg["policy"]
    out = {c: "원본 데이터 검증 결과 시장보다 부정확" for c in pol["banned_categories"]}
    ab = pol["auto_ban"]
    for c, s in category_calibration().items():
        if s["n"] >= ab["min_n"] and s["brier_delta"] > ab["max_brier_delta"]:
            out[c] = f"내 기록 {s['n']}건에서 시장보다 부정확 (brier Δ {s['brier_delta']:+.4f})"
    return out


def check_category(category, question, cfg=None):
    """베팅 가능하면 None, 아니면 거부 사유."""
    cfg = cfg or load_config()
    pol = cfg["policy"]
    if category not in pol["categories"]:
        return f"category 는 {pol['categories']} 중 하나"
    banned = banned_categories(cfg)
    if category in banned:
        return f"금지 카테고리 '{category}': {banned[category]}"
    for c, pat in pol["banned_category_patterns"].items():
        if c in banned and re.search(pat, question or ""):
            return f"질문이 금지 카테고리 '{c}' 유형에 해당"
    return None


def stake_cap(edge, cfg=None):
    """엣지에 비례한 최대 베팅액: 최소 엣지에서 min_stake, stake_full_at_edge 이상에서 max_stake."""
    cfg = cfg or load_config()
    pol = cfg["policy"]
    lo, hi = pol["min_edge_after_fees"], pol["stake_full_at_edge"]
    frac = 1.0 if edge >= hi else max(0.0, (edge - lo) / (hi - lo)) if hi > lo else 1.0
    return round(max(pol["min_stake_usd"], frac * cfg["max_stake_usd"]), 2)


def summary():
    cfg = load_config()
    pol = cfg["policy"]
    return {"min_edge_after_fees": pol["min_edge_after_fees"],
            "stake_rule": f"엣지 {pol['min_edge_after_fees']} 에서 ${pol['min_stake_usd']}, "
                          f"{pol['stake_full_at_edge']} 이상에서 ${cfg['max_stake_usd']} (선형)",
            "banned_categories": banned_categories(cfg),
            "category_calibration": category_calibration(),
            "categories": pol["categories"]}
