"""
按日回测"成长档"（SPY/QQQ/SSO/QLD 各 25%，趋势不在就换短期美债 BIL），
比较不同的信号检查频率和触发条件。决策用当天收盘价，下一个交易日开盘价成交。
用法：../.venv/bin/python backtest_daily.py
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SLEEVES = [("SPY", "SPY"), ("QQQ", "QQQ"), ("SPY", "SSO"), ("QQQ", "QLD")]  # (信号看谁, 持有谁)
SAFE, START, COST = "BIL", "2007-06-01", 0.001  # 每次换仓按 0.1% 计成本（卖+买、滑点）


def load(sym):
    c = dict(json.load(open(os.path.join(HERE, "cache", f"{sym}.json"))))
    o = dict(json.load(open(os.path.join(HERE, "cache", f"{sym}.open.json"))))
    return c, o


def run(rule, label):
    C, O = {}, {}
    for s in {"SPY", "QQQ", "SSO", "QLD", SAFE}:
        C[s], O[s] = load(s)
    days = sorted(set.intersection(*(set(C[s]) for s in C)))
    sig_hist = {s: [] for s in ("SPY", "QQQ")}
    month_ends = {s: [] for s in ("SPY", "QQQ")}
    vals = [0.25] * 4
    hold = [SAFE] * 4
    peak, mdd, switches, navs, pending = 1.0, 0.0, 0, [], {}
    yearly = {}
    for i, d in enumerate(days):
        if i > 0:
            p = days[i - 1]
            for k in range(4):
                vals[k] *= C[hold[k]][d] / C[hold[k]][p]
        # 换仓：昨天收盘做的决定，今天开盘执行
        if pending:
            for k, new in pending.items():
                old = hold[k]
                # 昨收→今开按旧资产算，今开→今收按新资产算
                vals[k] *= (O[old][d] / C[old][d]) * (C[new][d] / O[new][d]) * (1 - COST)
                hold[k] = new
                switches += 1
        pending = {}
        for s in ("SPY", "QQQ"):
            sig_hist[s].append(C[s][d])
        is_month_end = i + 1 < len(days) and days[i + 1][:7] != d[:7]
        if is_month_end:
            for s in ("SPY", "QQQ"):
                month_ends[s].append(C[s][d])
            tot = sum(vals)
            vals = [tot / 4] * 4  # 月末各仓位拉回 25%
        if d >= START:
            for k, (sig, asset) in enumerate(SLEEVES):
                want_in = rule(sig_hist[sig], month_ends[sig], is_month_end, d, hold[k] == asset)
                if want_in is None:
                    continue
                target = asset if want_in else SAFE
                if target != hold[k]:
                    pending[k] = target
        if d < START:
            continue
        if not navs:
            base = sum(vals)
        nav = sum(vals) / base
        navs.append((d, nav))
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
        yearly.setdefault(d[:4], [nav, nav])[1] = nav
    years = len(navs) / 252
    cagr = navs[-1][1] ** (1 / years) - 1
    r12 = [navs[k + 252][1] / navs[k][1] - 1 for k in range(len(navs) - 252)]
    rets = [navs[k + 1][1] / navs[k][1] - 1 for k in range(len(navs) - 1)]
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 * math.sqrt(252)
    prev = 1.0
    yr = {}
    for y in sorted(yearly):
        yr[y] = yearly[y][1] / prev - 1
        prev = yearly[y][1]
    return {"方案": label, "年化": cagr, "波动": vol, "最大回撤": mdd, "最差12月": min(r12),
            "每年换仓": switches / years, "2008": yr.get("2008"), "2020": yr.get("2020"),
            "2022": yr.get("2022"), "2025": yr.get("2025")}


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def monthly_10m(h, me, is_me, d, inside):
    """现行规则：月末收盘价 vs 最近 10 个月末收盘均线。"""
    if not is_me or len(me) < 10:
        return None
    return me[-1] > sum(me[-10:]) / 10


def daily_200(band=0.0, weekly=False):
    def rule(h, me, is_me, d, inside):
        from datetime import date
        if weekly and date.fromisoformat(d).weekday() != 4:
            return None
        m = sma(h, 200)
        if m is None:
            return None
        if inside:
            return h[-1] > m * (1 - band)   # 已持有：跌破均线 band 才卖
        return h[-1] > m * (1 + band)       # 未持有：涨过均线 band 才买
    return rule


def month_in_daily_out(band):
    """月末才买（同现行规则），但每天检查，跌破 10 月均线 band 就立刻卖。"""
    def rule(h, me, is_me, d, inside):
        if is_me and len(me) >= 10:
            return me[-1] > sum(me[-10:]) / 10
        if inside and len(me) >= 10:
            m = (sum(me[-9:]) + h[-1]) / 10  # 用今天价格代替本月末
            if h[-1] < m * (1 - band):
                return False
        return None
    return rule


if __name__ == "__main__":
    rows = [
        run(monthly_10m, "现行：每月末检查"),
        run(daily_200(weekly=True), "每周五检查"),
        run(daily_200(), "每天检查"),
        run(daily_200(0.01), "每天检查+1%缓冲"),
        run(daily_200(0.02), "每天检查+2%缓冲"),
        run(daily_200(0.03), "每天检查+3%缓冲"),
        run(month_in_daily_out(0.03), "月末买+跌破3%即卖"),
        run(month_in_daily_out(0.05), "月末买+跌破5%即卖"),
    ]
    cols = ["年化", "波动", "最大回撤", "最差12月", "每年换仓", "2008", "2020", "2022", "2025"]
    print(f"{'':16s}" + "".join(f"{c:>9s}" for c in cols))
    for r in rows:
        cells = "".join(f"{r[c]:9.1f}" if c == "每年换仓" else f"{r[c]:9.1%}" for c in cols)
        print(f"{r['方案']:16s}" + cells)
