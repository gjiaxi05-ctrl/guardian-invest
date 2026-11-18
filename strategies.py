"""
策略库。每个策略是函数 f(ctx) -> {代码: 权重}，权重和 <= 1，余下自动放现金。
ctx 只能看到"截至当月月末"的数据，回测和实盘共用同一套代码，避免回测作弊。
"""
import math
import statistics

import data

CASH = "CASH"  # 合成现金：BIL（1-3 月美债）上市前用 SHY 代替


class Ctx:
    """某个月末时点的数据视图。"""

    def __init__(self, months, mpx, dpx, i):
        self.months, self.mpx, self.dpx, self.i = months, mpx, dpx, i
        self.month = months[i]

    def px(self, sym, back=0):
        return self.mpx[sym].get(self.months[self.i - back])

    def ret(self, sym, n):
        a, b = self.px(sym), self.px(sym, n)
        return a / b - 1 if a and b else None

    def sma(self, sym, n):
        vals = [self.px(sym, k) for k in range(n)]
        return sum(vals) / n if all(vals) else None

    def mom13612(self, sym):
        rs = [self.ret(sym, n) for n in (1, 3, 6, 12)]
        return sum(rs) / 4 if None not in rs else None

    def daily_returns(self, sym, days=63):
        """截至当月最后一个交易日的最近 days 个日收益。"""
        rows = [p for d, p in self.dpx[sym] if d[:7] <= self.month]
        rows = rows[-(days + 1):]
        return [rows[k + 1] / rows[k] - 1 for k in range(len(rows) - 1)]

    def vol(self, sym, days=63):
        r = self.daily_returns(sym, days)
        return statistics.stdev(r) * math.sqrt(252) if len(r) > 20 else None

    def port_vol(self, weights, days=63):
        """用历史日收益直接算组合波动（自带相关性），不估协方差矩阵。"""
        syms = [s for s, w in weights.items() if w > 0 and s != CASH]
        if not syms:
            return 0.0
        series = {s: self.daily_returns(s, days) for s in syms}
        n = min(len(v) for v in series.values())
        port = [sum(weights[s] * series[s][-n + k] for s in syms) for k in range(n)]
        return statistics.stdev(port) * math.sqrt(252) if n > 20 else None


# ---------------- 对照组 ----------------

def buy_hold_spy(ctx):
    return {"SPY": 1.0}


def sixty_forty(ctx):
    return {"SPY": 0.6, "IEF": 0.4}


# ---------------- 公开的经典防守型策略 ----------------

def faber_gtaa5(ctx):
    """Meb Faber 2006：五类资产各 20%，跌破 10 个月均线就换成现金。"""
    out = {}
    for s in ("SPY", "EFA", "IEF", "VNQ", "DBC"):
        sma = ctx.sma(s, 10)
        if sma and ctx.px(s) > sma:
            out[s] = 0.2
    return out


def dual_momentum_gem(ctx):
    """Antonacci 2014：美股跑赢现金就持有美股/海外股中更强的，否则全仓债券。"""
    if ctx.ret("SPY", 12) > ctx.ret(CASH, 12):
        return {max(("SPY", "EFA"), key=lambda s: ctx.ret(s, 12)): 1.0}
    return {"AGG": 1.0}


def keller_haa(ctx):
    """Keller & Keuning 2023 HAA-Balanced：TIP 动量为金丝雀，翻负就全部防守。"""
    risk = ("SPY", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT")
    safe = max((CASH, "IEF"), key=ctx.mom13612)
    if ctx.mom13612("TIP") <= 0:
        return {safe: 1.0}
    top = sorted(risk, key=ctx.mom13612, reverse=True)[:4]
    out = {}
    for s in top:
        k = s if ctx.mom13612(s) > 0 else safe
        out[k] = out.get(k, 0) + 0.25
    return out


def trend_us_equity(ctx):
    """进攻仓：SPY/QQQ 各半，各自站上 10 个月均线才持有，否则换成 IEF/现金中较强者。"""
    safe = max((CASH, "IEF"), key=ctx.mom13612)
    out = {}
    for s in ("SPY", "QQQ"):
        k = s if ctx.px(s) > ctx.sma(s, 10) else safe
        out[k] = out.get(k, 0) + 0.5
    return out


def lev_trend_us(ctx):
    """两倍进攻仓：标普/纳指各半，信号看 SPY/QQQ 本身是否站上 10 个月均线，
    在上方就持有对应的两倍 ETF（SSO/QLD），否则换成 IEF/现金中较强者。
    两倍 ETF 不借钱、没有爆仓，但波动和回撤约是普通 ETF 的两倍，震荡市有损耗。"""
    safe = max((CASH, "IEF"), key=ctx.mom13612)
    out = {}
    for sig, lev in (("SPY", "SSO"), ("QQQ", "QLD")):
        k = lev if ctx.px(sig) > ctx.sma(sig, 10) else safe
        out[k] = out.get(k, 0) + 0.5
    return out


# ---------------- 自研：Guardian ----------------

GUARDIAN_UNIVERSE = ("SPY", "QQQ", "EFA", "EEM", "VNQ", "DBC", "GLD", "IEF", "TLT", "TIP", "LQD")


def guardian(ctx, lookbacks=(2, 4, 6, 8, 10, 12), target_vol=0.08, max_boost=2.0,
             universe=GUARDIAN_UNIVERSE):
    """
    设计目标是"输面最小"，不是收益最大：
    1. 趋势集成：每个资产在 6 个回看期上分别投票（价格>均线、收益>现金），
       得分 0~1。不押某一个参数，降低过拟合。
    2. 风险平价：按波动率倒数分配，债券和股票各自贡献相近的风险。
    3. 没通过投票的资产让出的仓位，按比例分给仍在趋势里的资产，
       但每个最多放大到 max_boost 倍（1.0 = 不再分配，全部留现金）。
    4. 波动率目标：组合历史波动超过 target_vol 就整体降仓；永不加杠杆。
    """
    inv_vol = {}
    for s in universe:
        v = ctx.vol(s)
        if v and ctx.px(s, max(lookbacks)):
            inv_vol[s] = 1 / v
    if not inv_vol:
        return {}
    total = sum(inv_vol.values())
    weights = {}
    for s, iv in inv_vol.items():
        votes = []
        for n in lookbacks:
            votes.append(ctx.px(s) > ctx.sma(s, n))
            votes.append(ctx.ret(s, n) > ctx.ret(CASH, n))
        score = sum(votes) / len(votes)
        if score > 0:
            weights[s] = score * iv / total
    gross = sum(weights.values())
    if gross > 0:
        weights = {s: w * min(max_boost, 1 / gross) for s, w in weights.items()}
    pv = ctx.port_vol(weights)
    if pv and pv > target_vol:
        weights = {s: w * target_vol / pv for s, w in weights.items()}
    gross = sum(weights.values())
    if gross > 1:
        weights = {s: w / gross for s, w in weights.items()}
    return weights


STRATEGIES = {
    "SPY 满仓": buy_hold_spy,
    "60/40 股债": sixty_forty,
    "Faber GTAA5": faber_gtaa5,
    "双动量 GEM": dual_momentum_gem,
    "Keller HAA": keller_haa,
    "Guardian": guardian,
    "趋势美股": trend_us_equity,
}

ENGINES = {"guardian": guardian, "keller_haa": keller_haa, "trend_us_equity": trend_us_equity,
           "lev_trend_us": lev_trend_us}


def blend(engine_weights):
    """把多个引擎按权重合成一个策略函数（回测用）。"""
    def f(ctx):
        out = {}
        for name, ew in engine_weights.items():
            for s, x in ENGINES[name](ctx).items():
                out[s] = out.get(s, 0) + x * ew
        return out
    return f


def load(symbols):
    """下载并对齐月度/日度数据，生成合成现金 CASH。"""
    mpx = {s: data.monthly(s) for s in symbols}
    dpx = {s: data.daily(s) for s in symbols}
    bil, shy = mpx["BIL"], mpx["SHY"]
    months = sorted(shy)
    cash, level = {}, 1.0
    for a, b in zip(months, months[1:]):
        src = bil if a in bil and b in bil else shy
        level *= src[b] / src[a]
        cash[b] = level
    cash[months[0]] = 1.0
    mpx[CASH] = cash
    return mpx, dpx
