"""
按日回测 strategies_daily.py 里的策略。
- 每天收盘后算目标，下一个交易日开盘价成交（昨收→今开按旧仓位，今开→今收按新仓位）
- 某个资产目标与实际偏差小于 2% 不交易（和实盘一样），目标为 0 时总是清仓
- 成本：每笔订单 0.05% 的成交额（滑点+价差）+ 固定 1 美元佣金（按 4200 美元本金折算）
用法：python backtest_flex.py [--levels] [--split] [--cost]
  --levels  只看四个风险档位（稳健/平衡/成长/激进）和满仓标普 500 对照
  --split   另外按 2008-2016、2017 至今两段分别统计（检查是不是只在某一段好看）
  --cost    换几种成本假设重跑
"""
import math
import sys

import data
import strategies_daily as sd

TRADE = ["SPY", "QQQ", "SSO", "QLD", "BIL"]
START, BAND, SLIP, FEE = "2008-01-02", 0.02, 0.0005, 1.0 / 4200


def load():
    rows = {s: data.daily(s) for s in sd.SIGNAL_SYMBOLS}
    h = sd.H(rows)
    opens = {}
    for s in TRADE:
        data.first_open_after(s, "1900-01-01")  # 确保开盘价缓存存在
        import json, os
        opens[s] = dict(json.load(open(os.path.join(data.CACHE, f"{s}.open.json"))))
    return h, opens


def simulate(h, opens, strat, start=START, end="9999", fee=FEE, band=BAND, weekly=False):
    d = h.dates
    i0 = next(k for k, x in enumerate(d) if x >= start)
    hold = {"BIL": 1.0}  # 持仓市值（以净值为单位）
    pending, last_target = None, {"BIL": 1.0}
    navs, orders, exposure = [], 0, []
    for i in range(i0, len(d)):
        if d[i] > end:
            break
        if i > i0:
            if pending:
                # 昨收 → 今开：旧仓位
                for s in hold:
                    hold[s] *= opens[s][d[i]] / h.c[s][i - 1]
                nav = sum(hold.values())
                cost = 0.0
                for s in set(pending) | set(hold):
                    cur, tgt = hold.get(s, 0) / nav, pending.get(s, 0)
                    if abs(tgt - cur) < band and not (tgt == 0 and cur > 0):
                        continue
                    cost += abs(tgt - cur) * nav * SLIP + fee * nav
                    hold[s] = tgt * nav
                    orders += 1
                # BIL 当现金用：吸收没交易的偏差和交易成本，保证总市值守恒
                hold["BIL"] = nav - cost - sum(v for s, v in hold.items() if s != "BIL")
                hold = {s: v for s, v in hold.items() if abs(v) > 1e-9}
                # 今开 → 今收：新仓位
                for s in hold:
                    hold[s] *= h.c[s][i] / opens[s][d[i]]
                pending = None
            else:
                for s in hold:
                    hold[s] *= h.c[s][i] / h.c[s][i - 1]
        nav = sum(hold.values())
        navs.append(nav)
        exposure.append((hold.get("SPY", 0) + hold.get("QQQ", 0) + 2 * hold.get("SSO", 0) + 2 * hold.get("QLD", 0)) / nav)
        out = strat(h, i)
        if weekly and out is not None and not isinstance(out, dict):
            from datetime import date as _d
            if _d.fromisoformat(d[i]).weekday() != 4:
                out = None  # 每周只在周五收盘后决策一次
        if out is not None:
            tgt = out if isinstance(out, dict) else sd.weights(out)
            if tgt != last_target:
                pending, last_target = tgt, tgt
            else:
                # 目标没变，但仓位漂移超过区间也要再平衡
                cur = {s: v / nav for s, v in hold.items()}
                if any(abs(tgt.get(s, 0) - cur.get(s, 0)) >= band for s in set(tgt) | set(cur)):
                    pending = tgt
    return stats(d[i0:i0 + len(navs)], navs, orders, exposure)


def stats(dates, navs, orders, exposure):
    years = len(navs) / 252
    cagr = (navs[-1] / navs[0]) ** (1 / years) - 1
    rets = [navs[k] / navs[k - 1] - 1 for k in range(1, len(navs))]
    mu = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets) * 252)
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    r12 = [navs[k + 252] / navs[k] - 1 for k in range(len(navs) - 252)] or [navs[-1] / navs[0] - 1]
    by_year, prev = {}, navs[0]
    for k, dt in enumerate(dates):
        if k + 1 == len(dates) or dates[k + 1][:4] != dt[:4]:
            by_year[dt[:4]] = navs[k] / prev - 1
            prev = navs[k]
    return {"dates": dates, "navs": navs, "年化": cagr, "波动": vol, "收益/回撤": cagr / -mdd if mdd else 0, "最大回撤": mdd,
            "最差12月": min(r12), "亏钱12月占比": sum(r < 0 for r in r12) / len(r12),
            "年均下单": orders / years, "平均档位": sum(exposure) / len(exposure), "years": by_year}


def table(results, years=("2008", "2011", "2018", "2020", "2022", "2025")):
    cols = ["年化", "波动", "最大回撤", "最差12月", "亏钱12月占比", "收益/回撤", "年均下单", "平均档位"]
    print(f"{'':18s}" + "".join(f"{c:>9s}" for c in cols) + "".join(f"{y:>8s}" for y in years))
    for name, r in results.items():
        cells = ""
        for c in cols:
            v = r[c]
            cells += f"{v:9.1f}" if c in ("年均下单",) else f"{v:9.2f}" if c in ("收益/回撤", "平均档位") else f"{v:9.1%}"
        cells += "".join(f"{r['years'].get(y, float('nan')):8.1%}" for y in years)
        print(f"{name:18s}{cells}")


LIVE = dict(fee=0.35 / 4200, band=0.10)  # 和实盘配置一致：每笔约 0.35 美元（IBKR Pro），偏离 10% 才交易


def buy_hold(h, sym, start=START, end="9999"):
    idx = [k for k, d in enumerate(h.dates) if start <= d <= end]
    return stats([h.dates[k] for k in idx], [h.c[sym][k] for k in idx], 0, [1.0] * len(idx))


if __name__ == "__main__":
    h, opens = load()
    if "--levels" in sys.argv:
        allstrats = {f"{lv['name']}档": sd.level_strategy(k) for k, lv in sd.RISK_LEVELS.items()}
    else:
        allstrats = {**sd.STRATEGIES, **sd.COMBOS}
        main_name = "现主策略：分批月度50%+波动率50%"
        allstrats = {"★ " + main_name: allstrats.pop(main_name), **allstrats}
    print(f"数据：{h.dates[0]} ~ {h.dates[-1]}，回测从 {START} 起，次日开盘成交，含成本")
    print("默认设置与实盘一致：每笔约 0.35 美元佣金 + 0.05% 滑点，偏离目标 10% 以上才交易\n")
    print("### 全周期")
    table({**{n: simulate(h, opens, f, **LIVE) for n, f in allstrats.items()}, "对照：满仓 SPY": buy_hold(h, "SPY")})
    if "--split" in sys.argv:
        for a, b, t in (("2008-01-02", "2016-12-31", "前半 2008-2016"), ("2017-01-01", "9999", "后半 2017 至今")):
            print(f"\n### {t}")
            table({n: simulate(h, opens, f, a, b, **LIVE) for n, f in allstrats.items()}, years=())
    if "--cost" in sys.argv:
        print("\n### 成本拆解：每笔 1 美元、偏离 2% 就交易（天天交易的策略会被佣金吃掉）")
        table({n: simulate(h, opens, f, fee=1 / 4200, band=0.02) for n, f in allstrats.items()}, years=())
        print("\n### 成本拆解：零佣金、偏离 2% 就交易（只看信号本身好不好）")
        table({n: simulate(h, opens, f, fee=0, band=0.02) for n, f in allstrats.items()}, years=())
