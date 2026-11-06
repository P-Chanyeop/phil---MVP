"""엔진 단위 테스트 (네트워크 없음): python -m unittest discover tests"""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from phil import (config, dart, forecast, guard, kis, ledger, market, notify, park, policy, research,  # noqa: E402
                  resolve, rules, safety, score, tuning, watch)

KST = config.KST
OPEN_TIME = dt.datetime(2026, 10, 14, 10, 0, tzinfo=KST)  # 수요일 장중


def bars(start="2026-10-13", closes=(100,), ohlc=None):
    """테스트용 일봉: 날짜는 start 부터 하루씩."""
    d0 = dt.date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        o, h, lo = (ohlc[i] if ohlc else (c, c, c))
        out.append(((d0 + dt.timedelta(days=i)).isoformat(), o, h, lo, c, 1000))
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        for mod, name, f in [(config, "LEDGER", "l.jsonl"), (ledger, "LEDGER", "l.jsonl"),
                             (resolve, "LEDGER", "l.jsonl"), (score, "LEDGER", "l.jsonl"),
                             (policy, "LEDGER", "l.jsonl"), (policy, "FORECASTS", "f.jsonl"),
                             (score, "FORECASTS", "f.jsonl"), (forecast, "FORECASTS", "f.jsonl"),
                             (resolve, "FORECASTS", "f.jsonl"), (score, "META", "m.json"),
                             (watch, "KIS_LEDGER", "k.jsonl"), (watch, "WATCH_EVENTS", "w.jsonl"),
                             (config, "LOCK_FILE", ".lock"), (notify, "LEDGER", "l.jsonl"),
                             (notify, "FORECASTS", "f.jsonl"), (notify, "NOTIFY_STATE", "n.json"),
                             (rules, "RULES", "rules.json"), (rules, "RULE_SIGNALS", "sig.jsonl"),
                             (safety, "META", "m.json"), (dart, "DISCLOSURES", "d.jsonl"),
                             (park, "PARK", "park.json"), (score, "CYCLES_LOG", "cyc.jsonl"),
                             (config, "TUNING", "tuning.json"), (tuning, "TUNING", "tuning.json"),
                             (tuning, "DAILY", "daily.jsonl"), (config, "OPERATOR", "op.json")]:
            mock.patch.object(mod, name, self.d / f).start()
        mock.patch("phil.market.universe", lambda refresh=False: {
            "005930": {"name": "삼성전자", "market": "KOSPI", "price": 100}}).start()
        mock.patch("phil.market.trading_window", lambda now=None: (True, "장중")).start()
        mock.patch("phil.naver.quote", lambda c: {"code": c, "name": "삼성전자", "price": 100.0,
                                                  "market_status": "OPEN", "tradable": True}).start()
        # 기저율 0.5 가 나오도록 오르내림 반복
        mock.patch("phil.naver.daily_bars", lambda c, n=260: bars("2026-01-01", [100, 101] * 100)).start()
        mock.patch("phil.naver.index_quote", lambda s="KOSPI": {"symbol": s, "name": s, "price": 5000.0,
                                                               "change_pct": 0.0, "market_status": "OPEN"}).start()
        mock.patch("phil.naver.sector", lambda c: {"code": "278", "name": "반도체와반도체장비"}).start()
        mock.patch.object(config, "SECTORS", self.d / "sectors.json").start()
        mock.patch.object(market, "_REGIME", {"t": 0.0, "value": None}).start()
        (self.d / "strategy").mkdir()
        mock.patch("phil.riskbook.STRATEGY", self.d / "strategy").start()

    def risk(self, **kw):
        """테스트용 strategy/risk.json"""
        import json
        (self.d / "strategy" / "risk.json").write_text(json.dumps(kw), encoding="utf-8")

    def tearDown(self):
        mock.patch.stopall()


class TestCosts(unittest.TestCase):
    def test_round_trip(self):
        c = {"commission": 0.00015, "sell_tax": 0.002, "slippage": 0.0005}
        _, cost = ledger.buy_cost(10000, 100, c)
        _, proc = ledger.sell_proceeds(10000, 100, c)
        self.assertEqual(cost, 1000650)       # 1,000,500 + 수수료 150
        self.assertEqual(proc, 997351)        # 999,500 − 150 − 1,999
        self.assertAlmostEqual((cost - proc) / 1e6, 0.0034, places=3)

    def test_base_rate(self):
        self.assertAlmostEqual(market.base_rate(bars(closes=[1, 2] * 50), 1), 0.5, delta=0.01)
        self.assertGreater(market.base_rate(bars(closes=list(range(1, 100))), 3), 0.95)


class TestNaverSign(unittest.TestCase):
    def test_signed_pct(self):
        from phil import naver
        self.assertEqual(naver.signed_pct("-6.03", {"name": "FALLING"}), -6.03)
        self.assertEqual(naver.signed_pct("6.03", {"name": "FALLING"}), -6.03)
        self.assertEqual(naver.signed_pct("3.33", {"name": "RISING"}), 3.33)
        self.assertEqual(naver.signed_pct("0.00", {"name": "UNCHANGED"}), 0.0)


class TestBuy(Tmp):
    def buy(self, est=0.6, **kw):
        args = dict(code="005930", est_prob=est, horizon_days=3, stop_pct=0.04, target_pct=0.06,
                    edge_class="earnings", rationale="실적 서프라이즈 테스트 근거")
        args.update(kw)
        return ledger.buy(**args)

    def test_buy_ok_and_sizing(self):
        e = self.buy()
        self.assertAlmostEqual(e["base_rate"], 0.5, delta=0.01)
        self.assertLessEqual(e["cost_krw"], 2_000_000)   # 자산의 20%
        self.assertEqual(e["stop_price"], 96.0)
        self.assertEqual(e["target_price"], 106.0)
        with self.assertRaises(ledger.Rejected):  # 중복 보유
            self.buy()

    def test_edge_required(self):
        with self.assertRaises(ledger.Rejected):  # 엣지 0.0175 < 탐색 최소 0.02
            self.buy(est=0.52)

    def test_exploration_buy_small_and_capped(self):
        e = self.buy(est=0.54)          # 엣지 ≈ 0.0375: 정규(0.05) 미만, 탐색(0.02) 이상
        self.assertTrue(e["exploration"])
        self.assertLessEqual(e["cost_krw"], 500_000)   # 자산의 5%
        codes = ["000660", "035420", "051910"]
        uni = {c: {"name": c, "market": "KOSPI", "price": 100} for c in ["005930"] + codes}
        with mock.patch("phil.market.universe", lambda refresh=False: uni):
            self.buy(est=0.54, code="000660")
            self.buy(est=0.54, code="035420")
            with self.assertRaises(ledger.Rejected):   # 하루 3건 한도
                self.buy(est=0.54, code="051910")
            big = self.buy(est=0.62, code="051910")    # 정규 매수는 한도와 무관
        self.assertFalse(big["exploration"])
        self.assertLessEqual(big["cost_krw"], 500_000)     # 미검증 근거 유형(정산 0건)은 자산의 5% 까지
        self.assertIn("미검증", big["size_notes"][0])

    def _settled_forecasts(self, edge_class, n, good):
        """정산된 예측 n건: good 이면 기저율보다 정확(맞힌 쪽에 0.7), 아니면 부정확(틀린 쪽에 0.7)."""
        for i in range(n):
            up = i % 2 == 0
            p = (0.7 if up else 0.3) if good else (0.3 if up else 0.7)
            config.append_jsonl(forecast.FORECASTS, {"id": f"f{i}", "code": "005930", "est_prob": p, "base_rate": 0.5,
                                                     "edge_class": edge_class, "status": "up" if up else "down"})

    def test_sizing_follows_accuracy(self):
        self._settled_forecasts("earnings", 12, good=True)
        e = self.buy(est=0.62)
        self.assertGreater(e["cost_krw"], 1_500_000)        # 검증된 유형은 원래 크기(20%)
        self.assertIn("정확", e["size_notes"][0])
        self._settled_forecasts("news-flow", 12, good=False)
        uni = {c: {"name": c, "market": "KOSPI", "price": 100} for c in ("005930", "000660")}
        with mock.patch("phil.market.universe", lambda refresh=False: uni):
            w = self.buy(est=0.62, code="000660", edge_class="news-flow")
        self.assertLessEqual(w["cost_krw"], 1_000_000)      # 부정확한 유형은 절반(10%)
        self.assertIn("x0.5", w["size_notes"][0])

    def test_risk_json_enforced(self):
        uni = {c: {"name": c, "market": "KOSPI", "price": 100} for c in ("005930", "000660", "035420")}
        mock.patch("phil.market.universe", lambda refresh=False: uni).start()
        self.risk(max_same_sector=1, avoid_if_up_today_pct=8, position_size_pct_of_equity=0.03)
        e = self.buy()
        self.assertEqual(e["sector"]["name"], "반도체와반도체장비")
        self.assertLessEqual(e["cost_krw"], 300_000)         # risk.json 크기 상한(3%)이 엔진(20%)보다 작으면 적용
        with self.assertRaises(ledger.Rejected) as cm:       # 같은 업종 2번째
            self.buy(code="000660")
        self.assertIn("같은 업종", str(cm.exception))
        self.risk(avoid_if_up_today_pct=8)
        with mock.patch("phil.naver.quote", lambda c: {"code": c, "name": c, "price": 100.0, "change_pct": 9.5,
                                                       "market_status": "OPEN", "tradable": True}):
            with self.assertRaises(ledger.Rejected) as cm:   # 오늘 +9.5% 추격
                self.buy(code="000660")
            self.assertIn("추격", str(cm.exception))
        self.risk(entry_filters=[{"name": "급락 후 반등일", "conditions": [["ret_20d", "<", -15], ["change_today", ">", 5]]}])
        crash = bars("2026-01-01", [100] * 181 + [80] * 19)   # 20일 전 100 → 지금 84 (-16%), 오늘 +7.5%
        with mock.patch("phil.naver.daily_bars", lambda c, n=260: crash), \
                mock.patch("phil.naver.quote", lambda c: {"code": c, "name": c, "price": 84.0, "change_pct": 7.5,
                                                          "market_status": "OPEN", "tradable": True}):
            with self.assertRaises(ledger.Rejected) as cm:
                self.buy(code="000660")
            self.assertIn("급락 후 반등일", str(cm.exception))
        self.risk(entry_filters=[{"name": "x", "conditions": [["ret_20d", "<", -15]]}])
        self.buy(code="035420")                              # 조건에 안 걸리면 통과
        self.risk(max_same_sector="abc", position_size_pct_of_equity=5.0)   # 엉터리 값·느슨한 값은 무시
        (self.d / "strategy" / "risk.json").write_text("{broken", encoding="utf-8")
        e = self.buy(code="000660")                          # 깨진 파일이면 강제 안 함 (기록만)
        self.assertIn("읽을 수 없음", e["size_notes"][0])

    def test_limits(self):
        for kw in ({"horizon_days": 6}, {"stop_pct": 0.01}, {"stop_pct": 0.2},
                   {"edge_class": "nope"}, {"code": "000000"}):
            with self.assertRaises(ledger.Rejected):
                self.buy(**kw)

    def test_closed_market(self):
        with mock.patch("phil.market.trading_window", lambda now=None: (False, "주말")):
            with self.assertRaises(ledger.Rejected):
                self.buy()


class TestResolve(Tmp):
    def pos(self, **kw):
        e = {"id": "p1", "ts": "2026-10-13T01:00:00Z", "code": "005930", "name": "삼성전자",
             "qty": 100, "quote_price": 100.0, "entry_price": 100.05, "cost_krw": 10007,
             "horizon_days": 3, "stop_price": 96.0, "target_price": 106.0, "est_prob": 0.6,
             "base_rate": 0.5, "edge_class": "earnings", "status": "open"}
        e.update(kw)
        return e

    def run_settle(self, e, b, now=dt.datetime(2026, 10, 20, 9, 0, tzinfo=KST)):
        resolve.settle_position(e, b, now, {"commission": 0.00015, "sell_tax": 0.002, "slippage": 0.0005})
        return e

    def test_stop(self):
        b = bars("2026-10-13", [100, 98, 95], [(100, 100, 100), (99, 99, 97), (97, 97, 94)])
        e = self.run_settle(self.pos(), b)
        self.assertEqual((e["status"], e["exit_reason"], e["exit_date"]), ("closed", "손절", "2026-10-15"))
        self.assertLess(e["pnl_krw"], 0)

    def test_gap_down_fills_at_open(self):
        b = bars("2026-10-13", [100, 90], [(100, 100, 100), (92, 93, 89)])
        e = self.run_settle(self.pos(), b)
        self.assertIn("갭하락", e["exit_reason"])
        self.assertAlmostEqual(e["exit_price"], 92 * 0.9995, places=2)

    def test_target(self):
        b = bars("2026-10-13", [100, 105], [(100, 100, 100), (101, 107, 100)])
        e = self.run_settle(self.pos(), b)
        self.assertEqual(e["exit_reason"], "목표가 도달")

    def test_both_same_day_is_stop(self):
        b = bars("2026-10-13", [100, 100], [(100, 100, 100), (100, 107, 95)])
        self.assertEqual(self.run_settle(self.pos(), b)["exit_reason"], "손절")

    def test_expiry_and_calib(self):
        b = bars("2026-10-13", [100, 101, 102, 103])
        e = self.run_settle(self.pos(), b)
        self.assertEqual((e["exit_reason"], e["calib"]), ("만기 (종가)", "up"))

    def test_expiry_waits_for_close(self):
        b = bars("2026-10-13", [100, 101, 102, 103])
        e = self.run_settle(self.pos(), b, now=dt.datetime(2026, 10, 16, 14, 0, tzinfo=KST))
        self.assertEqual(e["status"], "open")
        self.assertNotIn("calib", e)

    def test_calib_after_stop(self):
        b = bars("2026-10-13", [100, 95, 101, 104], [(100, 100, 100), (97, 97, 95), (101, 101, 101), (104, 104, 104)])
        e = self.run_settle(self.pos(target_price=None), b)
        self.assertEqual((e["exit_reason"], e["calib"]), ("손절", "up"))

    def test_forecast(self):
        r = {"status": "open", "ref_date": "2026-10-13", "horizon_days": 2, "ref_price": 100}
        self.assertTrue(resolve.settle_forecast(r, bars("2026-10-13", [100, 99, 98]),
                                                dt.datetime(2026, 10, 20, tzinfo=KST)))
        self.assertEqual(r["status"], "down")


class TestPolicy(Tmp):
    def test_auto_ban(self):
        rows = [{"id": str(i), "status": "down", "est_prob": 0.8, "base_rate": 0.5,
                 "edge_class": "momentum"} for i in range(30)]
        config.dump_jsonl(self.d / "f.jsonl", rows)
        self.assertIn("momentum", policy.banned_edge_classes())
        self.assertIsNotNone(policy.check_edge_class("momentum"))
        self.assertIsNone(policy.check_edge_class("earnings"))

    def test_forecast_record(self):
        r = forecast.record("005930", 3, 0.58, "news-flow", "note")
        self.assertEqual(r["ref_price"], 100.0)
        self.assertAlmostEqual(r["base_rate"], 0.5, delta=0.01)
        with self.assertRaises(forecast.Rejected):
            forecast.record("005930", 3, 0.58, "news-flow")


class TestWatch(Tmp):
    def setUp(self):
        super().setUp()
        self.price = 100.0
        mock.patch("phil.naver.quote", lambda c: {"code": c, "name": "삼성전자", "price": self.price,
                                                  "market_status": "OPEN", "tradable": True}).start()
        mock.patch("phil.watch.trading_dates", lambda today: ["2026-10-13", "2026-10-14", "2026-10-15"]).start()
        self.e = ledger.buy("005930", 0.6, 2, 0.04, 0.06, "earnings", "실적 서프라이즈 테스트 근거")

    def status(self):
        return next(x for x in ledger.read_ledger() if x["id"] == self.e["id"])

    def test_stop_and_target(self):
        now = dt.datetime(2026, 10, 15, 10, 0, tzinfo=KST)
        self.price = 97.0
        watch.check_positions(now)
        self.assertEqual(self.status()["status"], "open")
        self.price = 95.9
        watch.check_positions(now)
        self.assertEqual(self.status()["exit_reason"], "손절 (실시간)")

    def test_expiry_at_1515(self):
        with mock.patch("phil.watch.kst_date", lambda ts: "2026-10-13"):
            watch.check_positions(dt.datetime(2026, 10, 15, 15, 0, tzinfo=KST))
            self.assertEqual(self.status()["status"], "open")
            watch.check_positions(dt.datetime(2026, 10, 15, 15, 16, tzinfo=KST))
        self.assertEqual(self.status()["exit_reason"], "만기 (15:15)")

    def test_kis_mirror_buy_then_sell(self):
        orders = []
        mock.patch("phil.kis.configured", lambda: True).start()
        mock.patch("phil.kis.order", lambda code, qty, side: orders.append((code, qty, side)) or "0001").start()
        mock.patch("phil.watch.operator_state", lambda: {"kis_enabled": True, "kis_enabled_at": "2000-01-01T00:00:00Z"}).start()
        today = market.kst_date(config.iso())
        now = dt.datetime.fromisoformat(today + "T10:00:00+09:00")
        watch.sync_kis(now)
        watch.sync_kis(now)  # 중복 매수 없음
        self.assertEqual(orders, [("005930", self.e["qty"], "buy")])
        ledger.close_position(self.e["id"], 101.0, "테스트 청산")
        watch.sync_kis(now)
        watch.sync_kis(now)
        self.assertEqual(orders[-1], ("005930", self.e["qty"], "sell"))
        self.assertEqual(len(orders), 2)

    def test_kis_failure_recorded_no_retry_buy(self):
        mock.patch("phil.kis.configured", lambda: True).start()
        calls = []

        def boom(*a):
            calls.append(a)
            raise kis.KisError("모의투자 잔고 부족")
        mock.patch("phil.kis.order", boom).start()
        mock.patch("phil.watch.operator_state", lambda: {"kis_enabled": True, "kis_enabled_at": "2000-01-01T00:00:00Z"}).start()
        now = dt.datetime.fromisoformat(market.kst_date(config.iso()) + "T10:00:00+09:00")
        watch.sync_kis(now)
        watch.sync_kis(now)
        self.assertEqual(len(calls), 1)
        self.assertEqual(watch.kis_rows()[self.e["id"]]["status"], "buy_failed")


class TestNotify(Tmp):
    def test_notifies_once_per_event(self):
        sent = []
        mock.patch("phil.notify.configured", lambda: True).start()
        mock.patch("phil.notify.send", lambda t: sent.append(t)).start()
        self.assertEqual(notify.scan_and_notify(), 0)          # 첫 실행은 현재 상태만 기억
        e = ledger.buy("005930", 0.62, 3, 0.04, 0.06, "earnings", "실적 서프라이즈 테스트 근거")
        notify.scan_and_notify()
        notify.scan_and_notify()                                # 중복 없음
        self.assertEqual(len(sent), 1)
        self.assertIn("매수", sent[0])
        ledger.close_position(e["id"], 103.0, "목표가 도달 (실시간)")
        notify.scan_and_notify()
        self.assertEqual(len(sent), 2)
        self.assertIn("청산", sent[1])

    def test_failed_send_retries_later(self):
        mock.patch("phil.notify.configured", lambda: True).start()
        notify.scan_and_notify()
        ledger.buy("005930", 0.62, 3, 0.04, 0.06, "earnings", "실적 서프라이즈 테스트 근거")

        def boom(t):
            raise notify.NotifyError("network")
        with mock.patch("phil.notify.send", boom):
            notify.scan_and_notify()
        sent = []
        with mock.patch("phil.notify.send", lambda t: sent.append(t)):
            notify.scan_and_notify()
        self.assertEqual(len(sent), 1)


COSTS = {"commission": 0.00015, "sell_tax": 0.002, "slippage": 0.0005}
SPEC = {"conditions": [["ret_1d", ">", 3]], "horizon": 2, "stop": 0.05, "target": 0.04, "max_per_day": 3,
        "rank_by": None}


class TestResearch(unittest.TestCase):
    def test_simulate_paths(self):
        b = bars("2026-01-01", [100, 100, 100], [(100, 100, 100), (100, 105, 99), (100, 100, 100)])
        r, why, held, up = research.simulate(b, 0, SPEC, COSTS)
        self.assertEqual((why, held), ("target", 1))
        self.assertAlmostEqual(r, (104 * 0.9995 * (1 - 0.00215) / (100 * 1.0005 * 1.00015) - 1) * 100, places=4)
        b = bars("2026-01-01", [100, 90, 90], [(100, 100, 100), (93, 94, 89), (90, 90, 90)])
        self.assertEqual(research.simulate(b, 0, SPEC, COSTS)[1], "stop_gap")
        self.assertIsNone(research.simulate(b[:2], 0, SPEC, COSTS))   # 아직 만기 안 됨

    def test_validate_spec(self):
        for bad in ({"conditions": [["x", ">", 1]], "horizon": 2, "stop": 0.05},
                    {"conditions": [["ret_1d", "==", 1]], "horizon": 2, "stop": 0.05},
                    {"conditions": [["ret_1d", ">", 1]], "horizon": 9, "stop": 0.05},
                    {"conditions": [["ret_1d", ">", 1]], "horizon": 2, "stop": 0.5}, "not json"):
            with self.assertRaises(research.SpecError):
                research.validate_spec(bad)

    def test_features_no_lookahead(self):
        b = bars("2025-01-01", [100 + i for i in range(80)])
        f1 = research.features(b, {}, False)
        f2 = research.features(b[:70], {}, False)
        self.assertEqual(f1[69], f2[69])   # 미래 봉을 붙여도 과거 지표는 그대로


class TestRules(Tmp):
    def fake_bt(self, ok):
        return {"period": {}, "train": {"n": 50, "avg_net_pct": 0.5}, "baseline_test": {"n": 100},
                "test": {"n": 40, "avg_net_pct": 0.6 if ok else -0.4, "up_rate": 0.6},
                "verdict": {"pass": ok, "reasons": [] if ok else ["검증 손실"]}}

    def test_lifecycle(self):
        with mock.patch("phil.research.backtest", lambda spec: self.fake_bt(False)):
            r = rules.propose("나쁜 규칙", SPEC, "백테스트에서 떨어질 가설")
        self.assertEqual(r["status"], "rejected")
        with mock.patch("phil.research.backtest", lambda spec: self.fake_bt(True)):
            r = rules.propose("좋은 규칙", SPEC, "급등 다음 날 추세 지속 가설")
        self.assertEqual(r["status"], "shadow")
        # 그림자 신호 15건이 모두 +1% 로 끝났다고 가정 → 실전 승격
        sigs = [{"id": str(i), "rule_id": r["id"], "rule": "x", "date": "2026-01-01", "code": "005930",
                 "name": "삼성전자", "price": 100, "mode": "shadow", "status": "done", "ret": 1.0, "up": True}
                for i in range(15)]
        config.dump_jsonl(self.d / "sig.jsonl", sigs)
        with mock.patch("phil.research.load_all", lambda: ({}, {})):
            ev = rules.evaluate()
        self.assertEqual(rules.get(r["id"])["status"], "live")
        self.assertTrue(any("실전 승격" in e for e in ev))
        # 최근 20건 평균이 음수가 되면 은퇴
        sigs += [dict(sigs[0], id=f"n{i}", ret=-2.0) for i in range(20)]
        config.dump_jsonl(self.d / "sig.jsonl", sigs)
        with mock.patch("phil.research.load_all", lambda: ({}, {})):
            rules.evaluate()
        self.assertEqual(rules.get(r["id"])["status"], "retired")

    def test_agent_cannot_use_rule_class(self):
        with self.assertRaises(ledger.Rejected):
            ledger.buy("005930", 0.7, 2, 0.04, None, "rule", "엔진 전용 클래스를 쓰려는 시도")

    def test_rule_buy_sizing(self):
        rule = {"id": "r1", "name": "테스트", "spec": research.validate_spec(SPEC),
                "backtest": {"test": {"n": 40, "avg_net_pct": 0.6, "up_rate": 0.6}}}
        e = ledger.rule_buy(rule, "005930", "삼성전자", 100.0, bars("2025-01-01", [100, 101] * 100))
        self.assertEqual((e["edge_class"], e["rule_id"], e["horizon_days"]), ("rule", "r1", 2))
        self.assertLessEqual(e["cost_krw"], 800_000)   # 자산의 8%


class TestSafety(Tmp):
    def test_drawdown_pause_and_resume(self):
        now = dt.datetime(2026, 11, 2, 10, 0, tzinfo=KST)   # 월요일
        self.assertIsNone(safety.check(10_000_000, now))
        self.assertIsNone(safety.check(9_200_000, now, park_total_pnl=-700_000))   # 지수 하락분 빼면 -1%
        self.assertIsNone(safety.check(9_600_000, now))      # -4%
        msg = safety.check(9_450_000, now)                   # -5.5%
        self.assertIn("중단", msg)
        self.assertTrue(safety.paused(now)[0])
        self.assertFalse(safety.paused(dt.datetime(2026, 11, 5, 10, 0, tzinfo=KST))[0])   # 2거래일 뒤 풀림
        with mock.patch("phil.safety.now_kst", lambda: now), self.assertRaises(ledger.Rejected):
            ledger.buy("005930", 0.7, 2, 0.04, None, "earnings", "안전장치 중 매수 시도 테스트")
        safety.resume()
        self.assertFalse(safety.paused(now)[0])


class TestDart(Tmp):
    def test_filter_universe_and_keywords(self):
        items = [{"stock_code": "005930", "report_nm": "단일판매ㆍ공급계약체결", "rcept_no": "1", "flr_nm": "삼성전자"},
                 {"stock_code": "005930", "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_no": "2"},
                 {"stock_code": "999999", "report_nm": "단일판매ㆍ공급계약체결", "rcept_no": "3"}]
        with mock.patch("phil.dart.fetch_today", lambda now=None: items):
            new = dart.poll()
            again = dart.poll()
        self.assertEqual([r["rcept_no"] for r in new], ["1"])
        self.assertEqual(again, [])

    def test_routine_reports_do_not_wake_cycle(self):
        self.assertTrue(dart.wanted("연결재무제표기준영업(잠정)실적(공정공시)"))
        self.assertTrue(dart.wanted("자기주식취득결정"))
        self.assertFalse(dart.wanted("증권발행실적보고서"))          # 정기 보고 — 주가 근거 없음
        self.assertFalse(dart.wanted("결산실적공시예고(안내공시)"))
        self.assertFalse(dart.wanted("기업설명회(IR)개최(안내공시)"))


class TestDartDocument(Tmp):
    def _resp(self, body):
        class R:
            def __init__(self, b): self.b = b
            def read(self): return self.b
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return lambda url, timeout=None: R(body)

    def test_reads_zip_and_strips_tags(self):
        import io as _io
        import zipfile as _zf
        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w") as z:
            z.writestr("20261105000123.xml", "<DOCUMENT><TITLE>단일판매ㆍ공급계약체결</TITLE><P>계약금액 1,234억원</P>"
                                               "<TD>최근 매출액 대비 15.2%</TD><STYLE>x{}</STYLE></DOCUMENT>".encode())
        with mock.patch("phil.dart.key", lambda: "k"), mock.patch("phil.dart.JOURNAL", self.d, create=True), \
                mock.patch("phil.config.JOURNAL", self.d), \
                mock.patch("urllib.request.urlopen", self._resp(buf.getvalue())):
            r = dart.document_text("20261105000123")
        self.assertIn("계약금액 1,234억원", r["text"])
        self.assertIn("15.2%", r["text"])
        self.assertNotIn("<", r["text"])
        self.assertNotIn("x{}", r["text"])

    def test_table_rows_stay_on_one_line(self):
        xml = ("<TABLE><TR><TD>구분</TD><TD>당기실적</TD><TD>전기실적</TD></TR>\r\n"
               "<TR><TE>영업이익</TE><TE>1,008</TE><TE>849</TE></TR></TABLE>").encode()
        text = dart._xml_to_text(xml)
        self.assertIn("구분 | 당기실적 | 전기실적", text)
        self.assertIn("영업이익 | 1,008 | 849", text)
        self.assertNotIn("\r", text)

    def test_cells_with_paragraphs(self):
        xml = ("<TABLE><TBODY><TR ACOPY='Y'>\r\n<TD><P>영업이익</P></TD><TD>\n<P>당해실적</P></TD>"
               "<TD ALIGN='RIGHT'><P>1,008</P></TD><TD><P>849</P></TD><TD><P>18.7</P></TD></TR>"
               "<TR><TD><P>-</P></TD><TD></TD></TR></TBODY></TABLE><P>※ 잠정치</P>").encode()
        text = dart._xml_to_text(xml)
        self.assertIn("영업이익 | 당해실적 | 1,008 | 849 | 18.7", text)
        self.assertIn("※ 잠정치", text)

    def test_error_message(self):
        body = "<result><status>014</status><message>파일이 존재하지 않습니다.</message></result>".encode()
        with mock.patch("phil.dart.key", lambda: "k"), mock.patch("phil.config.JOURNAL", self.d), \
                mock.patch("urllib.request.urlopen", self._resp(body)):
            with self.assertRaises(dart.DartError) as cm:
                dart.document_text("20261105000999")
        self.assertIn("파일이 존재하지 않습니다", str(cm.exception))
        with self.assertRaises(dart.DartError):
            dart.document_text("123")


class TestImprovements(Tmp):
    def test_forecast_without_view_rejected(self):
        with self.assertRaises(forecast.Rejected):
            forecast.record("005930", 3, 0.51, "other", "기저율 그대로")    # 기저율 ≈ 0.5025

    def test_dart_first_poll_does_not_trigger(self):
        calls = []
        watch.Jobs.dart_warm, watch.Jobs.last_dart = False, 0
        watch.Jobs.on_event_cycle = lambda new: calls.append(new)
        items = [{"stock_code": "005930", "report_nm": "단일판매ㆍ공급계약체결", "rcept_no": "a1"}]
        now = dt.datetime(2026, 11, 2, 10, 0, tzinfo=KST)
        with mock.patch("phil.dart.key", lambda: "k"), mock.patch("phil.dart.fetch_today", lambda now=None: items), \
                mock.patch("phil.watch._market_open", lambda now: True), mock.patch("phil.watch.Jobs.once"):
            watch.scheduled(now)                                   # 첫 확인: 기록만
            self.assertEqual(calls, [])
            items.append({"stock_code": "005930", "report_nm": "자기주식취득결정", "rcept_no": "a2"})
            watch.Jobs.last_dart = 0
            watch.scheduled(now)                                   # 그 뒤 새 공시: 사이클
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["rcept_no"], "a2")
        watch.Jobs.on_event_cycle = None

    def test_discovery_strict_filter(self):
        from phil import discover
        cfg = {"min_test_trades": 40, "min_test_avg_net_pct": 0.3, "min_test_profit_factor": 1.3,
               "min_train_avg_net_pct": 0.1, "min_excess_over_baseline_pct": 0.3}
        ok = {"verdict": {"pass": True}, "train": {"avg_net_pct": 0.5},
              "test": {"n": 50, "avg_net_pct": 0.8, "profit_factor": 1.4}, "baseline_test": {"avg_net_pct": 0.2}}
        self.assertTrue(discover.strict_pass(ok, cfg))
        near_base = dict(ok, baseline_test={"avg_net_pct": 0.6})
        self.assertFalse(discover.strict_pass(near_base, cfg))   # 기준선과 0.3%p 미만 차이
        self.assertGreater(len(list(discover.candidates())), 300)


class TestPark(Tmp):
    def setUp(self):
        super().setUp()
        self.etf = 100.0
        mock.patch("phil.park._price", lambda: self.etf).start()
        mock.patch("phil.park.enabled", lambda: True).start()

    def test_park_idle_cash_and_fund_buys(self):
        now = dt.datetime(2026, 11, 2, 10, 0, tzinfo=KST)
        r = park.rebalance(ledger.cash([]), ledger.equity([]), now)
        self.assertAlmostEqual(r["qty"], 25_000, delta=100)      # 한 번에 자산의 25% 까지만 (분할 투입)
        self.assertGreater(ledger.cash([]), 7_000_000)
        eq0 = ledger.equity([])
        self.assertAlmostEqual(eq0, 10_000_000, delta=20_000)    # 비용만큼만 줄어듦
        self.assertIsNone(park.rebalance(ledger.cash([]), eq0, now))   # 한 시간 안엔 다시 안 함
        later = now + dt.timedelta(hours=1, minutes=1)
        with mock.patch("phil.park._minutes_since", lambda ts: 61):
            for _ in range(3):
                park.rebalance(ledger.cash([]), ledger.equity([]), later)
        self.assertLess(ledger.cash([]), 600_000)                # 네 번에 걸쳐 95% 까지
        config.save_json(park.PARK, {"qty": 98_000, "cost_krw": 9_800_000, "net_out": 9_800_000,
                                     "last_price": 100.0, "trades": []})
        e = ledger.buy("005930", 0.62, 3, 0.04, None, "earnings", "대기 자금에서 꺼내 쓰는 매수 테스트")
        self.assertGreater(e["cost_krw"], 400_000)               # 현금 20만뿐이어도 ETF 를 팔아 매수
        self.assertGreaterEqual(ledger.cash(ledger.read_ledger()), 0)
        self.etf = 110.0                                         # 시장 +10% → 대기 자금도 오름
        park.mark(110.0)                                         # (감시 루프가 1분마다 시세 반영)
        self.assertGreater(ledger.equity(ledger.read_ledger()), eq0 + 500_000)

    def test_gate_blocks_in_bear_market_and_crash_day(self):
        now = dt.datetime(2026, 11, 2, 10, 0, tzinfo=KST)
        bear = {"date": "2026-11-02", "label": "하락장", "kospi_vs_ma60": -8.0}
        with mock.patch("phil.market.regime_now", lambda max_age_sec=1800: bear):
            self.assertIn("하락장", park.gate(now))
            self.assertIsNone(park.rebalance(ledger.cash([]), ledger.equity([]), now))   # 안 넣음
        self.assertEqual(park.state()["qty"], 0)
        flat = {"date": "2026-11-02", "label": "횡보", "kospi_vs_ma60": 0.5}
        with mock.patch("phil.market.regime_now", lambda max_age_sec=1800: flat), \
                mock.patch("phil.naver.index_quote", lambda s="KOSPI": {"price": 5000.0, "change_pct": -2.4,
                                                                        "market_status": "OPEN"}):
            self.assertIn("급락일", park.gate(now))
            self.assertIsNone(park.rebalance(ledger.cash([]), ledger.equity([]), now))
        with mock.patch("phil.market.regime_now", lambda max_age_sec=1800: flat):
            self.assertIsNone(park.gate(now))
            self.assertIsNotNone(park.rebalance(ledger.cash([]), ledger.equity([]), now))
        with mock.patch("phil.market.regime_now", lambda max_age_sec=1800: None):
            self.assertIn("판정 불가", park.gate(now))              # 시세 실패 = 넣지 않음
        with mock.patch("phil.safety.paused", lambda now=None: (True, "테스트")):
            self.assertIn("안전장치", park.gate(now))

    def test_bear_market_reduces_park_to_cap(self):
        now = dt.datetime(2026, 11, 3, 10, 0, tzinfo=KST)
        config.save_json(park.PARK, {"qty": 76_000, "cost_krw": 7_600_000, "net_out": 7_600_000,
                                     "last_price": 100.0, "trades": []})                     # 자산의 76%
        bear = {"date": "2026-11-03", "label": "하락장", "kospi_vs_ma60": -8.0}
        with mock.patch("phil.market.regime_now", lambda max_age_sec=1800: bear):
            r = park.rebalance(ledger.cash([]), ledger.equity([]), now)
            self.assertEqual(r["side"], "sell")
            self.assertAlmostEqual(park.state()["qty"], 30_000, delta=100)                   # 30% 까지로
            self.assertIsNone(park.rebalance(ledger.cash([]), ledger.equity([]), now + dt.timedelta(hours=2)))  # 하루 한 번
        self.assertGreater(ledger.cash([]), 6_000_000)                                       # 나머지는 현금


class TestParkHalt(Tmp):
    """KODEX 200 이 잠깐 '거래 불가'로 떠도 감시 루프·매수가 터지지 않아야 한다."""
    def setUp(self):
        super().setUp()
        mock.patch("phil.park.enabled", lambda: True).start()
        mock.patch("phil.naver.quote", lambda c, allow_etf=False: {
            "code": c, "name": "x", "price": 100.0, "market_status": "OPEN",
            "tradable": c != "069500", "status": "ok/TRADING" if c != "069500" else "ok/HALT"}).start()
        watch.PARK_HOLD.update(until=0.0, last_event=0.0)

    def test_manage_park_survives_and_backs_off(self):
        now = dt.datetime(2026, 11, 6, 10, 0, tzinfo=KST)
        calls = []
        real = park.rebalance
        mock.patch("phil.park.rebalance", lambda *a: calls.append(1) or real(*a)).start()
        watch.manage_park(now, {})                              # 예외 없이 끝남
        watch.manage_park(now, {})                              # 10분 동안은 다시 시도 안 함
        self.assertEqual(len(calls), 1)
        evs = [e for e in config.load_jsonl(watch.WATCH_EVENTS) if e["kind"] == "park"]
        self.assertEqual(len(evs), 1)
        self.assertIn("ok/HALT", evs[0]["msg"])

    def test_buy_needing_park_cash_is_rejected_cleanly(self):
        config.save_json(park.PARK, {"qty": 98_000, "cost_krw": 9_800_000, "net_out": 9_800_000,
                                     "last_price": 100.0, "trades": []})
        with self.assertRaises(ledger.Rejected) as cm:
            ledger.buy("005930", 0.62, 3, 0.04, None, "earnings", "대기 자금이 묶였을 때 매수 테스트")
        self.assertIn("대기 자금", str(cm.exception))

    def test_tick_keeps_other_steps_running(self):
        done = []
        mock.patch("phil.watch._market_open", lambda now: True).start()
        mock.patch("phil.watch.scheduled", lambda now: None).start()
        mock.patch("phil.watch.check_positions", lambda now: {}).start()
        mock.patch("phil.watch.manage_park", lambda now, p: (_ for _ in ()).throw(ValueError("boom"))).start()
        mock.patch("phil.watch.snapshot_equity", lambda now, p: done.append("snap")).start()
        mock.patch("phil.watch.sync_kis", lambda now: done.append("kis")).start()
        mock.patch("phil.notify.scan_and_notify", lambda: None).start()
        with self.assertRaises(RuntimeError):                   # 진짜 오류는 여전히 알림 대상
            watch.tick()
        self.assertEqual(done, ["snap", "kis"])                 # 그래도 나머지 단계는 돌았음


class TestUpgrades(Tmp):
    def test_claude_cost_after_cost_return(self):
        config.dump_jsonl(self.d / "cyc.jsonl", [{"finished": config.iso(), "cost_usd": 1.0},
                                                 {"finished": "2020-01-01T00:00:00Z", "cost_usd": 2.0}])
        c = score.claude_cost({"usd_krw": 1000})
        self.assertEqual((c["total_usd"], c["today_usd"], c["total_krw"]), (3.0, 1.0, 3000))
        r = score.report()
        self.assertLess(r["return_after_cost_pct"], r["return_pct"])

    def test_regime_guard(self):
        from phil import discover
        cfg = {"min_test_trades": 1, "min_test_avg_net_pct": 0, "min_test_profit_factor": 0,
               "min_train_avg_net_pct": -9, "min_excess_over_baseline_pct": -9}
        base = {"name": "x", "spec": {"conditions": [["ret_1d", ">", 3]], "horizon": 3, "stop": 0.05,
                                      "target": None, "max_per_day": 3, "rank_by": None}}
        ok = dict(base, by_regime={"하락장": {"n": 30, "avg_net_pct": 0.5}})
        self.assertEqual(discover.regime_guard(ok, None, cfg)["spec"], base["spec"])   # 하락장도 플러스 → 그대로
        bad = dict(base, by_regime={"하락장": {"n": 30, "avg_net_pct": -0.5}})
        gated_bt = {"spec": {**base["spec"], "conditions": base["spec"]["conditions"] + [["kospi_vs_ma60", ">", 0]]},
                    "train": {"avg_net_pct": 1}, "test": {"n": 50, "avg_net_pct": 1, "profit_factor": 2},
                    "baseline_test": {"avg_net_pct": 0}, "by_regime": {}, "verdict": {"pass": True}}
        with mock.patch("phil.research.backtest", lambda spec, prepared=None: gated_bt):
            g = discover.regime_guard(bad, None, cfg)
        self.assertIn(["kospi_vs_ma60", ">", 0], g["spec"]["conditions"])
        self.assertIn("상승장만", g["name"])

    def test_cycle_failure_alerts(self):
        from phil import cycle
        sent = []
        cycle.FAILS["in_row"] = 0
        with mock.patch("phil.notify.alert", lambda t: sent.append(t)):
            cycle._alert_on_failure({"ok": False, "error": "시세 접속 실패"})
            self.assertEqual(sent, [])                                    # 1번째는 조용히
            cycle._alert_on_failure({"ok": False, "error": "시세 접속 실패"})
            self.assertEqual(len(sent), 1)                                # 2번 연속이면 알림
            cycle._alert_on_failure({"ok": True})
            cycle._alert_on_failure({"ok": False, "error_kind": "auth", "error": "로그인 만료"})
            self.assertEqual(len(sent), 2)                                # 로그인 만료는 바로


class TestTuning(Tmp):
    def days(self, equities, cost_per_day=0.0):
        rows = [{"date": f"2026-10-{i + 1:02d}", "equity": e, "cost_usd_total": i * cost_per_day, "kospi": 100}
                for i, e in enumerate(equities)]
        config.dump_jsonl(self.d / "daily.jsonl", rows)

    def test_defense_levels_and_overlay(self):
        base = config.load_config()["limits"]["max_position_pct"]
        self.days([10_000_000] + [9_800_000] * 10)               # 10일 -2%
        self.assertTrue(tuning.check_defense())
        cfg = config.load_config()
        self.assertAlmostEqual(cfg["limits"]["max_position_pct"], base * 0.5)
        self.assertEqual(cfg["policy"]["exploration"]["max_per_day"], 1)
        self.days([9_800_000] + [9_900_000] * 10)                # 회복 +1%
        tuning.check_defense()
        self.assertAlmostEqual(config.load_config()["limits"]["max_position_pct"], base)

    def test_window_uses_available_days_early_on(self):
        self.days([10_000_000, 9_950_000, 9_900_000])              # 기록 3일뿐 (2거래일 구간)
        self.assertIsNone(tuning.window(10))                       # 최소 3거래일 구간은 있어야
        self.days([10_000_000, 9_950_000, 9_900_000, 9_850_000])
        w = tuning.window(10)
        self.assertTrue(w["partial"])
        self.assertEqual(w["days"], 3)
        self.assertAlmostEqual(w["after_cost_pct"], -1.5, places=1)
        self.assertTrue(tuning.check_defense())                    # 시작 직후에도 방어 모드가 켜질 수 있다

    def test_window_excludes_park_etf_swings(self):
        rows = [{"date": f"2026-10-{i + 1:02d}", "equity": e, "cost_usd_total": 0, "kospi": 100, "park_total_pnl": p}
                for i, (e, p) in enumerate([(10_000_000, 0)] + [(9_500_000, -520_000)] * 10)]   # 지수만 빠짐, 종목은 +2만
        config.dump_jsonl(self.d / "daily.jsonl", rows)
        w = tuning.window(10)
        self.assertTrue(w["ex_park"])
        self.assertGreater(w["after_cost_pct"], 0)
        self.assertEqual(tuning.check_defense(), [])               # 방어 모드 안 켜짐

    def test_cost_saver(self):
        config.save_operator_state({"loop_minutes": 120})
        self.days([10_000_000] * 11, cost_per_day=1.0)           # 수익 0, 비용 있음
        self.assertTrue(tuning.check_cost())
        self.assertEqual(config.operator_state()["loop_minutes"], 240)
        self.days([10_000_000] + [10_200_000] * 10, cost_per_day=1.0)   # 20만원 > 비용 1.45만원 x 2
        tuning.check_cost()
        self.assertEqual(config.operator_state()["loop_minutes"], 120)

    def test_propose_bounds_and_trial(self):
        with self.assertRaises(tuning.TuneError):
            tuning.propose("safety.max_drawdown_pct", 50, "안전장치는 조정 대상이 아니라는 테스트입니다")
        with self.assertRaises(tuning.TuneError):
            tuning.propose("limits.horizon_days_max", 30, "범위를 벗어나는 값으로 제안하는 테스트입니다")
        self.days([10_000_000] + [10_100_000] * 10)               # 시험 전 10일 +1%
        tuning.propose("limits.horizon_days_max", 7, "백테스트에서 5일 이상 보유 규칙이 꾸준히 좋았음 (테스트)")
        self.assertEqual(config.load_config()["limits"]["horizon_days_max"], 7)
        with self.assertRaises(tuning.TuneError):                 # 한 번에 하나
            tuning.propose("policy.exploration.max_per_day", 2, "두 번째 제안은 시험 중이라 거부되어야 합니다")
        rows = config.load_jsonl(self.d / "daily.jsonl")
        rows += [{"date": f"2026-11-{i + 1:02d}", "equity": 10_100_000 - (i + 1) * 30_000, "cost_usd_total": 0,
                  "kospi": 100} for i in range(10)]                # 시험 10일 -3%
        config.dump_jsonl(self.d / "daily.jsonl", rows)
        msgs = tuning.evaluate_trial()
        self.assertIn("되돌림", msgs[0])
        self.assertEqual(config.load_config()["limits"]["horizon_days_max"], 5)


class TestGuard(unittest.TestCase):
    def test_restore(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "phil").mkdir()
        f = d / "phil" / "x.py"
        f.write_text("orig")
        with mock.patch.object(guard, "ROOT", d), mock.patch.object(guard, "PROTECTED_PATHS", ["phil"]):
            snap = guard.snapshot()
            f.write_text("hacked")
            (d / "phil" / "new.py").write_text("x")
            changed = guard.restore(snap)
        self.assertEqual(f.read_text(), "orig")
        self.assertFalse((d / "phil" / "new.py").exists())
        self.assertEqual(len(changed), 2)


if __name__ == "__main__":
    unittest.main()
