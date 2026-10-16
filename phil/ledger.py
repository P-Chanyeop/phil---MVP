"""모의 브로커: 모의 베팅 체결과 뱅크롤 관리. PROTECTED.

체결은 정직하게: 매수는 실시간 CLOB 최우선 매도호가(best ask)에 체결된다
(스프레드를 건너는 테이커 주문과 동일). journal/ledger.jsonl 의 유일한 작성자.
원본: bennyjo/phil core/ledger.py (Apache-2.0) 기반.
"""
import os
import re
import uuid

from . import pmapi
from .config import LEDGER, append_jsonl, iso, load_config, load_jsonl, now_utc, parse_ts


class Rejected(Exception):
    pass


def read_ledger():
    return load_jsonl(LEDGER)


def cash(entries, cfg=None):
    cfg = cfg or load_config()
    c = cfg["sim_bankroll_usd"]
    for e in entries:
        c -= e["stake_usd"]
        if e["status"] == "won":
            c += e["shares"]
        elif e["status"] == "void":
            c += e["stake_usd"]
    return round(c, 4)


def place(market_id, outcome, est_prob, stake, category, edge_class, rationale,
          cycle_id=None):
    cfg = load_config()
    entries = read_ledger()
    open_pos = [e for e in entries if e["status"] == "open"]
    cycle_id = cycle_id or os.environ.get("PHIL_CYCLE_ID") or "manual"

    if len(open_pos) >= cfg["max_open_positions"]:
        raise Rejected(f"최대 오픈 포지션({cfg['max_open_positions']}) 도달")
    if cycle_id != "manual" and sum(1 for e in entries if e.get("cycle_id") == cycle_id) \
            >= cfg["max_new_positions_per_cycle"]:
        raise Rejected(f"사이클당 신규 포지션 한도({cfg['max_new_positions_per_cycle']}) 도달")
    if not 0 < stake <= cfg["max_stake_usd"]:
        raise Rejected(f"stake 는 0 초과 {cfg['max_stake_usd']} 이하여야 함")
    if stake > cash(entries, cfg):
        raise Rejected("모의 현금 부족")
    if not 0.0 < est_prob < 1.0:
        raise Rejected("est-prob 는 (0,1) 범위")
    if edge_class not in cfg["edge_classes"]:
        raise Rejected(f"edge-class 는 {cfg['edge_classes']} 중 하나")
    if not rationale or len(rationale.strip()) < 10:
        raise Rejected("rationale(근거)를 한 줄 이상 적어야 함")
    if any(e["market_id"] == str(market_id) for e in open_pos):
        raise Rejected("이 마켓에 이미 오픈 포지션이 있음")

    m = pmapi.gamma_market(market_id)
    q = m.get("question") or ""
    if m.get("closed") or not m.get("active", True):
        raise Rejected("마감된 마켓")
    for pat in cfg["banned_question_patterns"]:
        if re.search(pat, q):
            raise Rejected("금지된 마켓 유형 (초단기 코인 업/다운)")
    end = parse_ts(m.get("endDate"))
    if end is None:
        raise Rejected("마감 시각을 알 수 없음")
    mins = (end - now_utc()).total_seconds() / 60
    if mins < cfg["min_minutes_to_resolution"]:
        raise Rejected(f"결과 확정까지 {cfg['min_minutes_to_resolution']}분 미만")
    tokens = pmapi.market_tokens(m)
    if tokens is None:
        raise Rejected("CLOB 토큰을 읽을 수 없는 마켓")
    if outcome not in tokens:
        raise Rejected(f"outcome {outcome!r} 은 {list(tokens)} 중 하나여야 함")
    bid, ask, _ = pmapi.best_prices(tokens[outcome])
    if ask is None:
        raise Rejected("호가창에 매도 물량 없음 (체결 불가)")
    if not cfg["min_entry_price"] <= ask <= cfg["max_entry_price"]:
        raise Rejected(f"체결가 {ask} 가 허용 범위 밖")

    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": iso(),
        "cycle_id": cycle_id,
        "market_id": str(market_id),
        "question": q,
        "slug": m.get("slug"),
        "end_date": m.get("endDate"),
        "outcome": outcome,
        "token_id": tokens[outcome],
        "entry_price": ask,
        "best_bid_at_entry": bid,
        "est_prob": round(est_prob, 4),
        "edge": round(est_prob - ask, 4),
        "stake_usd": round(stake, 2),
        "shares": round(stake / ask, 4),
        "category": category,
        "edge_class": edge_class,
        "rationale": rationale.strip(),
        "status": "open",
    }
    append_jsonl(LEDGER, entry)
    return entry


def status():
    cfg = load_config()
    entries = read_ledger()
    open_pos = [e for e in entries if e["status"] == "open"]
    settled = [e for e in entries if e["status"] in ("won", "lost")]
    return {
        "cash": cash(entries, cfg),
        "bankroll_start": cfg["sim_bankroll_usd"],
        "open_positions": len(open_pos),
        "open_stake": round(sum(e["stake_usd"] for e in open_pos), 2),
        "settled": len(settled),
        "wins": sum(1 for e in settled if e["status"] == "won"),
        "realized_pnl": round(sum(e.get("pnl_usd", 0) for e in settled), 2),
        "open": [{"id": e["id"], "market_id": e["market_id"], "q": e["question"][:90],
                  "outcome": e["outcome"], "entry": e["entry_price"],
                  "est": e["est_prob"], "stake": e["stake_usd"], "ends": e["end_date"],
                  "edge_class": e["edge_class"]} for e in open_pos],
    }
