"""엔진 단위 테스트 (네트워크·실제 자금 없음): python -m unittest discover tests"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from phil import config, guard, ledger, real, resolve, score  # noqa: E402


def bet(i, status, est, entry, edge_class="cross-market", stake=5.0, market=None):
    shares = round(stake / entry, 4)
    r = {"id": f"b{i}", "ts": "2026-10-01T00:00:00Z", "market_id": market or f"m{i}",
         "question": f"Q{i}", "end_date": "2099-01-01T00:00:00Z", "outcome": "Yes",
         "token_id": f"t{i}", "entry_price": entry, "est_prob": est, "edge": est - entry,
         "stake_usd": stake, "shares": shares, "category": "c", "edge_class": edge_class,
         "rationale": "test rationale", "status": status, "settled_ts": "2026-10-02T00:00:00Z"}
    if status == "won":
        r["pnl_usd"] = round(shares - stake, 4)
    elif status == "lost":
        r["pnl_usd"] = -stake
    return r


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(m, n, self.d / f) for m, n, f in [
            (config, "LEDGER", "ledger.jsonl"), (ledger, "LEDGER", "ledger.jsonl"),
            (resolve, "LEDGER", "ledger.jsonl"), (score, "LEDGER", "ledger.jsonl"),
            (real, "LEDGER", "ledger.jsonl"), (score, "REAL_LEDGER", "real.jsonl"),
            (real, "REAL_LEDGER", "real.jsonl"), (config, "OPERATOR", "op.json")]]
        for p in self.patches:
            p.start()
        mock.patch("phil.real.operator_state", lambda: {"real_enabled": True}).start()

    def tearDown(self):
        mock.patch.stopall()

    def write(self, rows):
        config.dump_jsonl(self.d / "ledger.jsonl", rows)


class TestResolve(Tmp):
    def test_settle_won_lost_void(self):
        now = config.now_utc()
        e = bet(1, "open", 0.6, 0.5)
        m = {"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'}
        self.assertTrue(resolve.settle_row(e, m, now))
        self.assertEqual(e["status"], "won")
        self.assertAlmostEqual(e["pnl_usd"], 5.0)
        e2 = bet(2, "open", 0.6, 0.5)
        m2 = {"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["0","1"]'}
        resolve.settle_row(e2, m2, now)
        self.assertEqual((e2["status"], e2["pnl_usd"]), ("lost", -5.0))
        e3 = bet(3, "open", 0.6, 0.5)
        e3["end_date"] = "2020-01-01T00:00:00Z"
        m3 = {"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}
        resolve.settle_row(e3, m3, now)
        self.assertEqual(e3["status"], "void")
        self.assertFalse(resolve.settle_row(bet(4, "open", .6, .5), {"closed": False}, now))

    def test_cash(self):
        self.write([bet(1, "won", .6, .5), bet(2, "lost", .6, .5), bet(3, "open", .6, .5)])
        # 1000 -5 +10 -5 -5
        self.assertAlmostEqual(ledger.cash(ledger.read_ledger()), 995.0)


class TestScore(Tmp):
    def test_qualification(self):
        rows = [bet(i, "won" if i < 7 else "lost", 0.7, 0.5) for i in range(10)]
        rows += [bet(100 + i, "lost", 0.7, 0.5, edge_class="other") for i in range(10)]
        self.write(rows)
        self.assertEqual(score.qualified_edge_classes(), ["cross-market"])
        self.write(rows[:9])  # 표본 부족
        self.assertEqual(score.qualified_edge_classes(), [])


class TestReal(Tmp):
    def setUp(self):
        super().setUp()
        rows = [bet(i, "won" if i < 7 else "lost", 0.7, 0.5) for i in range(10)]
        self.paper = bet(50, "open", 0.7, 0.5, market="mX")
        self.write(rows + [self.paper])
        mock.patch("phil.real.pmapi.best_prices", lambda t: (0.49, 0.51, 100)).start()
        self.fake = mock.MagicMock()
        self.fake.post_order.return_value = {"success": True, "status": "matched",
                                             "orderID": "o1", "makingAmount": "1",
                                             "takingAmount": "1.96"}
        mock.patch("phil.real.client", lambda: self.fake).start()
        mod = types.ModuleType("py_clob_client_v2")
        mod.MarketOrderArgs = lambda **k: k
        mod.OrderType = types.SimpleNamespace(FOK="FOK")
        mock.patch.dict(sys.modules, {"py_clob_client_v2": mod}).start()

    def test_place_and_caps(self):
        r = real.place_twin(self.paper)
        self.assertEqual(r["status"], "filled")
        self.assertEqual(r["usd"], 1.0)
        kwargs = self.fake.create_market_order.call_args[0][0]
        self.assertEqual(kwargs["amount"], 1.0)
        self.assertLessEqual(kwargs["price"], 0.52)
        with self.assertRaises(real.RealError):  # 같은 마켓 중복
            real.place_twin(self.paper)

    def test_unqualified_class_blocked(self):
        p = dict(self.paper, edge_class="other", market_id="mY")
        with self.assertRaises(real.RealError):
            real.place_twin(p)

    def test_slippage_blocked(self):
        mock.patch("phil.real.pmapi.best_prices", lambda t: (0.6, 0.6, 100)).start()
        with self.assertRaises(real.RealError):
            real.place_twin(self.paper)

    def test_unknown_blocks_next(self):
        self.fake.post_order.side_effect = TimeoutError("net")
        r = real.place_twin(self.paper)
        self.assertEqual(r["status"], "unknown")
        with self.assertRaises(real.RealError):
            real.place_twin(dict(self.paper, market_id="mZ", id="b51"))

    def test_daily_cap(self):
        with mock.patch("phil.real.caps", lambda: {"max_stake": 1.0, "daily_cap": 2.0,
                                                    "max_open": 10, "max_slippage": .02}):
            real.place_twin(dict(self.paper, market_id="a"))
            real.place_twin(dict(self.paper, market_id="b"))
            with self.assertRaises(real.RealError):
                real.place_twin(dict(self.paper, market_id="c"))

    def test_hard_ceiling(self):
        with mock.patch("phil.real.load_config", lambda: {"real": {
                "max_stake_usd": 999, "daily_stake_cap_usd": 999, "max_open_positions": 999}}):
            c = real.caps()
        self.assertEqual((c["max_stake"], c["daily_cap"], c["max_open"]),
                         (real.HARD_MAX_STAKE, real.HARD_DAILY_CAP, real.HARD_MAX_OPEN))


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
