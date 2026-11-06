"""엔진이 강제하는 매매 정책. PROTECTED.

- 엣지 = 내 확률 − 기저율(그 종목 최근 250거래일의 N일 상승 비율) ≥ min_edge.
- 근거 유형(edge class)별로 예측 정확도를 재서, 기저율보다 확실히 못하면 자동 금지.
"""
from collections import defaultdict

from .config import FORECASTS, LEDGER, load_config, load_jsonl


def _rows():
    """정확도 측정용 (est_prob, base_rate, outcome, edge_class) — 예측 + 만기 지난 매매."""
    out = []
    for r in load_jsonl(FORECASTS):
        if r["status"] in ("up", "down"):
            out.append((r["est_prob"], r["base_rate"], r["status"] == "up", r["edge_class"]))
    for e in load_jsonl(LEDGER):
        if "calib" in e:
            out.append((e["est_prob"], e["base_rate"], e["calib"] == "up", e["edge_class"]))
    return out


def brier(rows):
    n = len(rows)
    if not n:
        return None
    a = sum((p - y) ** 2 for p, _, y, _ in rows) / n
    b = sum((br - y) ** 2 for _, br, y, _ in rows) / n
    return {"n": n, "brier_agent": round(a, 4), "brier_base": round(b, 4),
            "brier_delta": round(a - b, 4),
            "hit_rate": round(sum(1 for p, _, y, _ in rows if (p > 0.5) == y) / n, 3)}


def calibration_by_class():
    g = defaultdict(list)
    for r in _rows():
        g[r[3]].append(r)
    return {k: brier(v) for k, v in sorted(g.items())}


def calibration_overall():
    return brier(_rows()) or {"n": 0}


def banned_edge_classes(cfg=None):
    cfg = cfg or load_config()
    ab = cfg["policy"]["auto_ban"]
    out = {}
    for k, s in calibration_by_class().items():
        if s and s["n"] >= ab["min_n"] and s["brier_delta"] > ab["max_brier_delta"]:
            out[k] = f"내 기록 {s['n']}건에서 기저율보다 부정확 (brier Δ {s['brier_delta']:+.4f})"
    return out


def check_edge_class(edge_class, cfg=None):
    cfg = cfg or load_config()
    if edge_class not in cfg["policy"]["edge_classes"]:
        return f"edge-class 는 {cfg['policy']['edge_classes']} 중 하나"
    banned = banned_edge_classes(cfg)
    if edge_class in banned:
        return f"자동 금지된 근거 유형 '{edge_class}': {banned[edge_class]}"
    return None


def size_factor(edge_class, cfg=None):
    """근거 유형의 예측 정확도에 따라 정규 매수 크기를 조절.
    반환 (max_pct 또는 None, factor, 설명). max_pct 가 있으면 그 비율(자산 대비)로 상한, factor 는 곱."""
    cfg = cfg or load_config()
    sa = cfg["policy"].get("sizing_by_accuracy")
    if not sa:
        return None, 1.0, ""
    s = calibration_by_class().get(edge_class)
    n = s["n"] if s else 0
    if n < sa["min_n"]:
        return sa["unproven_pct"], 1.0, f"미검증 근거 유형 '{edge_class}' (정산 {n}건 < {sa['min_n']}) → 자산의 {sa['unproven_pct']:.0%} 까지"
    if s["brier_delta"] > 0:
        return None, sa["worse_factor"], f"'{edge_class}' 는 기저율보다 부정확 (Δ {s['brier_delta']:+.4f}, {n}건) → 크기 x{sa['worse_factor']}"
    return None, 1.0, f"'{edge_class}' 기저율보다 정확 (Δ {s['brier_delta']:+.4f}, {n}건) → 원래 크기"


def summary():
    cfg = load_config()
    lim = cfg["limits"]
    return {
        "rules": [
            f"정규 매수: 엣지(내 확률 − 기저율) ≥ {cfg['policy']['min_edge_vs_base_rate']}",
            (lambda x: f"탐색 매수: 엣지 {x['min_edge']}~{cfg['policy']['min_edge_vs_base_rate']} 이면 자산의 "
                       f"{x['position_pct']:.0%} 로 작게, 하루 {x['max_per_day']}건까지")(cfg['policy']['exploration'])
            if cfg['policy'].get('exploration') else "탐색 매수 없음",
            f"종목당 최대 자산의 {lim['max_position_pct']:.0%}, 동시 보유 {lim['max_open_positions']}종목, "
            f"사이클당 신규 {lim['max_new_positions_per_cycle']}종목",
            f"보유 {lim['horizon_days_min']}~{lim['horizon_days_max']}거래일, 손절 {lim['stop_pct_min']:.0%}~{lim['stop_pct_max']:.0%} 필수",
            f"매매·예측은 평일 {lim['trade_start']}~{lim['trade_end']} 장중에만",
            "유니버스: 코스피 시총 상위 200 + 코스닥 상위 100 (우선주·스팩 제외)",
            "비용: 매수·매도 수수료 0.015%, 슬리피지 0.05%, 매도 시 증권거래세 0.20%",
            "근거 유형별 정확도 → 매수 크기: 정산 10건 미만 유형은 자산의 5% 까지, 기저율보다 부정확한 유형은 절반",
            "strategy/risk.json 의 max_same_sector·avoid_if_up_today_pct·entry_filters·크기 상한을 엔진이 강제 (더 보수적인 방향만)",
        ],
        "edge_classes": cfg["policy"]["edge_classes"],
        "banned_edge_classes": banned_edge_classes(cfg),
        "calibration_by_edge_class": calibration_by_class(),
        "sizing_by_accuracy": {k: size_factor(k, cfg)[2] for k in cfg["policy"]["edge_classes"] if k != "rule"},
        "risk_json": _risk_summary(),
    }


def _risk_summary():
    try:
        from . import riskbook
        return riskbook.summary()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:100]}
