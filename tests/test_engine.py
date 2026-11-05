"""엔진 단위 테스트 (네트워크 없음): python -m unittest discover tests"""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from phil import (config, dart, forecast, guard, kis, ledger, market, notify, policy, research,  # noqa: E402
                  resolve, rules, safety, score, watch)

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
                             (safety, "META", "m.json"), (dart, "DISCLOSURES", "d.jsonl")]:
            mock.patch.object(mod, name, self.d / f).start()
        mock.patch("phil.market.universe", lambda refresh=False: {
            "005930": {"name": "삼성전자", "market": "KOSPI", "price": 100}}).start()
        mock.patch("phil.market.trading_window", lambda now=None: (True, "장중")).start()
        mock.patch("phil.naver.quote", lambda c: {"code": c, "name": "삼성전자", "price": 100.0,
                                                  "market_status": "OPEN", "tradable": True}).start()
        # 기저율 0.5 가 나오도록 오르내림 반복
        mock.patch("phil.naver.daily_bars", lambda c, n=260: bars("2026-01-01", [100, 101] * 100)).start()

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
        self.assertLessEqual(e["cost_krw"], 1_000_000)   # 자산의 10%
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
        self.assertLessEqual(e["cost_krw"], 200_000)   # 자산의 2%
        codes = ["000660", "035420", "051910"]
        uni = {c: {"name": c, "market": "KOSPI", "price": 100} for c in ["005930"] + codes}
        with mock.patch("phil.market.universe", lambda refresh=False: uni):
            self.buy(est=0.54, code="000660")
            self.buy(est=0.54, code="035420")
            with self.assertRaises(ledger.Rejected):   # 하루 3건 한도
                self.buy(est=0.54, code="051910")
            big = self.buy(est=0.62, code="051910")    # 정규 매수는 한도와 무관
        self.assertFalse(big["exploration"])
        self.assertGreater(big["cost_krw"], 500_000)

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
        self.assertLessEqual(e["cost_krw"], 300_000)   # 자산의 3%


class TestSafety(Tmp):
    def test_drawdown_pause_and_resume(self):
        now = dt.datetime(2026, 11, 2, 10, 0, tzinfo=KST)   # 월요일
        self.assertIsNone(safety.check(10_000_000, now))
        self.assertIsNone(safety.check(9_600_000, now))      # -4%
        msg = safety.check(9_480_000, now)                   # -5.2%
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
