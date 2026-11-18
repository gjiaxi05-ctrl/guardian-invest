"""
月度回测：月末收盘算信号、按月末价成交，持有到下个月末。
用法：python3 backtest.py            # 全部策略对比
      python3 backtest.py --robust   # Guardian 参数敏感性
"""
import json
import math
import os
import statistics
import sys

import strategies as st

SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "IEF", "TLT", "TIP",
           "LQD", "SHY", "BIL", "AGG", "SSO", "QLD"]
START, COST = "2007-03", 0.001  # 2007-03 起所有资产都有 12 个月历史；单边成本 0.1%


def run(fn, mpx, dpx, start=START, end=None):
    months = sorted(m for m in mpx["SPY"] if m >= "2005-01")
    last_full = months[-2]  # 最后一个月未走完，不计入
    end = min(end or last_full, last_full)
    i0 = months.index(start)
    navs, rets, cash_rets, turnover, held = [1.0], [], [], 0.0, {}
    for i in range(i0, months.index(end)):
        w = fn(st.Ctx(months, mpx, dpx, i))
        w = {s: x for s, x in w.items() if x > 1e-6}
        w[st.CASH] = w.get(st.CASH, 0) + max(0.0, 1 - sum(w.values()))
        t = sum(abs(w.get(s, 0) - held.get(s, 0)) for s in set(w) | set(held))
        turnover += t / 2
        a, b = months[i], months[i + 1]
        r = sum(x * (mpx[s][b] / mpx[s][a] - 1) for s, x in w.items()) - t * COST
        rets.append(r)
        cash_rets.append(mpx[st.CASH][b] / mpx[st.CASH][a] - 1)
        navs.append(navs[-1] * (1 + r))
        # 持仓随行情漂移，下次调仓按漂移后的权重算换手
        grown = {s: x * mpx[s][b] / mpx[s][a] for s, x in w.items()}
        tot = sum(grown.values())
        held = {s: x / tot for s, x in grown.items()}
    return stats(navs, rets, cash_rets, turnover)


def stats(navs, rets, cash_rets, turnover):
    years = len(rets) / 12
    cagr = navs[-1] ** (1 / years) - 1
    vol = statistics.stdev(rets) * math.sqrt(12)
    ex = [r - c for r, c in zip(rets, cash_rets)]
    sharpe = statistics.mean(ex) / statistics.stdev(ex) * math.sqrt(12)
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    r12 = [navs[k + 12] / navs[k] - 1 for k in range(len(navs) - 12)]
    return {
        "年化": cagr, "波动": vol, "夏普": sharpe, "最大回撤": mdd,
        "最差单月": min(rets), "最差12月": min(r12),
        "12月亏损概率": sum(r < 0 for r in r12) / len(r12),
        "年换手": turnover / years, "navs": navs,
    }


def table(results):
    cols = ["年化", "波动", "夏普", "最大回撤", "最差单月", "最差12月", "12月亏损概率", "年换手"]
    print(f"{'':14s}" + "".join(f"{c:>10s}" for c in cols))
    for name, r in results.items():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(f"{v:10.2f}" if c == "夏普" else f"{v:10.1%}")
        print(f"{name:14s}" + "".join(cells))


def main():
    mpx, dpx = st.load(SYMBOLS)
    if "--robust" in sys.argv:
        robust(mpx, dpx)
        return
    periods = [("全周期 2007-03 起", START, None), ("前半 2007-2016", START, "2016-12"),
               ("后半 2017 至今", "2016-12", None), ("2008 金融危机", "2007-10", "2009-03"),
               ("2022 股债双杀", "2021-12", "2022-12")]
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
    strats = dict(st.STRATEGIES)
    strats.update({f"[{name}]": st.blend(w) for name, w in cfg["profiles"].items()})
    for title, a, b in periods:
        print(f"\n### {title}")
        table({n: run(f, mpx, dpx, a, b) for n, f in strats.items()})


def robust(mpx, dpx):
    """换参数看结果是否稳定：好策略不该只在某一组参数下好看。"""
    print("\n### Guardian 参数敏感性（全周期）")
    res = {}
    for tv in (0.05, 0.08, 0.10, 0.12):
        for mb in (1.0, 1.5, 2.0):
            res[f"vol{tv:.0%} boost{mb}"] = run(
                lambda c, tv=tv, mb=mb: st.guardian(c, target_vol=tv, max_boost=mb), mpx, dpx)
    for lb in ((3, 6, 9, 12), (1, 3, 6, 12), (4, 8, 12), (6, 12)):
        res[f"lb{','.join(map(str, lb))}"] = run(lambda c, lb=lb: st.guardian(c, lookbacks=lb), mpx, dpx)
    res["去掉QQQ/GLD"] = run(lambda c: st.guardian(
        c, universe=("SPY", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT", "TIP", "LQD")), mpx, dpx)
    table(res)


if __name__ == "__main__":
    main()
