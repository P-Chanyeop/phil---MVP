"""에이전트의 리스크 규칙(strategy/risk.json)을 엔진이 매수 때 강제. PROTECTED.

risk.json 은 에이전트가 회고에서 배운 것을 적는 파일이다. 전에는 화면에 보여 주기만 했지만,
이제 엔진이 읽어서 매수를 거부하거나 크기를 줄인다 — 엔진 한도(config/protected.json)보다
**더 보수적인 방향으로만**. 느슨하게 적어도 엔진 한도를 넘지는 못한다.

지원 항목:
  max_same_sector            같은 업종 동시 보유 한도 (네이버 업종 분류)
  avoid_if_up_today_pct      당일 등락률이 이 값 이상이면 매수 금지 (이미 반영된 급등 추격 방지)
  position_size_pct_of_equity  정규 매수 크기 상한 (엔진 max_position_pct 보다 작을 때만 효과)
  max_new_buys_per_cycle     사이클당 신규 매수 상한 (엔진 한도보다 작을 때만)
  entry_filters              조건을 전부 만족하면 매수 금지:
                             [{"name": "급락 후 반등일", "conditions": [["ret_20d", "<", -15], ["change_today", ">", 5]]}]
                             feature 는 research.features 의 지표 + change_today(당일 등락률), gap_today.
파일이 없거나 깨졌으면 아무것도 강제하지 않는다 (그 사실은 note 로 남김).
"""
import json

from . import market, naver, research
from .config import RISK_META, STRATEGY, load_config, load_json, now_kst, save_json

OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       ">=": lambda a, b: a >= b, "==": lambda a, b: a == b}


class Veto(Exception):
    pass


def load():
    """(risk dict, 문제 설명 or None)"""
    f = STRATEGY / "risk.json"
    if not f.exists():
        return {}, None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return {}, f"strategy/risk.json 을 읽을 수 없음 ({e}) — 강제 안 함"
    return (d if isinstance(d, dict) else {}), None


def enabled(cfg=None):
    cfg = cfg or load_config()
    return bool((cfg["policy"].get("risk_json") or {}).get("enforce", True))


def _num(d, key):
    v = d.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def tighten_limits(lim, risk):
    """엔진 한도 dict 에 risk.json 의 더 보수적인 값을 덮어쓴 사본."""
    out = dict(lim)
    v = _num(risk, "position_size_pct_of_equity")
    if v is not None and 0 < v < out["max_position_pct"]:
        out["max_position_pct"] = v
    v = _num(risk, "max_new_buys_per_cycle")
    if v is not None and 0 < v < out["max_new_positions_per_cycle"]:
        out["max_new_positions_per_cycle"] = int(v)
    return out


def today_features(code, bars, quote, kbars=None):
    """매수 시점 지표: 일봉(오늘 봉이 없으면 현재가로 임시 봉) + 당일 등락률."""
    today = market.kst_date(__import__("phil.config", fromlist=["iso"]).iso())
    b = list(bars)
    price = quote["price"]
    if b and b[-1][0] != today:
        b.append((today, price, price, price, price, 0.0))
    elif b:
        d, o, h, lo, c, v = b[-1]
        b[-1] = (d, o or price, max(h or price, price), min(lo or price, price), price, v)
    kmap = research._kospi_map(kbars) if kbars else {}
    feats = research.features(b, kmap, market.universe().get(code, {}).get("market") == "KOSDAQ")
    f = dict(feats[-1] or {})
    f["change_today"] = quote.get("change_pct") if quote.get("change_pct") is not None else f.get("ret_1d", 0.0)
    f["gap_today"] = f.get("gap_pct", 0.0)
    return f


def _match(conditions, f):
    for cond in conditions:
        if not (isinstance(cond, (list, tuple)) and len(cond) == 3):
            return False
        key, op, val = cond
        if key not in f or op not in OPS or not isinstance(val, (int, float)) or f[key] is None:
            return False
        if not OPS[op](f[key], val):
            return False
    return True


def check(code, quote, bars, open_positions, risk=None, kbars=None):
    """매수 전 검사. 막아야 하면 Veto. 통과하면 {"sector": ..., "notes": [...]}."""
    if risk is None:
        risk, _ = load()
    notes = []
    up = _num(risk, "avoid_if_up_today_pct")
    chg = quote.get("change_pct")
    if up is not None and chg is not None and chg >= up:
        raise Veto(f"risk.json avoid_if_up_today_pct: 오늘 이미 {chg:+.1f}% (기준 {up}%) — 추격 매수 금지")

    sector = market.sector_of(code)   # 보유 기록에 업종을 남겨 두면 나중에 업종별 성적도 볼 수 있다
    mss = _num(risk, "max_same_sector")
    if mss is not None and mss > 0 and sector:
        same = [e["name"] for e in open_positions if (e.get("sector") or {}).get("code") == sector["code"]]
        if len(same) >= mss:
            raise Veto(f"risk.json max_same_sector={int(mss)}: 같은 업종({sector['name']}) 이미 보유 {', '.join(same)}")

    filters = risk.get("entry_filters")
    if isinstance(filters, list) and filters:
        try:
            if kbars is None:
                kbars = naver.daily_bars("KOSPI", 120)
            f = today_features(code, bars, quote, kbars)
        except Exception as e:  # noqa: BLE001
            notes.append(f"entry_filters 지표 계산 실패로 건너뜀: {str(e)[:80]}")
            f = None
        if f:
            for rule in filters:
                if not isinstance(rule, dict):
                    continue
                conds = rule.get("conditions") or []
                if conds and _match(conds, f):
                    shown = ", ".join(f"{k} {op} {v} (지금 {f.get(k):.1f})" for k, op, v in conds)
                    raise Veto(f"risk.json entry_filters '{rule.get('name', '?')}' 에 걸림: {shown}")
    return {"sector": sector, "notes": notes}


def filter_ages(risk=None, now=None):
    """entry_filters 마다 처음 본 날짜·당시 장세를 기록(journal/risk_meta.json)하고 나이·재검증 필요 여부를 돌려준다.
    에이전트가 필터에 "added"(YYYY-MM-DD)·"regime" 을 직접 적었으면 그 값을 쓴다."""
    import datetime as dt
    if risk is None:
        risk, _ = load()
    cfg = load_config()["policy"].get("risk_json") or {}
    stale_days = cfg.get("stale_days", 30)
    meta = load_json(RISK_META, {})
    rg = market.regime_now()
    now_label = rg["label"] if rg else None
    today = (now or now_kst()).date()
    out, changed = [], False
    for f in risk.get("entry_filters") or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        m = meta.get(f["name"])
        if not m:
            m = {"added": str(f.get("added") or today.isoformat())[:10], "regime": f.get("regime") or now_label}
            meta[f["name"]] = m
            changed = True
        try:
            age = (today - dt.date.fromisoformat(m["added"])).days
        except ValueError:
            age = 0
        why = []
        if age >= stale_days:
            why.append(f"{age}일 지남")
        if m.get("regime") and now_label and m["regime"] != now_label:
            why.append(f"만들 때 {m['regime']}, 지금 {now_label}")
        out.append({"name": f["name"], "added": m["added"], "regime": m.get("regime"), "age_days": age,
                    "recheck": bool(why), "why": ", ".join(why)})
    names = {f["name"] for f in out}
    for k in [k for k in meta if k not in names]:   # 지운 필터는 기록에서도 지움
        del meta[k]
        changed = True
    if changed:
        save_json(RISK_META, meta)
    return out


def summary():
    risk, problem = load()
    if not (STRATEGY / "risk.json").exists():
        problem = "strategy/risk.json 없음"
    return {"enforced": enabled(), "problem": problem,
            "limits": {k: risk.get(k) for k in ("max_same_sector", "avoid_if_up_today_pct",
                                                "position_size_pct_of_equity", "max_new_buys_per_cycle")},
            "entry_filters": [r.get("name") for r in risk.get("entry_filters", []) if isinstance(r, dict)],
            "filter_ages": filter_ages(risk)}
