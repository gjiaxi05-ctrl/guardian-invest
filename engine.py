"""规则引擎：用上一个完整月的月末数据算目标仓位（和回测同一套代码）。"""
from datetime import datetime, timezone

import strategies as st

DATA_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "IEF", "TLT", "TIP",
                "LQD", "SHY", "BIL", "AGG", "SSO", "QLD", "SPYM", "QQQM"]
ENGINES = st.ENGINES


def load_market():
    mpx, dpx = st.load(DATA_SYMBOLS)
    months = sorted(m for m in mpx["SPY"] if m >= "2005-01")
    last_day = dpx["SPY"][-1][0]
    # 数据最后一天所在的月份如果就是本月，说明这个月还没走完，信号用上个月
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    signal_idx = len(months) - 2 if months[-1] == this_month else len(months) - 1
    return {"mpx": mpx, "dpx": dpx, "months": months, "signal_idx": signal_idx,
            "signal_month": months[signal_idx], "last_day": last_day}


def latest_prices(mkt):
    return {s: rows[-1][1] for s, rows in mkt["dpx"].items()}


def targets(mkt, engine_weights, cash_symbol):
    """返回 (合并后的目标权重, 每个引擎各自的权重)。"""
    ctx = st.Ctx(mkt["months"], mkt["mpx"], mkt["dpx"], mkt["signal_idx"])
    per_engine, blended = {}, {}
    for name, ew in engine_weights.items():
        w = ENGINES[name](ctx)
        per_engine[name] = {s: round(x, 4) for s, x in w.items() if x > 1e-4}
        for s, x in w.items():
            s = cash_symbol if s == st.CASH else s
            blended[s] = blended.get(s, 0) + x * ew
    blended[cash_symbol] = blended.get(cash_symbol, 0) + max(0.0, 1 - sum(blended.values()))
    return {s: x for s, x in blended.items() if x > 1e-4}, per_engine


def snapshot(mkt, symbols):
    """给 AI 和日报看的行情摘要。"""
    ctx = st.Ctx(mkt["months"], mkt["mpx"], mkt["dpx"], len(mkt["months"]) - 1)
    rows = {}
    for s in symbols:
        sma10 = ctx.sma(s, 10)
        rows[s] = {
            "1m": _pct(ctx.ret(s, 1)), "3m": _pct(ctx.ret(s, 3)), "12m": _pct(ctx.ret(s, 12)),
            "vs_10m_sma": _pct(ctx.px(s) / sma10 - 1 if sma10 else None),
            "vol_63d": _pct(ctx.vol(s)),
        }
    return rows


def _pct(x):
    return None if x is None else round(100 * x, 1)
