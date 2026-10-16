"""엔진 단위 테스트 (네트워크 없음): python -m unittest discover tests"""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from phil import config, forecast, guard, ledger, market, policy, resolve, score  # noqa: E402

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
                             (resolve, "FORECASTS", "f.jsonl"), (score, "META", "m.json")]:
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
        with self.assertRaises(ledger.Rejected):
            self.buy(est=0.55)

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
