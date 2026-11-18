"""
配置守门：每次运行前检查 config.json，手改配置也改不出离谱的值。
- 数值参数压回允许范围；类型不对的换成默认值
- 选项类参数只能取合法值，非法值换成更安全的那个
- 白名单只能是下面 UNIVERSE 里的 ETF（不允许个股、三倍、反向）
- 实盘模式必须写确认口令，否则强制只读
- risk_level（风险档位）展开成具体的策略参数
修正只作用于本次运行（不改写文件），每次都写进日报，直到配置被改正。

个人设置（账户号、自己选的档位等）写在同目录的 config.local.json 里，运行时覆盖 config.json；
这个文件不进 git，公开的 config.json 里不放任何个人信息。
"""
import json
import os

UNIVERSE = {
    "SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD",       # 股票、地产、商品、黄金指数 ETF
    "IEF", "TLT", "TIP", "LQD", "BIL", "AGG", "SHY",              # 债券与短债
    "SSO", "QLD",                                                   # 仅允许的两倍 ETF（标普、纳指）
    "SPYM", "QQQM",                                                 # 实盘用的低单价替代品
}
CASH = "BIL"
LIVE_PHRASE = "我已了解风险，确认使用真实资金"

# 路径: (下限, 上限, 默认值, 是否整数)
NUM = {
    "paper_capital_usd": (100, 1_000_000, 4200, False),
    "guardrails.rebalance_band": (0.01, 0.25, 0.10, False),
    "guardrails.max_orders_per_day": (1, 30, 12, True),
    "guardrails.max_order_pct": (0.05, 0.60, 0.35, False),
    "guardrails.max_daily_turnover_pct": (0.10, 2.0, 1.0, False),
    "guardrails.drawdown_breaker": (0.05, 0.50, 0.35, False),
    "guardrails.breaker_risk_scale": (0.0, 1.0, 0.5, False),
    "guardrails.max_stale_days": (1, 7, 4, True),
    "guardrails.max_daily_move": (0.05, 0.50, 0.25, False),
    "costs.commission_per_order_usd": (0.0, 10.0, 0.35, False),
    "costs.slippage": (0.0, 0.01, 0.0005, False),
    "ai.low_balance_cny": (0, 100, 3, False),
    "shadow.max_single_weight": (0.05, 1.0, 0.5, False),
    "arena.capital_usd": (100, 1_000_000, 4200, False),
    "daily_overlay.share": (0.0, 1.0, 0.5, False),
    "ibkr.port": (1, 65535, 4002, True),
    "ibkr.client_id": (0, 999, 17, True),
}
# 路径: (合法值, 非法时换成的值——总是更安全的那个)
ENUM = {
    "mode": (("paper", "ibkr_paper", "ibkr_live"), "paper"),
    "execution": (("auto", "approve"), "approve"),
    "ai.effort": (("low", "high", "max"), "max"),
    "ai.on_failure": (("rules", "hold"), "rules"),
    "ibkr.tif": (("OPG", "DAY"), "OPG"),
    "paper_fill": (("next_open", "close"), "next_open"),
    "risk_level": (("conservative", "balanced", "growth", "aggressive"), "conservative"),
}
# 路径: 非法时换成的值
BOOL = {
    "fractional_shares": False, "paper_like_ibkr": True, "monthly_tranches": True,
    "ai.enabled": True, "ai.news": True, "ai.daily": True,
    "shadow.enabled": True, "arena.enabled": True, "ibkr.dry_run": True,
    "publish_dashboard": False,
}


def _get(cfg, path):
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None, False
        cur = cur[k]
    return cur, True


def _set(cfg, path, value):
    keys = path.split(".")
    cur = cfg
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def check(cfg):
    """返回 (修正后的配置, 修正说明列表)。"""
    import engine
    import strategies_daily

    notes = []

    def fix(path, new, why):
        _set(cfg, path, new)
        notes.append(f"配置 {path} {why}，本次按 {new!r} 运行")

    for path, (lo, hi, default, is_int) in NUM.items():
        v, present = _get(cfg, path)
        if not present:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            fix(path, default, f"= {v!r} 不是数字")
        elif v < lo or v > hi:
            fix(path, min(max(v, lo), hi), f"= {v} 超出允许范围 [{lo}, {hi}]")
        elif is_int and int(v) != v:
            fix(path, int(v), f"= {v} 应为整数")

    for path, (allowed, safe) in ENUM.items():
        v, present = _get(cfg, path)
        if present and v not in allowed:
            fix(path, safe, f"= {v!r} 不在允许值 {list(allowed)} 中")

    for path, safe in BOOL.items():
        v, present = _get(cfg, path)
        if present and not isinstance(v, bool):
            fix(path, safe, f"= {v!r} 应为 true/false")

    # 白名单：只保留 UNIVERSE 里的 ETF，现金必须是 BIL
    wl = cfg.get("whitelist") if isinstance(cfg.get("whitelist"), list) else []
    bad = [s for s in wl if s not in UNIVERSE]
    good = [s for s in wl if s in UNIVERSE]
    if bad:
        notes.append(f"白名单里 {bad} 不在允许范围（只能是指数/债券/商品 ETF 和 SSO、QLD），已剔除")
    if CASH not in good:
        good.append(CASH)
    cfg["whitelist"] = good
    if cfg.get("cash_symbol") != CASH:
        fix("cash_symbol", CASH, f"= {cfg.get('cash_symbol')!r}，现金只能用短期美债 BIL")
    smap = (cfg.get("ibkr") or {}).get("symbol_map") or {}
    for k, v in list(smap.items()):
        if v not in UNIVERSE:
            smap.pop(k)
            notes.append(f"代码替换 {k}→{v} 不在允许范围，已取消")

    # 策略组合：引擎必须存在，权重非负、合计不超过 1
    profiles = cfg.get("profiles") if isinstance(cfg.get("profiles"), dict) else {}
    for name, eng in list(profiles.items()):
        if not isinstance(eng, dict):
            profiles.pop(name)
            continue
        for e, w in list(eng.items()):
            if e not in engine.ENGINES or isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
                eng.pop(e)
                notes.append(f"策略组合 {name} 里的 {e}={w!r} 无效，已移除")
        total = sum(eng.values())
        if total > 1 + 1e-9:
            for e in eng:
                eng[e] /= total
            notes.append(f"策略组合 {name} 的权重合计 {total:.2f} 超过 1（会变成加杠杆），已按比例缩到 1")
    if cfg.get("profile") not in profiles or not profiles.get(cfg.get("profile")):
        safe = "conservative" if profiles.get("conservative") else next((k for k, v in profiles.items() if v), None)
        notes.append(f"profile = {cfg.get('profile')!r} 不存在或为空，本次按 {safe!r} 运行")
        cfg["profile"] = safe
        if safe is None:
            raise SystemExit("配置里没有任何可用的策略组合（profiles），无法运行")

    ov = cfg.get("daily_overlay")
    if ov and ov.get("strategy") not in strategies_daily.STRAT_BY_KEY:
        notes.append(f"按日叠加策略 {ov.get('strategy')!r} 不存在，本次不叠加")
        cfg["daily_overlay"] = None
    members = (cfg.get("arena") or {}).get("members") or {}
    allowed_members = ({"monthly_growth", "volmix"} | set(strategies_daily.STRAT_BY_KEY)
                       | {"level_" + k for k in strategies_daily.RISK_LEVELS})
    for k in [k for k in members if k not in allowed_members]:
        members.pop(k)
        notes.append(f"擂台成员 {k!r} 不存在，已移除")

    # 风险档位 → 具体参数：分批月度趋势（两倍 ETF 比例）+ 波动率管理（目标波动、最高档位）+ 常备现金
    lv_key = cfg.get("risk_level")
    if lv_key:
        lv = strategies_daily.RISK_LEVELS[lv_key]
        cfg["monthly_tranches"] = True
        cfg["tranche_2x"] = lv["two"]
        cfg["daily_overlay"] = {"strategy": "vol_managed", "share": 0.5,
                                "params": {"tv": lv["tv"], "cap": lv["cap"]}}
        cfg["risk_scale"] = lv.get("scale", 1.0)

    # 实盘准入：必须有确认口令和资金上限，否则只读
    if cfg.get("mode") == "ibkr_live":
        ib = cfg.setdefault("ibkr", {})
        cap = ib.get("max_capital_usd")
        if cfg.get("live_confirmation") != LIVE_PHRASE:
            ib["dry_run"] = True
            notes.append(f'实盘模式需要在配置里加上 "live_confirmation": "{LIVE_PHRASE}"，未确认，本次只读不下单')
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not (100 <= cap <= 10_000_000):
            ib["dry_run"] = True
            notes.append(f"实盘模式必须设置 ibkr.max_capital_usd（100～10,000,000 美元），当前 {cap!r}，本次只读不下单")
    return cfg, notes


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load(path):
    cfg = json.load(open(path, encoding="utf-8"))
    local = os.path.join(os.path.dirname(os.path.abspath(path)), "config.local.json")
    if os.path.exists(local):
        _merge(cfg, json.load(open(local, encoding="utf-8")))
    return check(cfg)
