"""
每日运行一次（美股收盘后）。流程：
  急停检查 → 数据体检 → 估值/回撤熔断 → [月初] 规则出目标 → AI 只减不加 → 生成订单
  → 硬性风控截断 → 自动成交 或 等你审批 → 日报 + 推送
用法：
  python3 run.py              # 正常运行
  python3 run.py --approve    # 审批模式下，执行待审批订单
  python3 run.py --rebalance  # 强制重新计算目标（会再调一次 AI）
  python3 run.py --no-shadow  # 本次不运行 AI 影子账户
  touch STOP                  # 急停：存在 STOP 文件时什么都不做
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime

import ai_review
import config_guard
import broker as brokers
import engine
import shadow
import strategies_daily

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
SYS_STATE = os.path.join(STATE_DIR, "system.json")
PENDING = os.path.join(STATE_DIR, "pending_orders.json")
JOURNAL = os.path.join(STATE_DIR, "journal.jsonl")
REPORTS = os.path.join(HERE, "reports")


def load_json(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, ensure_ascii=False)


# ---------------- 数据体检 ----------------

def data_checks(mkt, cfg):
    g, problems = cfg["guardrails"], []
    age = (date.today() - date.fromisoformat(mkt["last_day"])).days
    if age > g["max_stale_days"]:
        problems.append(f"行情数据已 {age} 天未更新（最后 {mkt['last_day']}）")
    for s in cfg["whitelist"]:
        rows = mkt["dpx"][s]
        move = rows[-1][1] / rows[-2][1] - 1
        if abs(move) > g["max_daily_move"]:
            problems.append(f"{s} 单日变动 {move:+.1%}，疑似数据错误")
    return problems


# ---------------- 订单与风控 ----------------

def weights_now(br, prices, nav):
    return {s: q * prices[s] / nav for s, q in br.positions().items()}


def make_orders(target, br, prices, nav, cfg):
    band, frac = cfg["guardrails"]["rebalance_band"], cfg["fractional_shares"]
    cur, pos = weights_now(br, prices, nav), br.positions()
    orders = []
    for s in sorted(set(target) | set(cur)):
        tw, cw = target.get(s, 0.0), cur.get(s, 0.0)
        if tw == 0 and pos.get(s):
            qty = -pos[s]  # 目标为 0 时总是清仓，不受区间限制
        elif abs(tw - cw) < band:
            continue
        else:
            qty = (tw - cw) * nav / prices[s]
            qty = qty if frac else float(int(qty))
        if qty:
            orders.append({"symbol": s, "qty": qty, "value": round(qty * prices[s], 2),
                           "from_w": round(cw, 4), "to_w": round(tw, 4)})
    return sorted(orders, key=lambda o: o["qty"])  # 先卖后买


def guard(orders, cfg, nav, prices):
    """硬性限制，AI 和规则都绕不过去。卖单（降风险）优先保留。"""
    g, cash_sym = cfg["guardrails"], cfg["cash_symbol"]
    kept, blocked, turnover = [], [], 0.0
    for o in orders:
        if o["symbol"] not in cfg["whitelist"]:
            blocked.append({**o, "why": "不在白名单"})
            continue
        if len(kept) >= g["max_orders_per_day"]:
            blocked.append({**o, "why": "超过当日笔数上限"})
            continue
        cap = g["max_order_pct"] * nav
        if o["qty"] > 0 and o["symbol"] != cash_sym and o["value"] > cap:
            qty = float(int(cap / prices[o["symbol"]])) if not cfg["fractional_shares"] else cap / prices[o["symbol"]]
            blocked.append({**o, "why": f"单笔超过 {g['max_order_pct']:.0%}，截断"})
            o = {**o, "qty": qty, "value": round(qty * prices[o["symbol"]], 2)}
        # 每日成交额上限只限制"买入风险资产"：卖出、买短债都是降风险，不该被它挡住
        # （2026-10-28 发现：卖出也计入后，整体换仓时买单被拦，账户空仓了一天）
        risky_buy = o["qty"] > 0 and o["symbol"] != cash_sym
        if risky_buy and turnover + o["value"] > g["max_daily_turnover_pct"] * nav:
            blocked.append({**o, "why": "超过当日买入上限，明天继续"})
            continue
        if risky_buy:
            turnover += o["value"]
        if o["qty"]:
            kept.append(o)
    return kept, blocked


def execute(orders, br, cfg):
    fills = []
    for o in [o for o in orders if o["qty"] < 0]:
        fills.append(br.place(o["symbol"], o["qty"]))
    # 买单按剩余现金等比缩小，永不透支
    buys = [o for o in orders if o["qty"] > 0]
    need = sum(o["value"] for o in buys) * (1 + cfg["costs"]["slippage"]) + cfg["costs"]["commission_per_order_usd"] * len(buys)
    scale = min(1.0, br.cash() / need) if need > 0 else 1.0
    for o in buys:
        qty = o["qty"] * scale
        qty = qty if cfg["fractional_shares"] else float(int(qty))
        if qty > 0:
            fills.append(br.place(o["symbol"], qty))
    return fills


# ---------------- 推送 ----------------

def notify(text):
    """配置了 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 才会推送。"""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=15)
    except Exception as e:  # noqa: BLE001
        print("推送失败：", e)


# ---------------- 主流程 ----------------

def main():
    cfg, config_notes = config_guard.load(os.path.join(HERE, "config.json"))
    today = date.today().isoformat()
    if os.path.exists(os.path.join(HERE, "STOP")):
        print("检测到 STOP 文件，急停中，不做任何操作。")
        return

    global SYS_STATE, PENDING
    if cfg["mode"] != "paper":  # 不同账户的状态分开存，避免模拟盘和券商账户混在一起
        SYS_STATE = os.path.join(STATE_DIR, f"system_{cfg['mode']}.json")
        PENDING = os.path.join(STATE_DIR, f"pending_orders_{cfg['mode']}.json")
    mkt = engine.load_market()
    prices = engine.latest_prices(mkt)
    try:
        br = brokers.make(cfg, prices, mkt["last_day"])
    except Exception as e:  # noqa: BLE001
        msg = f"Guardian {today}：连接券商失败，今天没有任何操作。{type(e).__name__}: {e}"
        if cfg["mode"].startswith("ibkr"):
            msg += "\n请确认 IB Gateway 已打开并登录、API 端口与 config.json 里的 ibkr.port 一致。"
        print(msg)
        notify(msg)
        sys.exit(1)
    tcfg = dict(cfg, fractional_shares=cfg["fractional_shares"] and not br.whole_shares_only)
    st = load_json(SYS_STATE, {})
    events = list(config_notes)  # 配置被守门修正过的地方，每天都写进日报，直到改正

    settled = br.settle() if hasattr(br, "settle") else []
    if settled:
        br.save()
    nav = br.cash() + sum(q * prices[s] for s, q in br.positions().items())
    if not st:
        st = {"inception": today, "bench_px": {s: prices[s] for s in ("SPY", "IEF")},
              "hwm": nav, "breaker_on": False, "target": None, "last_signal_month": None,
              "nav_history": []}
    st["hwm"] = max(st["hwm"], nav)
    dd = nav / st["hwm"] - 1

    for x in settled:
        events.append(f"上次 {x['symbol']} {x['qty']:+g} 股按 {x['fill_day']} 开盘价 {x['fill_price']} 结算，"
                      f"现金调整 {x['cash_adjust']:+.2f}")
    problems = data_checks(mkt, cfg)
    if problems:
        events += ["数据异常，今日停止交易：" + p for p in problems]
        finish(cfg, st, br, mkt, prices, nav, dd, today, events, None, [], [], [], None)
        return

    # 回撤熔断：带回滞，跌破阈值触发，回撤收窄到阈值一半以内才解除
    g = cfg["guardrails"]
    recompute = "--rebalance" in sys.argv or st["last_signal_month"] != mkt["signal_month"]
    if not st["breaker_on"] and dd <= -g["drawdown_breaker"]:
        st["breaker_on"], recompute = True, True
        events.append(f"回撤 {dd:.1%} 触发熔断，风险资产降至 {g['breaker_risk_scale']:.0%}")
    elif st["breaker_on"] and dd > -g["drawdown_breaker"] / 2:
        st["breaker_on"], recompute = False, True
        events.append("回撤收窄，熔断解除")

    review, applied = None, []
    if "--approve" in sys.argv and os.path.exists(PENDING):
        orders = load_json(PENDING, [])
        os.remove(PENDING)
        events.append(f"已审批 {len(orders)} 笔待执行订单")
        fills = execute(orders, br, tcfg)
        br.save()
        nav = br.cash() + sum(q * prices[s] for s, q in br.positions().items())
        finish(cfg, st, br, mkt, prices, nav, dd, today, events, None, [], orders, fills, None)
        return

    # 1) 规则目标 = 月度部分（每月信号更新时重算）+ 按日部分（每天重算，例如波动率管理）
    st.setdefault("monthly_target", st.get("rule_target"))
    if recompute:
        monthly, per_engine = engine.targets(mkt, cfg["profiles"][cfg["profile"]], cfg["cash_symbol"])
        st["monthly_target"] = {s: round(w, 4) for s, w in monthly.items()}
        st["per_engine"] = per_engine
        st["last_signal_month"] = mkt["signal_month"]
        events.append(f"按信号月 {mkt['signal_month']} 重新计算月度目标")
    if cfg.get("monthly_tranches"):
        # 分批判定：月末、每月第 5/10/15 个交易日各判断四分之一，每天都可能有一批更新
        mt = strategies_daily.live_monthly_tranches(two=cfg.get("tranche_2x", 0.5))
        mt = {s: round(w, 4) for s, w in mt.items() if w > 1e-4}
        if st.get("monthly_target") and mt != st["monthly_target"]:
            events.append("分批月度信号有变化：" + json.dumps(mt, ensure_ascii=False))
        st["monthly_target"] = mt
        st.setdefault("per_engine", {})["monthly_tranches"] = mt
    rule = dict(st["monthly_target"] or {})
    ov = cfg.get("daily_overlay")
    if ov and rule:
        dw = strategies_daily.live_weights(ov["strategy"], **ov.get("params", {}))
        share = ov["share"]
        rule = {s: rule.get(s, 0) * (1 - share) + dw.get(s, 0) * share for s in set(rule) | set(dw)}
        st.setdefault("per_engine", {})["daily_" + ov["strategy"]] = {s: round(w, 4) for s, w in dw.items() if w > 1e-4}
    k = cfg.get("risk_scale", 1.0)
    if rule and k < 1:  # 稳健档：只拿一部分去跑策略，其余常年放短债
        cash = cfg["cash_symbol"]
        risk = {s: w * k for s, w in rule.items() if s != cash}
        rule = {**risk, cash: 1 - sum(risk.values())}
    if rule and st["breaker_on"]:
        cash = cfg["cash_symbol"]
        risk = {s: w * g["breaker_risk_scale"] for s, w in rule.items() if s != cash}
        rule = {**risk, cash: 1 - sum(risk.values())}
    if rule:
        st["rule_target"] = {s: round(w, 4) for s, w in rule.items() if w > 1e-4}

    # 2) AI 风控复核：每天在规则目标上重新给出削减（只减不加），风险消失后自动恢复到规则仓位
    snap = engine.snapshot(mkt, cfg["whitelist"])
    news = ai_review.headlines() if cfg["ai"].get("news", True) else []
    if st["rule_target"] and (recompute or cfg["ai"].get("daily", True)):
        account = {"nav_usd": round(nav, 2), "drawdown": round(dd, 4), "breaker_on": st["breaker_on"],
                   "current_weights": {s: round(w, 4) for s, w in weights_now(br, prices, nav).items()}}
        review = ai_review.review(cfg, st["rule_target"], st.get("per_engine"), snap, account, news)
        src = "，".join(ai_review.NEWS_SOURCES) or "全部新闻源都没取到"
        events.append(f"AI 复核：{review['status']}（新闻 {len(news)} 条：{src}）")
        if not news:
            events.append("警告：今天没取到任何新闻，AI 风控只能凭行情判断，看不到突发事件")
        if review["verdict"] == "hold":
            events.append("AI 不可用且配置为 hold：沿用上一次的目标")
        else:
            target, applied = ai_review.apply_caps(st["rule_target"], review["caps"], cfg["cash_symbol"])
            st["target"] = {s: round(w, 4) for s, w in target.items()}
    elif st["rule_target"] and not st.get("target"):
        st["target"] = st["rule_target"]

    orders, blocked, fills = [], [], []
    if st.get("target") and br.pending_orders():
        events.append(f"券商还有 {len(br.pending_orders())} 笔未成交订单，今天不再下新单")
    elif st.get("target"):
        tgt = trade_target(cfg, st["target"])
        orders, blocked = guard(make_orders(tgt, br, prices, nav, tcfg), tcfg, nav, prices)
        if br.sells_settle_later and any(o["qty"] < 0 for o in orders) and any(o["qty"] > 0 for o in orders):
            orders = [o for o in orders if o["qty"] < 0]
            events.append("先卖后买：今天只提交卖单，卖出资金到账后下一次运行再买入")
        if orders and cfg["execution"] == "approve":
            save_json(PENDING, orders)
            events.append(f"{len(orders)} 笔订单等待审批：python3 run.py --approve")
        elif orders:
            fills = execute(orders, br, tcfg)
    br.save()
    nav = br.cash() + sum(q * prices[s] for s, q in br.positions().items())

    extra = rule_only_counterfactual(st, prices, nav)
    if cfg["shadow"].get("enabled") and "--no-shadow" not in sys.argv:
        extra += run_shadow(cfg, st, today, prices, snap, news, nav, mkt["last_day"])
    if cfg.get("arena", {}).get("enabled"):
        extra += run_arena(cfg, st, mkt, prices, today, nav)
    key = ai_review.api_key()
    bal = ai_review.balance(key) if key else None
    if bal is not None:
        extra += ["", f"DeepSeek 余额：¥{bal:.2f}"]
        if bal < cfg["ai"]["low_balance_cny"]:
            events.append(f"DeepSeek 余额只剩 ¥{bal:.2f}，请充值")
    finish(cfg, st, br, mkt, prices, nav, dd, today, events, review, applied, orders, fills, blocked, extra)


def trade_target(cfg, target):
    """券商账户里把 SPY/QQQ 等换成单价更低的同指数 ETF（API 不支持零股），模拟盘不换。"""
    if not (cfg["mode"].startswith("ibkr") or cfg.get("paper_like_ibkr")):
        return target
    m = cfg["ibkr"].get("symbol_map", {})
    out = {}
    for s, w in target.items():
        out[m.get(s, s)] = out.get(m.get(s, s), 0) + w
    return out


def rule_only_counterfactual(st, prices, nav):
    """
    两个虚拟账户，用同一套记账方法（按目标权重、收盘价、不计成本）：
    - 纯规则：每天按规则目标持仓，完全不听 AI
    - 听 AI：每天按 AI 风控调整后的目标持仓
    两者之差才是 AI 风控自己的贡献，不会混进成交价、整股、手续费这些执行层面的差异。
    """
    ro = st.get("rule_only")
    if not ro or "ai_nav" not in ro:
        ro = {"nav": 1.0, "ai_nav": 1.0, "since": date.today().isoformat()}
    else:
        for key, wkey in (("nav", "weights"), ("ai_nav", "ai_weights")):
            w = ro.get(wkey, {})
            r = sum(x * (prices[s] / ro["px"][s] - 1) for s, x in w.items() if s in ro["px"])
            ro[key] *= 1 + r
    ro["weights"] = st.get("rule_target") or {}
    ro["ai_weights"] = st.get("target") or ro["weights"]
    ro["px"] = {s: prices[s] for s in set(ro["weights"]) | set(ro["ai_weights"])}
    st["rule_only"] = ro
    a, b = ro["nav"] - 1, ro["ai_nav"] - 1
    return ["", f"## AI 风控的效果（自 {ro['since']} 起）",
            f"- 完全按规则 {a:+.2%}，听 AI 调整后 {b:+.2%}，AI 风控贡献 {b - a:+.2%}"]


def run_arena(cfg, st, mkt, prices, today, main_nav):
    """
    策略擂台：每个策略一个独立虚拟账户（同样本金、次日开盘成交、同样成本和缓冲带，零股），
    每天各自按自己的信号调仓，日报里按收益排名。用来向前验证，而不只看回测。
    """
    ac = cfg["arena"]
    acfg = dict(cfg, fractional_shares=True, paper_capital_usd=ac["capital_usd"])
    h = strategies_daily.live_H()
    monthly, _ = engine.targets(mkt, {"trend_us_equity": 0.5, "lev_trend_us": 0.5}, cfg["cash_symbol"])
    vm = strategies_daily.live_weights("vol_managed", h)
    meta = st.setdefault("arena", {"start": today, "main_nav0": cfg["paper_capital_usd"], "accounts": {}})
    rows = []
    for key, name in ac["members"].items():
        if key == "monthly_growth":
            tgt = monthly
        elif key == "volmix":
            mt = strategies_daily.monthly_tranches(h, len(h.dates) - 1)
            tgt = {s: mt.get(s, 0) * 0.5 + vm.get(s, 0) * 0.5 for s in set(mt) | set(vm)}
        elif key.startswith("level_"):
            tgt = strategies_daily.level_strategy(key[len("level_"):])(h, len(h.dates) - 1)
        else:
            tgt = strategies_daily.live_weights(key, h)
        tgt = {s: w for s, w in tgt.items() if w > 1e-4}
        b = brokers.PaperBroker(acfg, prices, path=os.path.join(STATE_DIR, f"arena_{key}.json"),
                                market_day=mkt["last_day"])
        b.settle()
        nav = b.cash() + sum(q * prices[s] for s, q in b.positions().items())
        orders, _ = guard(make_orders(tgt, b, prices, nav, acfg), acfg, nav, prices)
        execute(orders, b, acfg)
        b.save()
        nav = b.cash() + sum(q * prices[s] for s, q in b.positions().items())
        a = meta["accounts"].setdefault(key, {"nav0": ac["capital_usd"], "hwm": nav, "mdd": 0.0})
        a["hwm"] = max(a["hwm"], nav)
        a["mdd"] = min(a["mdd"], nav / a["hwm"] - 1)
        hist = [x for x in a.get("history", []) if x[0] != today]
        a["history"] = hist + [[today, round(nav, 2)]]
        cur = weights_now(b, prices, nav)
        expo = cur.get("SPY", 0) + cur.get("QQQ", 0) + 2 * (cur.get("SSO", 0) + cur.get("QLD", 0))
        rows.append((nav / a["nav0"] - 1, name, a["mdd"], expo, len(orders)))
    rows.sort(reverse=True)
    lines = ["", f"## 策略擂台（自 {meta['start']} 起，各 ${ac['capital_usd']:,} 虚拟本金）",
             "| 名次 | 策略 | 收益 | 最大回撤 | 当前股票档位 | 今日下单 |", "|---:|---|---:|---:|---:|---:|"]
    for k, (r, name, mdd, expo, n) in enumerate(rows, 1):
        lines.append(f"| {k} | {name} | {r:+.2%} | {mdd:.2%} | {expo:.2f} 倍 | {n} |")
    lines.append(f"| — | 规则主账户（含 AI 风控、整股） | {main_nav / meta['main_nav0'] - 1:+.2%} | — | — | — |")
    return lines


def run_shadow(cfg, st, today, prices, snap, news, rules_nav, market_day):
    """AI 影子账户：DeepSeek 全权决定仓位，和规则账户同价同成本对比。返回日报段落。"""
    cfg = dict(cfg, fractional_shares=True)  # 影子账户比的是 AI 判断力，用零股避免整股误差
    sbr = brokers.PaperBroker(cfg, prices, path=shadow.STATE, market_day=market_day)
    if sbr.settle():
        sbr.save()
    snav = sbr.cash() + sum(q * prices[s] for s, q in sbr.positions().items())
    sst = st.setdefault("shadow", {"inception": today, "hwm": snav, "capital": snav})
    if "shadow_nav0" not in sst:  # 对比基准：两个账户从同一天起算收益
        sst.update(base_date=today, rules_nav0=rules_nav, shadow_nav0=snav)
    lines = ["", "## AI 影子账户（DeepSeek 全权操盘，对照实验）"]
    key = ai_review.api_key()
    if not key:
        return lines + ["- 跳过：没找到 DeepSeek key"]

    start = sst.get("capital", 10000)
    rules_ret = rules_nav / sst["rules_nav0"] - 1
    account = {"nav_usd": round(snav, 2), "return_since_inception": round(snav / start - 1, 4),
               "your_return_since_" + sst["base_date"]: round(snav / sst["shadow_nav0"] - 1, 4),
               "rules_account_return_since_" + sst["base_date"]: round(rules_ret, 4),
               "drawdown": round(snav / max(sst["hwm"], snav) - 1, 4),
               "current_weights": {s: round(w, 4) for s, w in weights_now(sbr, prices, snav).items()}}
    out, fills = None, []
    try:
        out, status = shadow.decide(cfg, key, today, st.get("target"), snap, news, account)
        lines.append(f"- 状态：{status}")
    except Exception as e:  # noqa: BLE001 —— 决策失败就维持原仓位
        lines.append(f"- 今日决策失败，维持原仓位：{type(e).__name__}: {e}")
    if out:
        target = shadow.sanitize(out.get("weights"), cfg)
        orders, _ = guard(make_orders(target, sbr, prices, snav, cfg), cfg, snav, prices)
        fills = execute(orders, sbr, cfg)
        sbr.save()
        snav = sbr.cash() + sum(q * prices[s] for s, q in sbr.positions().items())
        with open(shadow.JOURNAL, "a") as f:
            f.write(json.dumps({"date": today, "weights": target, "thesis": out.get("thesis"),
                                "vs_rules": out.get("vs_rules"), "confidence": out.get("confidence"),
                                "nav": round(snav, 2), "fills": fills}, ensure_ascii=False) + "\n")
    sst["hwm"] = max(sst["hwm"], snav)
    cur = weights_now(sbr, prices, snav)
    lines += [f"- 净值：${snav:,.2f}，自 {sst['inception']} 起 {snav / start - 1:+.2%}",
              f"- 同期对比（自 {sst['base_date']} 起）：影子账户 {snav / sst['shadow_nav0'] - 1:+.2%}，"
              f"规则账户 {rules_ret:+.2%}",
              f"- 距最高点回撤：{snav / sst['hwm'] - 1:.2%}",
              "- 持仓：" + "，".join(f"{s} {w:.1%}" for s, w in sorted(cur.items(), key=lambda x: -x[1]))]
    if out:
        lines += [f"- 判断：{out.get('thesis', '')}", f"- 与规则的差异：{out.get('vs_rules', '')}",
                  f"- 信心：{out.get('confidence')}"]
    if fills:
        lines.append("- 成交：" + "；".join(
            f"{'买' if x['qty'] > 0 else '卖'} {abs(x['qty']):g} {x['symbol']}" for x in fills))
    return lines


def finish(cfg, st, br, mkt, prices, nav, dd, today, events, review, applied, orders, fills, blocked,
           extra=()):
    hist = [h for h in st["nav_history"] if h[0] != today] + [[today, round(nav, 2)]]
    st["nav_history"] = hist
    save_json(SYS_STATE, st)
    b = st["bench_px"]
    spy = prices["SPY"] / b["SPY"] - 1
    sixty = 0.6 * spy + 0.4 * (prices["IEF"] / b["IEF"] - 1)
    start = cfg["paper_capital_usd"] if cfg["mode"] == "paper" else hist[0][1]
    cur = weights_now(br, prices, nav)
    target = trade_target(cfg, st.get("target") or {})
    label = {"paper": "本地模拟盘", "ibkr_paper": "IBKR 模拟账户", "ibkr_live": "IBKR 实盘"}[cfg["mode"]]
    if getattr(br, "dry_run", False):
        label += "，只读"

    lines = [f"# Guardian 日报 {today}（{label}）", "",
             f"- 净值：${nav:,.2f}，自 {st['inception']} 起 {nav / start - 1:+.2%}",
             f"- 同期对照：SPY {spy:+.2%}，60/40 {sixty:+.2%}",
             f"- 距最高点回撤：{dd:.2%}{'（熔断中）' if st['breaker_on'] else ''}",
             f"- 行情截至 {mkt['last_day']}，信号月 {st.get('last_signal_month')}", "",
             "| 标的 | 目标 | 实际 |", "|---|---:|---:|"]
    for s in sorted(set(target) | set(cur), key=lambda s: -target.get(s, 0)):
        lines.append(f"| {s} | {target.get(s, 0):.1%} | {cur.get(s, 0):.1%} |")
    if fills:
        lines += ["", "## 成交"] + [f"- {'买入' if f['qty'] > 0 else '卖出'} {abs(f['qty']):g} {f['symbol']} @ {f['price']}（${abs(f['value']):,.2f}）{('，' + f['status']) if f.get('status') else ''}" for f in fills]
    elif orders and cfg["execution"] == "approve":
        lines += ["", "## 待审批订单"] + [f"- {o['symbol']} {o['qty']:+g}（${o['value']:,.2f}）" for o in orders]
    if blocked:
        lines += ["", "## 风控拦截/截断"] + [f"- {o['symbol']} {o['qty']:+g}：{o['why']}" for o in blocked]
    if review:
        lines += ["", "## AI 复核", f"- 状态：{review['status']}，结论：{review['verdict']}"]
        lines += [f"- 下调 {a['symbol']} {a['from']:.1%} → {a['max_weight']:.1%}：{a['reason']}" for a in applied]
        if review.get("risk_notes"):
            lines.append(f"- 备注：{review['risk_notes']}")
        if review.get("log"):
            lines.append(f"- 原始记录（提示词、思考过程、原始回答）：`{review['log']}`")
    lines += list(extra)
    if events:
        lines += ["", "## 事件"] + [f"- {e}" for e in events]
    text = "\n".join(lines)

    os.makedirs(REPORTS, exist_ok=True)
    open(os.path.join(REPORTS, f"{today}.md"), "w").write(text + "\n")
    with open(JOURNAL, "a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "nav": round(nav, 2),
                            "dd": round(dd, 4), "events": events, "fills": fills,
                            "review": review, "blocked": blocked}, ensure_ascii=False) + "\n")
    print(text)
    if fills or events:
        notify(text)


def _watchdog(signum, frame):
    raise TimeoutError("运行超过 60 分钟仍未结束，强制退出（券商连接或 AI 调用卡住）")


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGALRM, _watchdog)
    signal.alarm(60 * 60)
    main()
