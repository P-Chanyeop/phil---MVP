"""한국투자증권 Open API — **모의투자 계좌 전용**. PROTECTED.

- 주소는 모의 서버로 고정되어 있다 (실계좌 주문은 이 파일로는 불가능).
- 앱키/시크릿/계좌번호는 .env 에서만 읽고, 에이전트 프로세스에는 넘기지 않는다.
- 접근 토큰은 하루 유효, 발급은 1분에 1번 제한이라 journal/kis-token.json 에 캐시한다.
- 거래 ID 는 한투 공식 저장소(koreainvestment/open-trading-api, examples_llm) 기준:
  매수 VTTC0012U, 매도 VTTC0011U, 잔고 VTTC8434R.
"""
import datetime as dt
import json
import urllib.error
import urllib.request

from .config import KIS_TOKEN, load_env, load_json, save_json

BASE = "https://openapivts.koreainvestment.com:29443"   # 모의투자 서버 (고정)
TR_BUY, TR_SELL, TR_BALANCE = "VTTC0012U", "VTTC0011U", "VTTC8434R"
TR_BUY_OLD, TR_SELL_OLD = "VTTC0802U", "VTTC0801U"     # 구 거래 ID (신규가 거부될 때만)


class KisError(RuntimeError):
    pass


def creds():
    env = load_env()
    key, secret = env.get("PHIL_KIS_APP_KEY"), env.get("PHIL_KIS_APP_SECRET")
    acct = (env.get("PHIL_KIS_ACCOUNT") or "").replace("-", "").strip()
    if not (key and secret and len(acct) == 10 and acct.isdigit()):
        return None
    return {"key": key, "secret": secret, "cano": acct[:8], "prod": acct[8:]}


def configured():
    return creds() is not None


def _http(method, path, headers=None, body=None, params=None, timeout=15):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json; charset=UTF-8", "Accept": "text/plain",
         "User-Agent": "phil-mvp"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            msg = ""
        raise KisError(f"HTTP {e.code} {msg}") from None
    except Exception as e:  # noqa: BLE001
        raise KisError(str(e)) from None


def token():
    c = creds()
    if not c:
        raise KisError(".env 에 PHIL_KIS_APP_KEY / PHIL_KIS_APP_SECRET / PHIL_KIS_ACCOUNT 가 필요합니다")
    cached = load_json(KIS_TOKEN, {})
    if cached.get("token") and cached.get("key_tail") == c["key"][-4:]:
        exp = cached.get("expires", "")
        if exp and dt.datetime.now() < dt.datetime.fromisoformat(exp) - dt.timedelta(minutes=30):
            return cached["token"]
    r = _http("POST", "/oauth2/tokenP", body={"grant_type": "client_credentials",
                                              "appkey": c["key"], "appsecret": c["secret"]})
    tok = r.get("access_token")
    if not tok:
        raise KisError(f"토큰 발급 실패: {r.get('error_description') or r}")
    exp = r.get("access_token_token_expired") or (dt.datetime.now() + dt.timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    save_json(KIS_TOKEN, {"token": tok, "expires": exp.replace(" ", "T"), "key_tail": c["key"][-4:]})
    return tok


def _headers(tr_id):
    c = creds()
    return {"authorization": f"Bearer {token()}", "appkey": c["key"], "appsecret": c["secret"],
            "tr_id": tr_id, "custtype": "P", "tr_cont": ""}


def order(code, qty, side):
    """시장가 주문. side: 'buy' | 'sell'. 성공 시 주문번호, 실패 시 KisError."""
    c = creds()
    body = {"CANO": c["cano"], "ACNT_PRDT_CD": c["prod"], "PDNO": code, "ORD_DVSN": "01",
            "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""}
    new_tr, old_tr = (TR_BUY, TR_BUY_OLD) if side == "buy" else (TR_SELL, TR_SELL_OLD)
    r = _http("POST", "/uapi/domestic-stock/v1/trading/order-cash", _headers(new_tr), body)
    if r.get("rt_cd") != "0" and any(k in (r.get("msg1") or "") for k in ("TR", "tr_id", "거래ID")):
        # 신규 거래 ID 가 거부된 경우에만 구 ID 로 재시도 (거부된 주문은 체결되지 않았으므로 중복 위험 없음)
        old_body = {k: v for k, v in body.items() if k not in ("EXCG_ID_DVSN_CD", "SLL_TYPE", "CNDT_PRIC")}
        r = _http("POST", "/uapi/domestic-stock/v1/trading/order-cash", _headers(old_tr), old_body)
    if r.get("rt_cd") != "0":
        raise KisError(f"{r.get('msg_cd', '')} {r.get('msg1', r)}".strip())
    return (r.get("output") or {}).get("ODNO", "")


def balance():
    """{'holdings': [...], 'summary': {...}} — 모의계좌 잔고."""
    c = creds()
    params = {"CANO": c["cano"], "ACNT_PRDT_CD": c["prod"], "AFHR_FLPR_YN": "N", "OFL_YN": "",
              "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
              "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "",
              "CTX_AREA_NK100": ""}
    r = _http("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", _headers(TR_BALANCE),
              params=params)
    if r.get("rt_cd") != "0":
        raise KisError(f"{r.get('msg_cd', '')} {r.get('msg1', r)}".strip())

    def n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    holdings = [{"code": h.get("pdno"), "name": h.get("prdt_name"), "qty": int(n(h.get("hldg_qty"))),
                 "avg_price": n(h.get("pchs_avg_pric")), "price": n(h.get("prpr")),
                 "eval_krw": n(h.get("evlu_amt")), "pnl_krw": n(h.get("evlu_pfls_amt")),
                 "pnl_pct": n(h.get("evlu_pfls_rt"))}
                for h in r.get("output1", []) if n(h.get("hldg_qty")) > 0]
    s = (r.get("output2") or [{}])[0]
    return {"holdings": holdings,
            "summary": {"cash_krw": n(s.get("dnca_tot_amt")), "eval_krw": n(s.get("scts_evlu_amt")),
                        "total_krw": n(s.get("tot_evlu_amt")), "pnl_krw": n(s.get("evlu_pfls_smtl_amt"))}}


import urllib.parse  # noqa: E402  (위 _http 에서 사용)
