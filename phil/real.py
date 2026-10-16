"""실거래 실행 — 실제 자금을 움직이는 유일한 코드. PROTECTED.

- 에이전트(Claude Code)는 이 모듈을 직접 호출할 수 없다. 사이클이 끝난 뒤
  러너(phil/cycle.py)가 새 모의 베팅 중 '검증된 엣지 클래스'에 속한 것만 복제한다.
- 개인키는 .env 의 PHIL_PRIVATE_KEY 에서만 읽고, 에이전트 프로세스 환경에는 넘기지 않는다.
- 한도: config/protected.json 의 real 블록 + 아래 HARD_* 상한 (설정값이 더 커도 이 값까지만).
- 매수는 멱등이 아니다: 결과가 불분명한 주문이 있으면 정리(reconcile)될 때까지 새 주문 금지.
- journal/real-ledger.jsonl 의 유일한 작성자. 같은 id 의 마지막 행이 최신 상태.
"""
import datetime as dt
import uuid

from . import pmapi
from .config import (LEDGER, REAL_LEDGER, append_jsonl, iso, load_config, load_env,
                     load_jsonl, now_utc, operator_state, parse_ts)
from .score import qualified_edge_classes

HARD_MAX_STAKE = 10.0
HARD_DAILY_CAP = 50.0
HARD_MAX_OPEN = 20
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


class RealError(Exception):
    pass


def _rows():
    last = {}
    for r in load_jsonl(REAL_LEDGER):
        last[r["id"]] = r
    return list(last.values())


def _write(row):
    row["updated"] = iso()
    append_jsonl(REAL_LEDGER, row)


def caps():
    c = load_config()["real"]
    return {"max_stake": min(float(c["max_stake_usd"]), HARD_MAX_STAKE),
            "daily_cap": min(float(c["daily_stake_cap_usd"]), HARD_DAILY_CAP),
            "max_open": min(int(c["max_open_positions"]), HARD_MAX_OPEN),
            "max_slippage": min(float(c.get("max_slippage", 0.02)), 0.05)}


def client():
    env = load_env()
    pk = env.get("PHIL_PRIVATE_KEY")
    if not pk:
        raise RealError(".env 에 PHIL_PRIVATE_KEY 가 없습니다")
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        raise RealError("py-clob-client-v2 미설치: pip install -r requirements-real.txt")
    sig = int(env.get("PHIL_SIGNATURE_TYPE", "1"))
    funder = env.get("PHIL_FUNDER_ADDRESS") or None
    c = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=pk, signature_type=sig, funder=funder)
    c.set_api_creds(c.create_or_derive_api_key())
    return c


def doctor():
    """실거래 경로 점검. 키 값 자체는 절대 출력하지 않는다."""
    env = load_env()
    out = {"real_enabled": operator_state()["real_enabled"],
           "private_key_set": bool(env.get("PHIL_PRIVATE_KEY")),
           "funder_set": bool(env.get("PHIL_FUNDER_ADDRESS")),
           "signature_type": env.get("PHIL_SIGNATURE_TYPE", "1"),
           "caps": caps(), "qualified_edge_classes": qualified_edge_classes()}
    try:
        import py_clob_client_v2  # noqa: F401
        out["library"] = True
    except ImportError:
        out["library"] = False
    if out["library"] and out["private_key_set"]:
        try:
            c = client()
            out["address"] = c.get_address()
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            bal = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            out["usdc_balance"] = round(int(bal.get("balance", 0)) / 1e6, 2)
            out["ready"] = True
        except Exception as e:  # noqa: BLE001
            out["ready"] = False
            out["error"] = str(e)[:300]
    else:
        out["ready"] = False
    return out


def _check_caps(paper, usd, rows):
    cp = caps()
    if any(r["status"] in ("pending", "unknown") for r in rows):
        raise RealError("결과 불분명한 주문이 있어 새 주문 차단 (reconcile 필요)")
    if any(r["market_id"] == paper["market_id"] and r["status"] in ("filled", "won", "lost")
           for r in rows):
        raise RealError("이 마켓은 이미 실거래 포지션 보유")
    if sum(1 for r in rows if r["status"] == "filled") >= cp["max_open"]:
        raise RealError(f"실거래 오픈 포지션 한도({cp['max_open']}) 도달")
    today = now_utc().date().isoformat()
    spent = sum(r.get("usd", 0) for r in rows
                if r["status"] in ("filled", "won", "lost") and r["ts"][:10] == today)
    if spent + usd > cp["daily_cap"] + 1e-9:
        raise RealError(f"일일 한도 초과 (오늘 {spent:.2f} + {usd:.2f} > {cp['daily_cap']})")


def place_twin(paper):
    """모의 베팅 1건에 대한 실거래 복제 주문. 결과 row 반환."""
    st = operator_state()
    if not st["real_enabled"]:
        raise RealError("실거래가 꺼져 있음 (웹앱 설정)")
    if paper["edge_class"] not in qualified_edge_classes():
        raise RealError(f"엣지 클래스 '{paper['edge_class']}' 는 아직 검증되지 않음")
    end = parse_ts(paper["end_date"])
    if not end or end - now_utc() < dt.timedelta(minutes=load_config()["min_minutes_to_resolution"]):
        raise RealError("결과 확정 임박")
    cp = caps()
    usd = cp["max_stake"]
    rows = _rows()
    _check_caps(paper, usd, rows)

    _, ask, _ = pmapi.best_prices(paper["token_id"])
    if ask is None:
        raise RealError("호가 없음")
    limit = round(min(paper["entry_price"] + cp["max_slippage"],
                      load_config()["max_entry_price"]), 2)
    if ask > limit:
        raise RealError(f"현재 ask {ask} 가 허용가 {limit} 초과 (모의 체결가 {paper['entry_price']})")
    if paper["est_prob"] - ask <= 0:
        raise RealError("현재가 기준 엣지 없음")

    c = client()  # 키/라이브러리 문제는 주문 기록 전에 실패
    from py_clob_client_v2 import MarketOrderArgs, OrderType
    row = {"id": uuid.uuid4().hex[:12], "ts": iso(), "paper_id": paper["id"],
           "market_id": paper["market_id"], "question": paper["question"],
           "outcome": paper["outcome"], "token_id": paper["token_id"],
           "edge_class": paper["edge_class"], "usd": usd, "limit_price": limit,
           "ask_at_submit": ask, "status": "pending"}
    _write(dict(row))
    try:
        order = c.create_market_order(MarketOrderArgs(token_id=paper["token_id"], amount=usd,
                                                      side="BUY", price=limit,
                                                      order_type=OrderType.FOK))
        resp = c.post_order(order, OrderType.FOK)
    except Exception as e:  # noqa: BLE001 — 제출 여부 불명: 차단 후 reconcile
        row.update(status="unknown", error=str(e)[:300])
        _write(row)
        return row
    ok = isinstance(resp, dict) and resp.get("success") and resp.get("status") in ("matched", "MATCHED")
    row["order_id"] = (resp or {}).get("orderID") if isinstance(resp, dict) else None
    if ok:
        shares = float(resp.get("takingAmount") or 0) or round(usd / ask, 4)
        spent = float(resp.get("makingAmount") or 0) or usd
        row.update(status="filled", shares=shares, usd=spent,
                   fill_price=round(spent / shares, 4) if shares else ask)
    else:
        row.update(status="rejected", error=str(resp)[:300])
    _write(row)
    return row


def mirror(paper_ids):
    """러너가 사이클 후 호출. 자격 있는 새 모의 베팅만 실거래로 복제."""
    paper = {e["id"]: e for e in load_jsonl(LEDGER)}
    results = []
    for pid in paper_ids:
        p = paper.get(pid)
        if not p or p["status"] != "open":
            continue
        try:
            r = place_twin(p)
            results.append({"paper_id": pid, "status": r["status"], "real_id": r["id"]})
        except RealError as e:
            results.append({"paper_id": pid, "status": "skipped", "reason": str(e)})
    return results


def reconcile():
    """불분명(unknown/pending) 주문을 체결 내역으로 확인."""
    rows = [r for r in _rows() if r["status"] in ("pending", "unknown")]
    if not rows:
        return []
    c = client()
    from py_clob_client_v2 import TradeParams
    out = []
    for r in rows:
        after = int(parse_ts(r["ts"]).timestamp()) - 60
        trades = c.get_trades(TradeParams(asset_id=r["token_id"], after=after))
        if trades:
            t = trades[0]
            size, price = float(t.get("size", 0)), float(t.get("price", 0))
            r.update(status="filled", shares=size, fill_price=price,
                     usd=round(size * price, 4), reconciled=True)
        else:
            r.update(status="failed", reconciled=True)
        _write(r)
        out.append({"id": r["id"], "status": r["status"]})
    return out


def settle():
    """모의 쌍둥이의 공식 결과로 실거래 포지션 손익 기록 (상환은 polymarket.com 에서)."""
    paper = {e["id"]: e for e in load_jsonl(LEDGER)}
    out = []
    for r in _rows():
        if r["status"] != "filled":
            continue
        p = paper.get(r["paper_id"])
        if not p or p["status"] not in ("won", "lost", "void"):
            continue
        if p["status"] == "won":
            r.update(status="won", pnl_usd=round(r["shares"] - r["usd"], 4))
        elif p["status"] == "lost":
            r.update(status="lost", pnl_usd=-r["usd"])
        else:
            r.update(status="void", pnl_usd=0.0)
        _write(r)
        out.append({"id": r["id"], "status": r["status"], "pnl": r["pnl_usd"]})
    return out


def rows():
    return sorted(_rows(), key=lambda r: r["ts"], reverse=True)
