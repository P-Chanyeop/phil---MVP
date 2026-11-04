"""주간 감사 자료: 전략 버전별 성적, 이번 주 전략 수정 내역, 규칙 현황, 안전장치. PROTECTED."""
import datetime as dt
from collections import defaultdict

from . import rules, safety
from .config import FORECASTS, LEDGER, STRATEGY_HISTORY, load_jsonl, now_utc, parse_ts


def report(days=14):
    since = now_utc() - dt.timedelta(days=days)
    by_rev = defaultdict(lambda: {"trades": 0, "pnl_krw": 0, "wins": 0, "forecasts": 0,
                                  "fc_hits": 0, "brier_agent": 0.0, "brier_base": 0.0})
    for e in load_jsonl(LEDGER):
        if e["status"] == "closed" and parse_ts(e.get("exit_ts")) and parse_ts(e["exit_ts"]) >= since:
            s = by_rev[e.get("strategy_rev", "?")]
            s["trades"] += 1
            s["pnl_krw"] += e["pnl_krw"]
            s["wins"] += e["pnl_krw"] > 0
    for r in load_jsonl(FORECASTS):
        if r["status"] in ("up", "down") and parse_ts(r["ts"]) >= since:
            s = by_rev[r.get("strategy_rev", "?")]
            y = r["status"] == "up"
            s["forecasts"] += 1
            s["fc_hits"] += (r["est_prob"] > 0.5) == y
            s["brier_agent"] += (r["est_prob"] - y) ** 2
            s["brier_base"] += (r["base_rate"] - y) ** 2
    revs = {}
    for k, s in by_rev.items():
        n = s["forecasts"]
        revs[k] = {"trades": s["trades"], "pnl_krw": s["pnl_krw"], "wins": s["wins"], "forecasts": n,
                   "forecast_hit_rate": round(s["fc_hits"] / n, 3) if n else None,
                   "brier_delta": round((s["brier_agent"] - s["brier_base"]) / n, 4) if n else None}
    edits = [{"ts": h["ts"], "cycle_id": h["cycle_id"], "paths": [d["path"] for d in h["diffs"]],
              "diff_head": "\n".join(d["diff"][:600] for d in h["diffs"])}
             for h in load_jsonl(STRATEGY_HISTORY) if parse_ts(h["ts"]) >= since]
    return {"window_days": days, "by_strategy_rev": revs, "strategy_edits": edits[-12:],
            "rules": rules.summary(), "buy_pause": safety.paused()[1] or None,
            "how_to_read": "strategy_rev 는 매매·예측 당시 playbook+risk 의 해시. 수정 전후 rev 의 "
                           "brier_delta(음수=기저율보다 정확)·손익을 비교해 유지/보완/되돌리기를 정하라."}
