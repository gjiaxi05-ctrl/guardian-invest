"""
生成策略看板 reports/dashboard.html：
- 历史回测：四个风险档位和研究过的其他策略 2008 年至今的净值、回撤、逐年收益、分时段指标
  （和 backtest_flex.py 同一套设置）
- 向前实测：规则主账户、AI 影子账户、策略擂台每个账户的每日净值
每天定时任务跑完后自动重新生成；也可以手动运行：python3 dashboard.py
加 --publish 时，如果配置里 publish_dashboard 为 true，再把看板推到 GitHub 的 gh-pages 分支（GitHub Pages 在线版）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime

import backtest_flex as bf
import strategies_daily as sd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reports", "dashboard.html")            # 本地打开用（完整网页）
OUT_ARTIFACT = os.path.join(HERE, "reports", "dashboard_artifact.html")  # 发布在线版用（不带外层骨架）
TEMPLATE = os.path.join(HERE, "dashboard_template.html")

# 图上画的是四个风险档位（颜色按 key 固定）；其余研究过的策略只进指标表。每项都带中英文名，看板可切换语言
LEVEL_KEYS = {"conservative": "cons", "balanced": "bal", "growth": "main", "aggressive": "aggr"}
CHARTED = [(LEVEL_KEYS[k], f"{lv['name']}档" + ("（作者在跑）" if k == "growth" else ""),
            lv["name_en"] + (" (author's own)" if k == "growth" else ""), sd.level_strategy(k))
           for k, lv in sd.RISK_LEVELS.items()]
TABLE_ONLY = [
    ("old", "旧主策略：月度成长档", "Old main: monthly growth", sd.monthly_growth),
    ("prev", "上一版主策略：月末判定 + 波动率", "Previous main: month-end check + volatility",
     sd.COMBOS["50%月度成长+50%波动率管理"]),
    ("tranche", "分批月度（不含波动率）", "Tranched monthly trend only", sd.monthly_tranches),
    ("vol", "波动率管理", "Volatility management", sd.vol_managed),
    ("txv", "趋势×波动", "Trend × volatility", sd.trend_x_vol),
    ("dash", "多信号仪表盘", "Multi-signal dashboard", sd.dashboard),
    ("vix", "VIX 期限结构", "VIX term structure", sd.vix_term),
    ("credit", "信用债金丝雀", "Credit canary", sd.credit_canary),
    ("tvote", "趋势投票×波动率", "Trend votes × volatility", sd.trend_vol),
    ("rsi2", "趋势中抄短线 RSI2", "Buy dips in uptrend (RSI2)", sd.dip_buyer),
    ("tom", "月初效应", "Turn of the month", sd.turn_of_month),
    ("mix2", "50% 趋势×波动 + 50% 仪表盘", "50% trend×vol + 50% dashboard", sd.COMBOS["50%趋势×波动+50%仪表盘"]),
]
# 向前实测账户的英文名（中文名来自 config.json 的擂台设置）
FORWARD_EN = {
    "fwd_main": "Main account (rules + AI review, whole shares)",
    "fwd_shadow": "AI shadow account (DeepSeek in full control)",
    "monthly_growth": "Arena: old monthly growth",
    "volmix": "Arena: Growth level (no AI)",
    "vol_managed": "Arena: volatility management",
    "trend_x_vol": "Arena: trend × volatility",
    "dashboard": "Arena: multi-signal dashboard",
    "vix_term": "Arena: VIX term structure",
    "dip_buyer": "Arena: buy dips in uptrend",
    **{"level_" + k: f"Arena: {lv['name_en']} level" for k, lv in sd.RISK_LEVELS.items()},
}
PERIODS = [("all", "全周期", "Full period", bf.START, "9999"),
           ("h1", "2008–2016", "2008–2016", "2008-01-02", "2016-12-31"),
           ("h2", "2017 至今", "2017–today", "2017-01-01", "9999")]
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
        series.append({"key": "fwd_main", "name": "规则主账户（含 AI 风控、整股）", "name_en": FORWARD_EN["fwd_main"],
                       "points": st["nav_history"]})
    sj = os.path.join(HERE, "state", "shadow_journal.jsonl")
    if os.path.exists(sj):
        pts = {}
        for line in open(sj):
            r = json.loads(line)
            pts[r["date"]] = r["nav"]
        series.append({"key": "fwd_shadow", "name": "AI 影子账户（DeepSeek 全权）", "name_en": FORWARD_EN["fwd_shadow"],
                       "points": sorted(pts.items())})
    arena = st.get("arena", {})
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json")))
        names = cfg.get("arena", {}).get("members", {})
    except (OSError, ValueError):
        names = {}
    for key, a in arena.get("accounts", {}).items():
        if a.get("history"):
            series.append({"key": "arena_" + key, "name": "擂台：" + names.get(key, key),
                           "name_en": FORWARD_EN.get(key, "Arena: " + key),
                           "points": a["history"], "mdd": a.get("mdd", 0)})
    for s in series:
        p0 = s["points"][0][1]
        s["ret"] = s["points"][-1][1] / p0 - 1
    return {"start": arena.get("start"), "series": series}


def build():
    h, opens = bf.load()
    hist, table = [], []
    for key, name, name_en, fn in CHARTED + TABLE_ONLY:
        row = {"key": key, "name": name, "name_en": name_en, "charted": any(key == k for k, *_ in CHARTED),
               "periods": {}}
        for pkey, _, _, a, b in PERIODS:
            r = bf.simulate(h, opens, fn, a, b, **bf.LIVE)
            row["periods"][pkey] = {m: r[m] for m in METRICS}
            if pkey == "all":
                row["years"] = r["years"]
                if row["charted"]:
                    hist.append({"key": key, "name": name, "name_en": name_en, "points": weekly(r["dates"], r["navs"])})
        table.append(row)
    spy = {"key": "spy", "name": "对照：一直满仓标普 500（SPY）", "name_en": "Benchmark: always 100% S&P 500 (SPY)",
           "charted": True, "periods": {}}
    for pkey, _, _, a, b in PERIODS:
        r = spy_buy_hold(h, a, b)
        spy["periods"][pkey] = {m: r[m] for m in METRICS}
        spy["periods"][pkey]["年均下单"] = 0
        if pkey == "all":
            spy["years"] = r["years"]
            hist.append({"key": "spy", "name": spy["name"], "name_en": spy["name_en"],
                         "points": weekly(r["dates"], r["navs"])})
    table.append(spy)
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_range": [h.dates[0], h.dates[-1]],
        "start": bf.START,
        "periods": [[k, n, en] for k, n, en, _, _ in PERIODS],
        "settings": "次日开盘价成交 · 每笔约 0.35 美元佣金 + 0.05% 滑点 · 偏离目标 10% 以上才交易",
        "settings_en": "fills at the next trading day's open · about $0.35 commission per order + 0.05% slippage · "
                       "trades only when a holding is 10% or more off target",
        "history": hist,
        "table": table,
        "forward": forward(),
    }


def publish():
    """把看板推到 origin 的 gh-pages 分支。这个分支每次强制覆盖成只有一个提交，
    main 上不会每天多一条数据提交。失败只打印原因，不影响交易和本地看板。"""
    import config_guard
    cfg, _ = config_guard.load(os.path.join(HERE, "config.json"))
    if not cfg.get("publish_dashboard"):
        print("publish_dashboard 未开启，不发布在线看板")
        return

    def git(*args, cwd=HERE):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=True).stdout.strip()

    tmp = tempfile.mkdtemp(prefix="guardian-pages-")
    try:
        url = git("remote", "get-url", "origin")
        name = git("config", "user.name") or "guardian"
        email = git("config", "user.email") or "guardian@users.noreply.github.com"
        shutil.copy(OUT, os.path.join(tmp, "index.html"))
        open(os.path.join(tmp, ".nojekyll"), "w").close()  # 纯静态页，不让 GitHub 用 Jekyll 处理
        git("init", "-q", "-b", "gh-pages", cwd=tmp)
        git("add", "-A", cwd=tmp)
        git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m",
            f"Dashboard {datetime.now():%Y-%m-%d %H:%M}", cwd=tmp)
        git("push", "-q", "-f", url, "gh-pages", cwd=tmp)
        print("在线看板已发布到 gh-pages")
    except (OSError, subprocess.SubprocessError) as e:
        err = getattr(e, "stderr", "") or str(e)
        print(f"在线看板发布失败（不影响交易）：{err.strip()[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    if "--publish" in sys.argv:
        publish()


if __name__ == "__main__":
    main()
