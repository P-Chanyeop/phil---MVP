"""과거 데이터 캐시 · 지표 계산 · 백테스트. PROTECTED.

규칙은 JSON 으로 적는다 (코드 실행 없음):
  {"conditions": [["ret_1d", "<", -5], ["vol_ratio", ">", 2]],
   "horizon": 3, "stop": 0.05, "target": 0.06, "max_per_day": 3, "rank_by": "vol_ratio"}

- 신호일 종가에 매수 (비용 포함), 다음 거래일부터 resolve.py 와 같은 규칙으로 청산
  (갭 손절·손절·갭 목표·목표·만기 종가, 같은 날 둘 다 닿으면 손절).
- 기간을 앞 2/3(학습) / 뒤 1/3(검증)으로 나눠 둘 다 보고한다. 검증 구간이 진짜 성적이다.
- 한계: 유니버스는 '지금의' 시총 상위 300 이라 생존 편향이 있다 (망한 종목은 빠져 있음).
"""
import concurrent.futures as cf
import json
import math

from . import market, naver
from .config import BARS_DIR, load_config, now_kst

FEATURES = {
    "ret_1d": "오늘 등락률 %", "ret_5d": "5일 수익률 %", "ret_20d": "20일 수익률 %",
    "ret_60d": "60일 수익률 %", "gap_pct": "시가 갭 % (시가/전일종가)",
    "vol_ratio": "거래량 / 20일 평균", "close_vs_ma20": "종가의 20일선 대비 %",
    "close_vs_ma60": "종가의 60일선 대비 %", "rsi14": "RSI(14)", "range_pct": "일중 변동폭 %",
    "close_pos": "일중 위치 0~1 (저가=0, 고가=1)", "from_52w_high": "52주 고가 대비 % (0 이하)",
    "from_52w_low": "52주 저가 대비 % (0 이상)", "kospi_ret_1d": "코스피 오늘 등락률 %",
    "kospi_ret_5d": "코스피 5일 수익률 %", "kospi_ret_20d": "코스피 20일 수익률 %",
    "kospi_vs_ma60": "코스피의 60일선 대비 % (양수=상승장, 음수=하락장)", "is_kosdaq": "코스닥이면 1",
}
OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b,
       "<=": lambda a, b: a <= b}


class SpecError(ValueError):
    pass


# ───────────────────────── 데이터 캐시 ─────────────────────────

def _clean(bars):
    """거래정지일 등 시가·고가·저가가 0 인 봉은 종가로 채운다."""
    out = []
    for d, o, h, lo, c, v in bars:
        o, h, lo = (o or c), (h or c), (lo or c)
        out.append((d, o, max(h, o, c), min(lo, o, c), c, v))
    return out


def _cache_path(code):
    return BARS_DIR / f"{code}.json"


def bars_cached(code, count=800, max_age_days=0):
    """일봉 캐시. 오늘 받은 캐시가 있으면 그대로 (장중에는 오늘 봉이 바뀌므로 live=True 로 따로 받는다)."""
    f = _cache_path(code)
    today = now_kst().date().isoformat()
    if f.exists():
        try:
            d = json.loads(f.read_text())
            if d.get("fetched") == today or max_age_days > 0:
                return _clean(d["bars"])
        except ValueError:
            pass
    bars = naver.daily_bars(code, count)
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"fetched": today, "bars": bars}))
    return _clean(bars)


def prefetch(codes=None, workers=8):
    """유니버스 + 지수 일봉을 병렬로 받아 캐시 (서버 시작 시·매일 장 마감 후)."""
    codes = list(codes or market.universe().keys()) + ["KOSPI"]
    ok, fail = 0, []
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(bars_cached, c): c for c in codes}
        for f in cf.as_completed(futs):
            try:
                f.result()
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail.append(f"{futs[f]}: {str(e)[:80]}")
    return {"ok": ok, "failed": fail[:10]}


def load_all(max_age_days=7):
    """{code: bars} — 캐시에 있는 것만 (없으면 prefetch 필요)."""
    out = {}
    uni = market.universe()
    for code in list(uni) + ["KOSPI"]:
        f = _cache_path(code)
        if f.exists():
            try:
                out[code] = _clean(json.loads(f.read_text())["bars"])
            except ValueError:
                pass
    return out, uni


# ───────────────────────── 지표 ─────────────────────────

def features(bars, kospi_by_date, is_kosdaq):
    """bars(오래된 순) 각 날짜의 지표 dict 리스트. 그날까지의 데이터만 쓴다 (미래 정보 없음)."""
    out = []
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    gains, losses = [], []
    for i, (d, o, h, lo, c, v) in enumerate(bars):
        if i < 60:
            if i > 0:
                ch = c - closes[i - 1]
                gains.append(max(ch, 0))
                losses.append(max(-ch, 0))
            out.append(None)
            continue
        ch = c - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
        g, l_ = sum(gains[-14:]) / 14, sum(losses[-14:]) / 14
        avgv = sum(vols[i - 20:i]) / 20
        hi52 = max(b[2] for b in bars[max(0, i - 251):i + 1])
        lo52 = min(b[3] for b in bars[max(0, i - 251):i + 1])
        k = kospi_by_date.get(d)
        out.append({
            "date": d,
            "ret_1d": (c / closes[i - 1] - 1) * 100,
            "ret_5d": (c / closes[i - 5] - 1) * 100,
            "ret_20d": (c / closes[i - 20] - 1) * 100,
            "ret_60d": (c / closes[i - 60] - 1) * 100,
            "gap_pct": (o / closes[i - 1] - 1) * 100 if o else 0.0,
            "vol_ratio": v / avgv if avgv else 0.0,
            "close_vs_ma20": (c / (sum(closes[i - 19:i + 1]) / 20) - 1) * 100,
            "close_vs_ma60": (c / (sum(closes[i - 59:i + 1]) / 60) - 1) * 100,
            "rsi14": 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_),
            "range_pct": (h - lo) / c * 100 if c else 0.0,
            "close_pos": (c - lo) / (h - lo) if h > lo else 0.5,
            "from_52w_high": (c / hi52 - 1) * 100,
            "from_52w_low": (c / lo52 - 1) * 100,
            "kospi_ret_1d": k[0] if k else 0.0,
            "kospi_ret_5d": k[1] if k else 0.0,
            "kospi_ret_20d": k[2] if k else 0.0,
            "kospi_vs_ma60": k[3] if k else 0.0,
            "is_kosdaq": 1 if is_kosdaq else 0,
        })
    return out


def _kospi_map(kbars):
    """날짜 → (코스피 1일, 5일, 20일 수익률 %, 60일선 대비 %) — 시장 분위기 지표."""
    m = {}
    closes = [b[4] for b in kbars]
    for i in range(60, len(kbars)):
        c = closes[i]
        ma60 = sum(closes[i - 59:i + 1]) / 60
        m[kbars[i][0]] = ((c / closes[i - 1] - 1) * 100, (c / closes[i - 5] - 1) * 100,
                          (c / closes[i - 20] - 1) * 100, (c / ma60 - 1) * 100)
    return m


def regime(kbars):
    """지금 시장 분위기 (에이전트·화면용)."""
    k = _kospi_map(kbars)
    if not k:
        return None
    d = max(k)
    r1, r5, r20, vs60 = k[d]
    label = "상승장" if vs60 > 2 else "하락장" if vs60 < -2 else "횡보"
    return {"date": d, "label": label, "kospi_vs_ma60": round(vs60, 2), "kospi_ret_20d": round(r20, 2),
            "kospi_ret_5d": round(r5, 2), "note": "코스피가 60일 평균보다 위면 상승장, 아래면 하락장"}


# ───────────────────────── 규칙 검사 ─────────────────────────

def validate_spec(spec):
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as e:
            raise SpecError(f"JSON 형식 오류: {e}") from None
    lim = load_config()["limits"]
    conds = spec.get("conditions")
    if not isinstance(conds, list) or not 1 <= len(conds) <= 6:
        raise SpecError("conditions 는 1~6개의 [지표, 연산자, 값] 목록")
    for c in conds:
        if not (isinstance(c, list) and len(c) == 3 and c[0] in FEATURES and c[1] in OPS
                and isinstance(c[2], (int, float))):
            raise SpecError(f"잘못된 조건 {c}. 지표: {list(FEATURES)}, 연산자: {list(OPS)}")
    h = spec.get("horizon")
    if not isinstance(h, int) or not lim["horizon_days_min"] <= h <= lim["horizon_days_max"]:
        raise SpecError(f"horizon 은 {lim['horizon_days_min']}~{lim['horizon_days_max']} 정수")
    st = spec.get("stop")
    if not isinstance(st, (int, float)) or not lim["stop_pct_min"] <= st <= lim["stop_pct_max"]:
        raise SpecError(f"stop 은 {lim['stop_pct_min']}~{lim['stop_pct_max']}")
    t = spec.get("target")
    if t is not None and (not isinstance(t, (int, float)) or not 0 < t <= 0.5):
        raise SpecError("target 은 null 또는 0~0.5")
    mpd = spec.get("max_per_day", 3)
    if not isinstance(mpd, int) or not 1 <= mpd <= 5:
        raise SpecError("max_per_day 는 1~5")
    rb = spec.get("rank_by")
    if rb is not None and rb.lstrip("-") not in FEATURES:
        raise SpecError("rank_by 는 지표 이름 (앞에 - 를 붙이면 작은 순)")
    return {"conditions": conds, "horizon": h, "stop": float(st),
            "target": float(t) if t else None, "max_per_day": mpd, "rank_by": rb}


def matches(spec, f):
    return f is not None and all(OPS[op](f[name], val) for name, op, val in spec["conditions"])


def rank_key(spec, f):
    rb = spec.get("rank_by")
    if not rb:
        return 0
    return f[rb.lstrip("-")] if rb.startswith("-") else -f[rb]


# ───────────────────────── 체결 시뮬레이션 ─────────────────────────

def simulate(bars, i, spec, costs):
    """bars[i] 종가 매수 → 이후 청산. (순수익률, 사유, 보유일, 만기종가>매수가) 또는 None(아직 안 끝남)."""
    entry = bars[i][4]
    stop = entry * (1 - spec["stop"])
    tgt = entry * (1 + spec["target"]) if spec["target"] else None
    buy = entry * (1 + costs["slippage"]) * (1 + costs["commission"])
    if i + spec["horizon"] >= len(bars):
        return None
    exit_px, reason = None, None
    for k in range(1, spec["horizon"] + 1):
        _, o, h, lo, c, _ = bars[i + k]
        if o <= stop:
            exit_px, reason = o, "stop_gap"
        elif lo <= stop:
            exit_px, reason = stop, "stop"
        elif tgt and o >= tgt:
            exit_px, reason = o, "target_gap"
        elif tgt and h >= tgt:
            exit_px, reason = tgt, "target"
        elif k == spec["horizon"]:
            exit_px, reason = c, "expiry"
        if exit_px is not None:
            break
    sell = exit_px * (1 - costs["slippage"]) * (1 - costs["commission"] - costs["sell_tax"])
    up = bars[i + spec["horizon"]][4] > entry
    return (sell / buy - 1) * 100, reason, k, up


def _stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for r in rets:  # 같은 금액씩 순서대로 넣었다고 가정한 누적 (% 합)
        eq += r
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    srt = sorted(rets)
    return {"n": n, "win_rate": round(len(wins) / n, 3), "avg_net_pct": round(sum(rets) / n, 3),
            "median_pct": round(srt[n // 2], 3),
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) else None,
            "sum_pct": round(sum(rets), 1), "max_drawdown_pct": round(mdd, 1),
            "up_rate": round(sum(1 for t in trades if t["up"]) / n, 3),
            "days": len({t["date"] for t in trades})}


def prepare(data=None):
    """지표를 한 번만 계산해 둔다 (여러 가설을 빠르게 시험하기 위해)."""
    bars_by, uni = data or load_all()
    kb = bars_by.get("KOSPI")
    if not kb or len(bars_by) < 50:
        raise SpecError("과거 데이터 캐시가 없습니다. 운영자가 `python run.py prefetch` 를 실행해야 합니다.")
    kmap = _kospi_map(kb)
    dates = [b[0] for b in kb]
    rows = []
    for code, bars in bars_by.items():
        if code == "KOSPI" or code not in uni:
            continue
        rows.append((code, bars, features(bars, kmap, uni[code]["market"] == "KOSDAQ")))
    return {"rows": rows, "uni": uni, "dates": dates, "split": dates[int(len(dates) * 2 / 3)]}


def backtest(spec, data=None, prepared=None):
    spec = validate_spec(spec)
    costs = load_config()["costs"]
    pre = prepared or prepare(data)
    uni, dates, split = pre["uni"], pre["dates"], pre["split"]
    bars_by = {code: bars for code, bars, _ in pre["rows"]}
    cand = {}  # date -> [(rank, code, i)]
    bkey = (spec["horizon"], spec["stop"], spec["target"])
    base_cache = pre.setdefault("_base", {})
    base_all = base_cache.get(bkey)
    need_base = base_all is None
    base_all = base_all if base_all is not None else []
    for code, bars, feats in pre["rows"]:
        for i, f in enumerate(feats):
            if f is None:
                continue
            if need_base and i % 7 == 0:  # 기준선: 아무 날·아무 종목이나 같은 방식으로 샀을 때 (표본 추출)
                r = simulate(bars, i, spec, costs)
                if r:
                    base_all.append({"date": f["date"], "ret": r[0], "up": r[3]})
            if matches(spec, f):
                cand.setdefault(f["date"], []).append((rank_key(spec, f), code, i, f["kospi_vs_ma60"]))
    base_cache[bkey] = base_all
    trades = []
    for d, lst in cand.items():
        lst.sort()
        for _, code, i, kv in lst[:spec["max_per_day"]]:
            r = simulate(bars_by[code], i, spec, costs)
            if r:
                trades.append({"date": d, "code": code, "name": uni[code]["name"], "ret": r[0],
                               "reason": r[1], "held": r[2], "up": r[3], "bull": kv > 0})
    trades.sort(key=lambda t: t["date"])
    train = [t for t in trades if t["date"] < split]
    test = [t for t in trades if t["date"] >= split]
    btrain = [t for t in base_all if t["date"] < split]
    btest = [t for t in base_all if t["date"] >= split]
    return {
        "spec": spec,
        "period": {"from": dates[60] if len(dates) > 60 else dates[0], "split": split, "to": dates[-1]},
        "train": _stats(train), "test": _stats(test),
        "baseline_train": _stats(btrain), "baseline_test": _stats(btest),
        "by_regime": {"상승장": _stats([t for t in trades if t["bull"]]),
                      "하락장": _stats([t for t in trades if not t["bull"]])},
        "verdict": verdict(_stats(train), _stats(test), _stats(btest)),
        "recent_test_trades": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}
                               for t in test[-8:]],
        "note": "유니버스가 현재 시총 상위 300 이라 생존 편향이 있음. 검증(test) 구간 성적과 기준선 대비를 보라.",
    }


def verdict(train, test, base_test):
    c = load_config()["rules"]["promote_from_backtest"]
    reasons = []
    if test.get("n", 0) < c["min_test_trades"]:
        reasons.append(f"검증 구간 거래 {test.get('n', 0)}건 < {c['min_test_trades']}")
    if (test.get("avg_net_pct") or -1) < c["min_test_avg_net_pct"]:
        reasons.append(f"검증 평균 순수익 {test.get('avg_net_pct')}% < {c['min_test_avg_net_pct']}%")
    if (test.get("profit_factor") or 0) < c["min_test_profit_factor"]:
        reasons.append(f"검증 손익비 {test.get('profit_factor')} < {c['min_test_profit_factor']}")
    if (train.get("avg_net_pct") or -1) <= 0:
        reasons.append("학습 구간 평균이 0 이하 (우연일 가능성)")
    if base_test.get("n") and (test.get("avg_net_pct") or -9) <= (base_test.get("avg_net_pct") or 0):
        reasons.append("검증 구간에서 아무 종목이나 산 기준선보다 못함")
    return {"pass": not reasons, "reasons": reasons}


def today_signals(spec, live_bars, uni, kbars):
    """오늘(장 막판) 신호. live_bars: {code: 오늘 봉 포함 일봉}."""
    spec = validate_spec(spec)
    kmap = _kospi_map(kbars)
    out = []
    for code, bars in live_bars.items():
        if code not in uni or len(bars) < 70:
            continue
        f = features(bars, kmap, uni[code]["market"] == "KOSDAQ")[-1]
        if matches(spec, f):
            out.append((rank_key(spec, f), code, bars[-1][4], {k: round(v, 2) for k, v in f.items() if k != "date"}))
    out.sort(key=lambda x: x[0])
    return [{"code": c, "price": p, "features": f} for _, c, p, f in out[:spec["max_per_day"]]]


__all__ = ["backtest", "prefetch", "today_signals", "validate_spec", "simulate", "features",
           "FEATURES", "SpecError", "math"]
