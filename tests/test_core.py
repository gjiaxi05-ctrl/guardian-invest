"""
离线单元测试：不联网、不调 AI、不碰券商，只验证"代码守底线"的几条规则真的成立。
运行：python -m unittest discover -s tests -v
"""
import json
import math
import os
import random
import sys
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ai_review  # noqa: E402
import config_guard  # noqa: E402
import run  # noqa: E402
import shadow  # noqa: E402
import strategies_daily as sd  # noqa: E402


def base_config():
    """公开的 config.json（不读本地私有覆盖）。"""
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def synthetic_history(days=900, seed=7):
    """造一段随机行情：有牛市、有急跌，够算 10 个月均线和 20 日波动率。"""
    rnd = random.Random(seed)
    d, dates = date(2015, 1, 1), []
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    rows = {}
    for s in sd.SIGNAL_SYMBOLS:
        px, out = 100.0, []
        for k, day in enumerate(dates):
            shock = -0.04 if 500 <= k < 520 else 0.0  # 中间放一段急跌
            px *= math.exp(0.0004 + shock + rnd.gauss(0, 0.012))
            out.append((day, px))
        rows[s] = out
    return sd.H(rows)


class AiCanOnlyReduce(unittest.TestCase):
    def test_caps_never_increase(self):
        proposal = {"SPY": 0.4, "QQQ": 0.3, "BIL": 0.3}
        caps = [{"symbol": "SPY", "max_weight": 0.9},   # 高于原值：忽略
                {"symbol": "QQQ", "max_weight": 0.1},   # 削减：多出来的进 BIL
                {"symbol": "TQQQ", "max_weight": 0.5},  # 不在方案里：忽略
                {"symbol": "BIL", "max_weight": 0.0}]   # 现金不能被削
        out, applied = ai_review.apply_caps(proposal, caps, "BIL")
        self.assertAlmostEqual(out["SPY"], 0.4)
        self.assertAlmostEqual(out["QQQ"], 0.1)
        self.assertAlmostEqual(out["BIL"], 0.5)
        self.assertAlmostEqual(sum(out.values()), 1.0)
        self.assertEqual([a["symbol"] for a in applied], ["QQQ"])
        for s in proposal:
            if s != "BIL":
                self.assertLessEqual(out.get(s, 0), proposal[s])

    def test_garbage_caps_ignored(self):
        out, applied = ai_review.apply_caps({"SPY": 0.6, "BIL": 0.4},
                                            [{"symbol": "SPY"}, {"max_weight": 0}, "x", {"symbol": "SPY", "max_weight": "abc"}],
                                            "BIL")
        self.assertEqual(out, {"SPY": 0.6, "BIL": 0.4})
        self.assertEqual(applied, [])


class ConfigGuard(unittest.TestCase):
    def test_out_of_range_numbers_are_clamped(self):
        cfg = base_config()
        cfg["guardrails"]["max_order_pct"] = 5
        cfg["guardrails"]["drawdown_breaker"] = "big"
        out, notes = config_guard.check(cfg)
        self.assertEqual(out["guardrails"]["max_order_pct"], 0.60)
        self.assertEqual(out["guardrails"]["drawdown_breaker"], 0.35)
        self.assertEqual(len(notes), 2)

    def test_whitelist_rejects_single_stocks_and_3x(self):
        cfg = base_config()
        cfg["whitelist"] += ["TQQQ", "NVDA", "SQQQ"]
        out, _ = config_guard.check(cfg)
        for s in ("TQQQ", "NVDA", "SQQQ"):
            self.assertNotIn(s, out["whitelist"])

    def test_bad_enum_falls_back_to_safer_value(self):
        cfg = base_config()
        cfg["execution"] = "yolo"
        cfg["risk_level"] = "ultra"
        out, _ = config_guard.check(cfg)
        self.assertEqual(out["execution"], "approve")
        self.assertEqual(out["risk_level"], "conservative")
        self.assertEqual(out["risk_scale"], sd.RISK_LEVELS["conservative"]["scale"])

    def test_live_mode_needs_confirmation(self):
        cfg = base_config()
        cfg["mode"] = "ibkr_live"
        cfg["ibkr"]["dry_run"] = False
        out, notes = config_guard.check(cfg)
        self.assertTrue(out["ibkr"]["dry_run"])
        self.assertTrue(any("live_confirmation" in n for n in notes))

    def test_profile_weights_above_one_are_scaled(self):
        cfg = base_config()
        cfg["profiles"]["growth"] = {"trend_us_equity": 1.0, "lev_trend_us": 1.0}
        out, _ = config_guard.check(cfg)
        self.assertAlmostEqual(sum(out["profiles"]["growth"].values()), 1.0)


class HardGuardrails(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = config_guard.check(base_config())
        self.prices = {"SPY": 500.0, "SSO": 90.0, "BIL": 91.0, "TQQQ": 80.0}

    def test_non_whitelisted_symbol_blocked(self):
        kept, blocked = run.guard([{"symbol": "TQQQ", "qty": 10, "value": 800.0}], self.cfg, 10000, self.prices)
        self.assertEqual(kept, [])
        self.assertEqual(blocked[0]["why"], "不在白名单")

    def test_single_order_capped(self):
        kept, _ = run.guard([{"symbol": "SSO", "qty": 100, "value": 9000.0}], self.cfg, 10000, self.prices)
        self.assertLessEqual(kept[0]["value"], self.cfg["guardrails"]["max_order_pct"] * 10000)

    def test_sells_never_blocked_by_turnover(self):
        orders = [{"symbol": "SPY", "qty": -20, "value": -10000.0},
                  {"symbol": "SSO", "qty": 30, "value": 2700.0},
                  {"symbol": "BIL", "qty": 60, "value": 5460.0}]
        cfg = json.loads(json.dumps(self.cfg))
        cfg["guardrails"]["max_daily_turnover_pct"] = 0.2
        kept, blocked = run.guard(orders, cfg, 10000, self.prices)
        self.assertIn("SPY", [o["symbol"] for o in kept])
        self.assertIn("BIL", [o["symbol"] for o in kept])
        self.assertEqual([o["symbol"] for o in blocked], ["SSO"])


class RiskLevels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = synthetic_history()

    def test_weights_valid_every_day(self):
        for key, lv in sd.RISK_LEVELS.items():
            f = sd.level_strategy(key)
            for i in range(250, len(self.h.dates), 7):
                w = f(self.h, i)
                self.assertTrue(all(x >= -1e-12 for x in w.values()), key)
                self.assertAlmostEqual(sum(w.values()), 1.0, places=9, msg=key)
                if lv["two"] == 0 and lv["cap"] <= 1:
                    self.assertFalse({"SSO", "QLD"} & {s for s, x in w.items() if x > 1e-12}, key)

    def test_higher_level_means_more_exposure(self):
        def exposure(w):
            return w.get("SPY", 0) + w.get("QQQ", 0) + 2 * (w.get("SSO", 0) + w.get("QLD", 0))
        avg = {}
        for key in sd.RISK_LEVELS:
            f = sd.level_strategy(key)
            xs = [exposure(f(self.h, i)) for i in range(250, len(self.h.dates))]
            avg[key] = sum(xs) / len(xs)
        order = list(sd.RISK_LEVELS)
        for a, b in zip(order, order[1:]):
            self.assertLess(avg[a], avg[b], f"{a} 应该比 {b} 更保守")

    def test_no_lookahead(self):
        """把今天之后的行情全改掉，今天的目标不能变。"""
        f = sd.level_strategy("growth")
        i = 600
        before = f(self.h, i)
        for s in self.h.c:
            for k in range(i + 1, len(self.h.dates)):
                self.h.c[s][k] *= 3
        self.h._cum.clear()
        after = sd.level_strategy("growth")(self.h, i)
        self.assertEqual(before, after)


class ShadowSanitize(unittest.TestCase):
    def test_shadow_weights_forced_into_limits(self):
        cfg, _ = config_guard.check(base_config())
        out = shadow.sanitize({"SPY": 0.9, "TQQQ": 0.5, "QQQ": "x", "GLD": -0.2, "IEF": 0.4}, cfg)
        self.assertNotIn("TQQQ", out)
        self.assertNotIn("GLD", out)
        self.assertLessEqual(out["SPY"], cfg["shadow"]["max_single_weight"] + 1e-9)
        self.assertAlmostEqual(sum(out.values()), 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
