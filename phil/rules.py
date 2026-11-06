"""규칙 등록부 — 연구원(Claude)이 제안하고, 엔진이 검증·승격·실행·은퇴시킨다. PROTECTED.

수명주기:
  propose → 백테스트(학습/검증 분할) 통과 → shadow (실제 시장에서 돈 없이 신호만 기록)
          → 그림자 신호가 충분히 쌓이고 성적이 좋으면 live (엔진이 매일 15:05 에 직접 모의 매수)
          → 최근 성적이 나빠지면 retired.
  에이전트는 제안과 은퇴만 할 수 있고, 승격은 엔진만 한다.
"""
import concurrent.futures as cf
import uuid

from . import ledger, market, naver, research
from .config import (RULE_SIGNALS, RULES, append_jsonl, dump_jsonl, iso, ledger_lock, load_config,
                     load_json, load_jsonl, now_kst, save_json)


class RuleError(Exception):
    pass


def _load():
    return load_json(RULES, {"rules": []})


def _save(reg):
    save_json(RULES, reg)


def all_rules():
    return _load()["rules"]


def get(rule_id):
    return next((r for r in all_rules() if r["id"] == rule_id), None)


def _set_status(reg, r, status, why):
    r["status"] = status
    r.setdefault("history", []).append({"ts": iso(), "status": status, "why": why})


def propose(name, spec, why):
    cfg = load_config()["rules"]
    spec = research.validate_spec(spec)
    reg = _load()
    today = now_kst().date().isoformat()
    if sum(1 for r in reg["rules"] if r["created"][:10] == today) >= 5:
        raise RuleError("오늘 규칙 제안 한도(5개) 도달")
    if sum(1 for r in reg["rules"] if r["status"] in ("shadow", "live")) >= cfg["max_rules"]:
        raise RuleError(f"활성 규칙 한도({cfg['max_rules']}) — 먼저 약한 규칙을 은퇴시켜라")
    if any(r["spec"] == spec and r["status"] in ("shadow", "live") for r in reg["rules"]):
        raise RuleError("같은 규칙이 이미 활성 상태")
    bt = research.backtest(spec, attempts=research.record_attempt(spec))
    r = {"id": uuid.uuid4().hex[:8], "name": name.strip()[:60], "why": why.strip()[:500],
         "spec": spec, "created": iso(),
         "backtest": {k: bt[k] for k in ("period", "train", "test", "baseline_test", "verdict")}}
    _set_status(reg, r, "shadow" if bt["verdict"]["pass"] else "rejected",
                "백테스트 통과" if bt["verdict"]["pass"] else "; ".join(bt["verdict"]["reasons"]))
    reg["rules"].append(r)
    _save(reg)
    return r


def add_auto(name, spec, bt):
    """자동 탐색이 찾은 규칙을 바로 그림자로 (이미 엄격한 백테스트를 통과함)."""
    reg = _load()
    r = {"id": uuid.uuid4().hex[:8], "name": "[자동] " + name[:55], "source": "auto",
         "why": "엔진 자동 탐색: 과거 3년 학습·검증 구간 모두 기준선보다 좋았음 (다중 비교 주의)",
         "spec": research.validate_spec(spec), "created": iso(),
         "backtest": {"train": bt["train"], "test": bt["test"], "baseline_test": bt["baseline_test"],
                      "verdict": {"pass": True, "reasons": []}, "period": {}}}
    _set_status(reg, r, "shadow", "자동 탐색 엄격 기준 통과")
    reg["rules"].append(r)
    _save(reg)
    return r


def retire(rule_id, why):
    reg = _load()
    r = next((x for x in reg["rules"] if x["id"] == rule_id), None)
    if not r or r["status"] not in ("shadow", "live"):
        raise RuleError("활성 규칙 id 가 아님")
    _set_status(reg, r, "retired", f"에이전트: {why.strip()[:200]}")
    _save(reg)
    return r


# ───────────── 매일 15:05: 오늘 신호 (그림자 기록 + 실전 매수) ─────────────

def _live_bars(codes, workers=8):
    out = {}

    def get(c):
        return c, naver.daily_bars(c, 300)
    with cf.ThreadPoolExecutor(workers) as ex:
        for f in cf.as_completed([ex.submit(get, c) for c in codes]):
            try:
                c, b = f.result()
                out[c] = research._clean(b)
            except Exception:  # noqa: BLE001
                pass
    return out


def run_signals(now=None):
    now = now or now_kst()
    active = [r for r in all_rules() if r["status"] in ("shadow", "live")]
    if not active:
        return []
    uni = market.universe()
    bars = _live_bars(list(uni) + ["KOSPI"])
    kb = bars.pop("KOSPI", None)
    if not kb or kb[-1][0] != now.date().isoformat():
        return [{"error": "오늘 코스피 일봉이 없어 신호 계산을 건너뜀"}]
    today = now.date().isoformat()
    done = {(s["rule_id"], s["code"]) for s in load_jsonl(RULE_SIGNALS) if s["date"] == today}
    events = []
    for r in active:
        sigs = research.today_signals(r["spec"], bars, uni, kb)
        for s in sigs:
            if (r["id"], s["code"]) in done:
                continue
            row = {"id": uuid.uuid4().hex[:10], "rule_id": r["id"], "rule": r["name"], "date": today,
                   "code": s["code"], "name": uni[s["code"]]["name"], "price": s["price"],
                   "mode": r["status"], "status": "open"}
            if r["status"] == "live":
                try:
                    e = ledger.rule_buy(r, s["code"], uni[s["code"]]["name"], s["price"], bars[s["code"]])
                    row["position_id"] = e["id"]
                    events.append(f"🤖 규칙 '{r['name']}' 매수 · {e['name']} {e['qty']}주")
                except ledger.Rejected as err:
                    row["skipped"] = str(err)[:150]
            append_jsonl(RULE_SIGNALS, row)
    return events


# ───────────── 매일 장 마감 후: 그림자 신호 채점, 승격·은퇴 ─────────────

def evaluate():
    cfg = load_config()
    rc = cfg["rules"]
    bars_by, uni = research.load_all()
    events = []
    with ledger_lock():
        sigs = load_jsonl(RULE_SIGNALS)
        changed = False
        for s in sigs:
            if s["status"] != "open" or s["code"] not in bars_by:
                continue
            r = get(s["rule_id"])
            if not r:
                continue
            bars = bars_by[s["code"]]
            idx = next((i for i, b in enumerate(bars) if b[0] == s["date"]), None)
            if idx is None:
                continue
            res = research.simulate(bars, idx, r["spec"], cfg["costs"])
            if res:
                s.update(status="done", ret=round(res[0], 3), reason=res[1], up=res[3])
                changed = True
        if changed:
            dump_jsonl(RULE_SIGNALS, sigs)
    reg = _load()
    entries = ledger.read_ledger()
    for r in reg["rules"]:
        done = [s for s in sigs if s["rule_id"] == r["id"] and s["status"] == "done"]
        r["forward"] = {"n": len(done), "avg_net_pct": round(sum(s["ret"] for s in done) / len(done), 3) if done else None,
                        "win_rate": round(sum(1 for s in done if s["ret"] > 0) / len(done), 3) if done else None}
        trades = [e for e in entries if e.get("rule_id") == r["id"] and e["status"] == "closed"]
        r["live_trades"] = {"n": len(trades), "pnl_krw": sum(e["pnl_krw"] for e in trades),
                            "avg_ret_pct": round(sum(e["ret_pct"] for e in trades) / len(trades), 3) if trades else None}
        if r["status"] == "shadow":
            p = rc["promote_from_shadow"]
            n_live = sum(1 for x in reg["rules"] if x["status"] == "live")
            if r["forward"]["n"] >= p["min_signals"] and (r["forward"]["avg_net_pct"] or -9) >= p["min_avg_net_pct"] \
                    and n_live < rc["live"]["max_live_rules"]:
                _set_status(reg, r, "live", f"그림자 신호 {r['forward']['n']}건 평균 {r['forward']['avg_net_pct']}%")
                events.append(f"🚀 규칙 '{r['name']}' 실전 승격 (그림자 {r['forward']['n']}건, 평균 {r['forward']['avg_net_pct']}%)")
        elif r["status"] == "live":
            w = rc["retire"]["window"]
            recent = [s for s in done][-w:]
            if len(recent) >= w and sum(s["ret"] for s in recent) / w < rc["retire"]["min_avg_net_pct"]:
                avg = sum(s["ret"] for s in recent) / w
                _set_status(reg, r, "retired", f"최근 {w}건 평균 {avg:.2f}% — 성적 악화")
                events.append(f"🪦 규칙 '{r['name']}' 은퇴 (최근 {w}건 평균 {avg:.2f}%)")
    _save(reg)
    return events


def summary():
    out = []
    for r in all_rules():
        out.append({"id": r["id"], "name": r["name"], "status": r["status"], "spec": r["spec"],
                    "why": r.get("why", ""), "backtest_test": r.get("backtest", {}).get("test", {}),
                    "backtest_verdict": r.get("backtest", {}).get("verdict", {}), "forward": r.get("forward"),
                    "live_trades": r.get("live_trades"), "history": r.get("history", [])[-3:]})
    return out
