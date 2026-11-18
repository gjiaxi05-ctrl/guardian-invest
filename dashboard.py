"""
生成策略看板 reports/dashboard.html：
- 历史回测：四个风险档位和研究过的其他策略 2008 年至今的净值、回撤、逐年收益、分时段指标
  （和 backtest_flex.py 同一套设置）
- 向前实测：规则主账户、AI 影子账户、策略擂台每个账户的每日净值
每天定时任务跑完后自动重新生成；也可以手动运行：../.venv/bin/python dashboard.py
"""
import json
import os
from datetime import date, datetime

import backtest_flex as bf
import strategies_daily as sd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reports", "dashboard.html")            # 本地打开用（完整网页）
OUT_ARTIFACT = os.path.join(HERE, "reports", "dashboard_artifact.html")  # 发布在线版用（不带外层骨架）
TEMPLATE = os.path.join(HERE, "dashboard_template.html")

# 图上画的是四个风险档位（颜色按 key 固定）；其余研究过的策略只进指标表
LEVEL_KEYS = {"conservative": "cons", "balanced": "bal", "growth": "main", "aggressive": "aggr"}
CHARTED = [(LEVEL_KEYS[k], f"{lv['name']}档" + ("（作者在跑）" if k == "growth" else ""), sd.level_strategy(k))
           for k, lv in sd.RISK_LEVELS.items()]
TABLE_ONLY = [
    ("old", "旧主策略：月度成长档", sd.monthly_growth),
    ("prev", "上一版主策略：月末判定 + 波动率", sd.COMBOS["50%月度成长+50%波动率管理"]),
    ("tranche", "分批月度（不含波动率）", sd.monthly_tranches),
    ("vol", "波动率管理", sd.vol_managed),
    ("txv", "趋势×波动", sd.trend_x_vol),
    ("dash", "多信号仪表盘", sd.dashboard),
    ("vix", "VIX 期限结构", sd.vix_term),
    ("credit", "信用债金丝雀", sd.credit_canary),
    ("tvote", "趋势投票×波动率", sd.trend_vol),
    ("rsi2", "趋势中抄短线 RSI2", sd.dip_buyer),
    ("tom", "月初效应", sd.turn_of_month),
    ("mix2", "50% 趋势×波动 + 50% 仪表盘", sd.COMBOS["50%趋势×波动+50%仪表盘"]),
]
PERIODS = [("all", "全周期", bf.START, "9999"), ("h1", "2008–2016", "2008-01-02", "2016-12-31"),
           ("h2", "2017 至今", "2017-01-01", "9999")]
METRICS = ["年化", "最大回撤", "最差12月", "亏钱12月占比", "收益/回撤", "年均下单", "平均档位"]


def weekly(dates, navs):
    """每周最后一个交易日取一个点，图上约 1000 个点。"""
    out = []
    for k, d in enumerate(dates):
        nxt = dates[k + 1] if k + 1 < len(dates) else None
        if nxt is None or date.fromisoformat(nxt).isocalendar()[:2] != date.fromisoformat(d).isocalendar()[:2]:
            out.append([d, round(navs[k] / navs[0], 5)])
    return out


def spy_buy_hold(h, start, end="9999"):
    i0 = next(k for k, d in enumerate(h.dates) if d >= start)
    idx = [k for k in range(i0, len(h.dates)) if h.dates[k] <= end]
    dates = [h.dates[k] for k in idx]
    navs = [h.c["SPY"][k] for k in idx]
    return bf.stats(dates, navs, 0, [1.0] * len(navs))


def forward():
    """向前实测：从状态文件读取每日净值。"""
    st_path = os.path.join(HERE, "state", "system.json")
    st = json.load(open(st_path)) if os.path.exists(st_path) else {}
    series = []
    if st.get("nav_history"):
        series.append({"key": "fwd_main", "name": "规则主账户（含 AI 风控、整股）", "points": st["nav_history"]})
    sj = os.path.join(HERE, "state", "shadow_journal.jsonl")
    if os.path.exists(sj):
        pts = {}
        for line in open(sj):
            r = json.loads(line)
            pts[r["date"]] = r["nav"]
        series.append({"key": "fwd_shadow", "name": "AI 影子账户（DeepSeek 全权）", "points": sorted(pts.items())})
    arena = st.get("arena", {})
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json")))
        names = cfg.get("arena", {}).get("members", {})
    except (OSError, ValueError):
        names = {}
    for key, a in arena.get("accounts", {}).items():
        if a.get("history"):
            series.append({"key": "arena_" + key, "name": "擂台：" + names.get(key, key),
                           "points": a["history"], "mdd": a.get("mdd", 0)})
    for s in series:
        p0 = s["points"][0][1]
        s["ret"] = s["points"][-1][1] / p0 - 1
    return {"start": arena.get("start"), "series": series}


def build():
    h, opens = bf.load()
    hist, table = [], []
    for key, name, fn in CHARTED + TABLE_ONLY:
        row = {"key": key, "name": name, "charted": any(key == k for k, _, _ in CHARTED), "periods": {}}
        for pkey, _, a, b in PERIODS:
            r = bf.simulate(h, opens, fn, a, b, **bf.LIVE)
            row["periods"][pkey] = {m: r[m] for m in METRICS}
            if pkey == "all":
                row["years"] = r["years"]
                if row["charted"]:
                    hist.append({"key": key, "name": name, "points": weekly(r["dates"], r["navs"])})
        table.append(row)
    spy = {"key": "spy", "name": "对照：一直满仓标普 500（SPY）", "charted": True, "periods": {}}
    for pkey, _, a, b in PERIODS:
        r = spy_buy_hold(h, a, b)
        spy["periods"][pkey] = {m: r[m] for m in METRICS}
        spy["periods"][pkey]["年均下单"] = 0
        if pkey == "all":
            spy["years"] = r["years"]
            hist.append({"key": "spy", "name": spy["name"], "points": weekly(r["dates"], r["navs"])})
    table.append(spy)
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_range": [h.dates[0], h.dates[-1]],
        "start": bf.START,
        "periods": [[k, n] for k, n, _, _ in PERIODS],
        "settings": "次日开盘价成交 · 每笔约 0.35 美元佣金 + 0.05% 滑点 · 偏离目标 10% 以上才交易",
        "history": hist,
        "table": table,
        "forward": forward(),
    }


def main():
    payload = json.dumps(build(), ensure_ascii=False, separators=(",", ":"))
    html = open(TEMPLATE, encoding="utf-8").read().replace("/*__DATA__*/null", payload)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT_ARTIFACT, "w", encoding="utf-8").write(html)
    page = ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>\n'
            + html + "\n</body></html>\n")
    open(OUT, "w", encoding="utf-8").write(page)
    print(f"已生成 {OUT}（{len(page) / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
