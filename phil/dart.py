"""DART 전자공시 감시 — 유니버스 종목의 주요 공시가 뜨면 사이클을 앞당겨 깨운다. PROTECTED.

.env 의 PHIL_DART_API_KEY (opendart.fss.or.kr 무료 발급)가 있을 때만 동작.
"""
import json
import urllib.parse
import urllib.request

from . import market
from .config import DISCLOSURES, append_jsonl, iso, load_config, load_env, load_jsonl, now_kst

API = "https://opendart.fss.or.kr/api/list.json"
STATUS = {"last_poll": None, "error": None}


class DartError(RuntimeError):
    pass


def key():
    return load_env().get("PHIL_DART_API_KEY") or None


def fetch_today(now=None):
    k = key()
    if not k:
        raise DartError(".env 에 PHIL_DART_API_KEY 가 없습니다")
    d = (now or now_kst()).strftime("%Y%m%d")
    out, page = [], 1
    while page <= 5:
        url = API + "?" + urllib.parse.urlencode({"crtfc_key": k, "bgn_de": d, "end_de": d,
                                                  "page_no": page, "page_count": 100})
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                body = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            raise DartError(f"DART 연결 실패: {str(e).replace(k, '***')[:150]}") from None
        st = body.get("status")
        if st == "013":   # 조회된 데이터 없음
            break
        if st != "000":
            raise DartError(f"DART 오류 {st}: {body.get('message')}")
        out += body.get("list", [])
        if page >= int(body.get("total_page") or 1):
            break
        page += 1
    return out


def poll(now=None):
    """새 주요 공시 목록 (유니버스 종목 + 키워드). 기록도 남긴다."""
    now = now or now_kst()
    cfg = load_config()["dart"]
    uni = market.universe()
    seen = {r["rcept_no"] for r in load_jsonl(DISCLOSURES)}
    new = []
    for it in fetch_today(now):
        code = (it.get("stock_code") or "").strip()
        title = it.get("report_nm", "").strip()
        if code not in uni or it.get("rcept_no") in seen:
            continue
        if not any(k in title for k in cfg["keywords"]):
            continue
        row = {"ts": iso(), "rcept_no": it["rcept_no"], "code": code, "name": uni[code]["name"],
               "title": title, "filer": it.get("flr_nm"), "date": it.get("rcept_dt"),
               "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"}
        append_jsonl(DISCLOSURES, row)
        new.append(row)
    STATUS.update(last_poll=iso(), error=None)
    return new


def recent(code=None, n=30):
    rows = load_jsonl(DISCLOSURES)
    if code:
        rows = [r for r in rows if r["code"] == code]
    return rows[-n:][::-1]
