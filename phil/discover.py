"""자동 가설 탐색 — 매일 장 마감 후 엔진이 단순한 가설 수백 개를 과거 3년으로 시험한다 (Claude 비용 0). PROTECTED.

여러 개를 시험할수록 '우연히 좋아 보이는' 규칙이 섞인다 (다중 비교). 그래서
 - 조건 후보는 미리 정한 둥근 숫자만 쓰고 (데이터에 맞춘 미세조정 금지),
 - 통과 기준을 사람이 제안한 규칙보다 엄격하게 하고,
 - 하루 최대 N개만 '그림자' 로 올린다. 실전 승격은 그림자 단계(앞으로의 실제 신호)를 통과해야 한다.
결과는 journal/discovery.json 에 남아 Phil(연구원)이 읽고 다듬는다.
"""
import itertools
import time

from . import research, rules
from .config import JOURNAL, iso, load_config, load_json, save_json

REPORT = JOURNAL / "discovery.json"

PRIMARY = [  # (조건, 사람이 읽는 이름)
    (["ret_1d", ">", 3], "당일 +3% 이상"), (["ret_1d", ">", 6], "당일 +6% 이상"),
    (["ret_1d", "<", -3], "당일 -3% 이하"), (["ret_1d", "<", -6], "당일 -6% 이하"),
    (["ret_5d", ">", 10], "5일 +10% 이상"), (["ret_5d", "<", -10], "5일 -10% 이하"),
    (["ret_20d", ">", 20], "20일 +20% 이상"), (["ret_20d", "<", -20], "20일 -20% 이하"),
    (["from_52w_high", ">", -2], "52주 신고가 근처"), (["from_52w_high", "<", -40], "52주 고점 대비 -40% 이하"),
    (["rsi14", "<", 30], "RSI 30 미만"), (["rsi14", ">", 70], "RSI 70 초과"),
    (["close_vs_ma20", "<", -8], "20일선 -8% 아래"), (["close_vs_ma20", ">", 8], "20일선 +8% 위"),
    (["gap_pct", ">", 3], "시가 갭상승 +3%"), (["gap_pct", "<", -3], "시가 갭하락 -3%"),
    (["close_pos", ">", 0.9], "고가 마감"), (["close_pos", "<", 0.1], "저가 마감"),
]
MODIFIER = [
    (None, ""), (["vol_ratio", ">", 2], "거래량 2배"), (["vol_ratio", ">", 4], "거래량 4배"),
    (["close_vs_ma60", ">", 0], "60일선 위"), (["close_vs_ma60", "<", 0], "60일선 아래"),
    (["kospi_ret_1d", "<", -1.5], "코스피 -1.5% 급락일"), (["kospi_ret_1d", ">", 1], "코스피 +1% 상승일"),
    (["is_kosdaq", ">", 0.5], "코스닥만"), (["is_kosdaq", "<", 0.5], "코스피만"),
]
EXITS = [  # (horizon, stop, target, 이름)
    (1, 0.04, None, "1일"), (3, 0.05, None, "3일"), (3, 0.05, 0.06, "3일·목표6%"), (5, 0.07, None, "5일"),
]


def candidates():
    for (pc, pn), (mc, mn), (h, st, tg, en) in itertools.product(PRIMARY, MODIFIER, EXITS):
        if mc and mc[0] == pc[0]:
            continue
        conds = [pc] + ([mc] if mc else [])
        name = " + ".join(x for x in (pn, mn) if x) + f" → {en}"
        yield name, {"conditions": conds, "horizon": h, "stop": st, "target": tg, "max_per_day": 3,
                     "rank_by": "vol_ratio"}


def strict_pass(bt, cfg):
    t, tr, b = bt["test"], bt["train"], bt["baseline_test"]
    return (bt["verdict"]["pass"]
            and t.get("n", 0) >= cfg["min_test_trades"]
            and (t.get("avg_net_pct") or -9) >= cfg["min_test_avg_net_pct"]
            and (t.get("profit_factor") or 0) >= cfg["min_test_profit_factor"]
            and (tr.get("avg_net_pct") or -9) >= cfg["min_train_avg_net_pct"]
            and (t.get("avg_net_pct") or -9) - (b.get("avg_net_pct") or 0) >= cfg["min_excess_over_baseline_pct"])


def run(max_new=None):
    cfg = load_config()["rules"]["discovery"]
    max_new = cfg["max_new_per_day"] if max_new is None else max_new
    t0 = time.time()
    pre = research.prepare()
    results = []
    for name, spec in candidates():
        try:
            bt = research.backtest(spec, prepared=pre)
        except research.SpecError:
            continue
        results.append({"name": name, "spec": bt["spec"], "train": bt["train"], "test": bt["test"],
                        "baseline_test": bt["baseline_test"], "strict_pass": strict_pass(bt, cfg)})
    passed = sorted([r for r in results if r["strict_pass"]],
                    key=lambda r: -(r["test"]["avg_net_pct"] * min(r["test"]["n"], 100) ** 0.5))
    existing = [r["spec"] for r in rules.all_rules() if r["status"] in ("shadow", "live")]
    auto_active = sum(1 for r in rules.all_rules() if r["status"] in ("shadow", "live") and r.get("source") == "auto")
    added = []
    for r in passed:
        if len(added) >= max_new or auto_active + len(added) >= cfg["max_auto_active"]:
            break
        if r["spec"] in existing or any(r["spec"]["conditions"] == e["conditions"] for e in existing):
            continue
        try:
            rule = rules.add_auto(r["name"], r["spec"], r)
            added.append(rule["name"])
            existing.append(r["spec"])
        except rules.RuleError:
            break
    report = {"ts": iso(), "seconds": round(time.time() - t0), "tested": len(results), "passed": len(passed),
              "added_to_shadow": added,
              "top": [{k: r[k] for k in ("name", "spec", "train", "test", "baseline_test")} for r in passed[:15]],
              "worst": [{k: r[k] for k in ("name", "test")}
                        for r in sorted(results, key=lambda r: r["test"].get("avg_net_pct") or 0)[:5]],
              "note": "여러 가설을 시험한 결과라 우연히 좋아 보이는 것이 섞여 있다. 그림자 단계 성적으로 판단하라."}
    save_json(REPORT, report)
    msgs = [f"🔬 자동 탐색: 가설 {len(results)}개 시험, 엄격 기준 통과 {len(passed)}개"]
    msgs += [f"🧪 그림자 검증 시작 · {n}" for n in added]
    return msgs


def last_report():
    return load_json(REPORT, {})
