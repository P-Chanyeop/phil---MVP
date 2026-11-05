"""자동 조정 — 성적에 따라 엔진이 스스로 설정을 바꾸고, 나빠지면 되돌린다. PROTECTED.

세 가지:
 1) 방어 모드: 비용 차감 후 최근 10거래일 수익이 -1% 미만이면 한 단계 보수적으로 (매수 크기↓, 기준↑),
    +0.5% 를 넘으면 한 단계 복구.
 2) 비용 절감: 최근 10거래일 Claude 비용이 비용 전 수익보다 크면 자동 반복을 4시간으로, 수익이 비용의 2배를
    넘으면 원래 간격으로.
 3) Phil 의 제안: 정해진 설정만, 정해진 범위 안에서. 바로 '시험' 으로 적용하고 10거래일 뒤 성적을
    직전 10거래일과 비교해 나빠졌으면 자동으로 되돌린다. 한 번에 하나만, 5거래일 간격.
안전장치(손실 한도·실거래·보호 파일)는 어떤 경우에도 자동 조정 대상이 아니다.

기록: journal/tuning.json (overrides, trial, defense, cost_saver, history), journal/daily.jsonl (장 마감 자산).
"""
from .config import (DAILY, TUNING, append_jsonl, iso, load_base_config, load_config, load_json,
                     load_jsonl, now_kst, operator_state, save_json, save_operator_state, _get_path)


class TuneError(Exception):
    pass


def _state():
    s = load_json(TUNING, {})
    s.setdefault("overrides", {})
    s.setdefault("history", [])
    s.setdefault("defense", {"level": 0})
    return s


def _log(s, kind, msg, **kw):
    s["history"] = (s["history"] + [{"ts": iso(), "kind": kind, "msg": msg, **kw}])[-100:]


# ───────────── 매일 장 마감 기록 ─────────────

def record_daily(now=None):
    """하루 한 번: 장 마감 자산, 누적 Claude 비용, 코스피."""
    from . import ledger, naver, score
    now = now or now_kst()
    day = now.date().isoformat()
    if any(r["date"] == day for r in load_jsonl(DAILY)):
        return None
    try:
        kospi = naver.index_quote("KOSPI")["price"]
    except Exception:  # noqa: BLE001
        kospi = None
    row = {"date": day, "equity": ledger.equity(), "cost_usd_total": score.claude_cost()["total_usd"],
           "kospi": kospi}
    append_jsonl(DAILY, row)
    return row


def window(days, end_index=None):
    """최근 days 거래일 성과: (비용 전 손익, 비용, 비용 후 수익률 %) — 데이터 부족이면 None."""
    rows = load_jsonl(DAILY)
    if end_index is not None:
        rows = rows[:end_index + 1]
    if len(rows) < days + 1:
        return None
    a, b = rows[-days - 1], rows[-1]
    rate = load_base_config().get("usd_krw", 1450)
    pnl = b["equity"] - a["equity"]
    cost = (b["cost_usd_total"] - a["cost_usd_total"]) * rate
    return {"from": a["date"], "to": b["date"], "pnl_krw": round(pnl), "cost_krw": round(cost),
            "after_cost_pct": round((pnl - cost) / a["equity"] * 100, 3)}


# ───────────── 1) 방어 모드 ─────────────

def check_defense():
    cfg = load_base_config()["defense"]
    w = window(cfg["window_days"])
    if not w:
        return []
    s = _state()
    lvl = s["defense"].get("level", 0)
    msgs = []
    if w["after_cost_pct"] < cfg["enter_below_pct"] and lvl < 2:
        s["defense"] = {"level": lvl + 1, "since": iso(), "why": w}
        m = (f"🛡️ 방어 모드 {lvl + 1}단계: 최근 {cfg['window_days']}거래일 비용 차감 후 {w['after_cost_pct']:+.2f}% → "
             f"매수 크기를 줄이고 기준을 높였어요")
        _log(s, "defense", m)
        msgs.append(m)
    elif w["after_cost_pct"] > cfg["exit_above_pct"] and lvl > 0:
        s["defense"] = {"level": lvl - 1, "since": iso(), "why": w}
        m = f"🟢 방어 모드 {lvl}→{lvl - 1}단계로 완화: 최근 {cfg['window_days']}거래일 비용 차감 후 {w['after_cost_pct']:+.2f}%"
        _log(s, "defense", m)
        msgs.append(m)
    if msgs:
        save_json(TUNING, s)
    return msgs


# ───────────── 2) 비용 절감 ─────────────

def check_cost():
    cfg = load_base_config()["cost_saver"]
    w = window(cfg["window_days"])
    if not w:
        return []
    s = _state()
    op = operator_state()
    cs = s.get("cost_saver") or {}
    msgs = []
    cur = int(op.get("loop_minutes", 120))
    if not cs.get("active") and w["cost_krw"] > max(w["pnl_krw"], 0) and cur < cfg["slow_minutes"]:
        s["cost_saver"] = {"active": True, "original_minutes": cur, "since": iso(), "why": w}
        op["loop_minutes"] = cfg["slow_minutes"]
        save_operator_state(op)
        m = (f"💸 비용 절감: 최근 {cfg['window_days']}거래일 Claude 비용 {w['cost_krw']:,}원 > 수익 {w['pnl_krw']:,}원 → "
             f"자동 반복 {cur}분 → {cfg['slow_minutes']}분")
        _log(s, "cost", m)
        msgs.append(m)
    elif cs.get("active") and w["pnl_krw"] > cfg["restore_ratio"] * max(w["cost_krw"], 1):
        orig = cs.get("original_minutes", 120)
        s["cost_saver"] = {"active": False, "since": iso()}
        op["loop_minutes"] = orig
        save_operator_state(op)
        m = f"💰 비용 절감 해제: 수익 {w['pnl_krw']:,}원이 비용의 {cfg['restore_ratio']}배 이상 → 자동 반복 {orig}분으로 복구"
        _log(s, "cost", m)
        msgs.append(m)
    if msgs:
        save_json(TUNING, s)
    return msgs


# ───────────── 3) Phil 의 제안 → 시험 → 유지/되돌리기 ─────────────

def params():
    base = load_base_config()
    cur = load_config()
    out = {}
    for path, spec in base["tuning"]["params"].items():
        out[path] = dict(spec, base=_get_path(base, path), current=_get_path(cur, path))
    return out


def propose(path, value, why):
    base = load_base_config()
    tcfg = base["tuning"]
    spec = tcfg["params"].get(path)
    if not spec:
        raise TuneError(f"조정할 수 없는 설정. 가능한 것: {list(tcfg['params'])}")
    if not isinstance(value, (int, float)):
        raise TuneError("값은 숫자")
    value = int(round(value)) if spec.get("int") else round(float(value), 4)
    if not spec["min"] <= value <= spec["max"]:
        raise TuneError(f"{spec['label']} 허용 범위 {spec['min']}~{spec['max']}")
    if not why or len(why.strip()) < 20:
        raise TuneError("근거(why)를 20자 이상, 증거(매매 id·회고·백테스트 수치)와 함께")
    s = _state()
    if s.get("trial"):
        raise TuneError(f"이미 시험 중: {s['trial']['path']} — 끝난 뒤 제안하라")
    rows = load_jsonl(DAILY)
    last = next((h for h in reversed(s["history"]) if h["kind"] in ("trial_start", "trial_keep", "trial_revert")), None)
    if last:
        since = [r for r in rows if r["date"] > last["ts"][:10]]
        if len(since) < tcfg["min_days_between"]:
            raise TuneError(f"최근 조정 후 {tcfg['min_days_between']}거래일이 지나야 새 제안 가능")
    old = _get_path(load_config(), path)
    if old == value:
        raise TuneError("현재 값과 같음")
    s["trial"] = {"path": path, "label": spec["label"], "old": old, "new": value, "why": why.strip()[:500],
                  "start": iso(), "start_index": len(rows) - 1,
                  "before": window(tcfg["trial_days"])}
    s["overrides"][path] = value
    m = f"🧪 설정 시험 시작 · {spec['label']} {old} → {value} ({tcfg['trial_days']}거래일 뒤 성적 비교)"
    _log(s, "trial_start", m, why=why.strip()[:300])
    save_json(TUNING, s)
    return m


def _end_trial(s, keep, reason):
    t = s.pop("trial")
    if not keep:
        if t["old"] == _get_path(load_base_config(), t["path"]):
            s["overrides"].pop(t["path"], None)
        else:
            s["overrides"][t["path"]] = t["old"]
    kind = "trial_keep" if keep else "trial_revert"
    m = (f"✅ 설정 유지 · {t['label']} {t['old']} → {t['new']} ({reason})" if keep
         else f"↩️ 설정 되돌림 · {t['label']} {t['new']} → {t['old']} ({reason})")
    _log(s, kind, m)
    return m


def evaluate_trial():
    s = _state()
    t = s.get("trial")
    if not t:
        return []
    tcfg = load_base_config()["tuning"]
    rows = load_jsonl(DAILY)
    if len(rows) - 1 - t["start_index"] < tcfg["trial_days"]:
        return []
    after = window(tcfg["trial_days"])
    before = t.get("before")
    if not after or not before:
        m = _end_trial(s, True, "비교할 이전 기록이 부족해 유지")
    elif after["after_cost_pct"] < before["after_cost_pct"] - tcfg["revert_if_worse_by_pct"]:
        m = _end_trial(s, False, f"시험 {after['after_cost_pct']:+.2f}% < 이전 {before['after_cost_pct']:+.2f}%")
    else:
        m = _end_trial(s, True, f"시험 {after['after_cost_pct']:+.2f}% vs 이전 {before['after_cost_pct']:+.2f}%")
    save_json(TUNING, s)
    return [m]


def revert_trial_by_operator():
    s = _state()
    if not s.get("trial"):
        return None
    m = _end_trial(s, False, "운영자가 되돌림")
    save_json(TUNING, s)
    return m


def reset_defense_by_operator():
    s = _state()
    s["defense"] = {"level": 0, "since": iso()}
    _log(s, "defense", "운영자가 방어 모드 해제")
    save_json(TUNING, s)


def daily_job():
    """장 마감 후(감시 루프): 기록 → 시험 평가 → 방어 모드 → 비용 절감."""
    record_daily()
    return evaluate_trial() + check_defense() + check_cost()


def summary():
    s = _state()
    return {"defense": s["defense"], "cost_saver": s.get("cost_saver") or {"active": False},
            "trial": s.get("trial"), "overrides": s["overrides"], "history": s["history"][-15:][::-1],
            "window_10d": window(10), "params": params()}
