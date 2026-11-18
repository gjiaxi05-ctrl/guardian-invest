"""
按日运行的策略。思路：每天收盘后给出一个"风险档位" e（0～2），
e=0 全部短债 BIL，e=1 标普/纳指各半满仓，e=2 全部换成两倍 ETF（SSO/QLD）。
档位是连续的，不再是"满仓/空仓"二选一，所以能随市场状态每天细调。

每个策略 f(H, i) -> 档位 e。H 是对齐好的日线，i 是今天的下标，只能看 i 及以前的数据。
回测（backtest_flex.py）和实盘（run.py）用同一套函数。
参数一律取文献/业界常用值，没有为了回测好看去调。
"""
import math

SIGNAL_SYMBOLS = ["SPY", "QQQ", "SSO", "QLD", "BIL", "IEF", "HYG", "^VIX", "^VIX3M"]


class H:
    """多个品种按共同交易日对齐的收盘价，附带前缀和，便于快速算均线。"""

    def __init__(self, rows_by_sym):
        common = set.intersection(*(set(d for d, _ in rows) for rows in rows_by_sym.values()))
        self.dates = sorted(common)
        self.c = {}
        for s, rows in rows_by_sym.items():
            m = dict(rows)
            self.c[s] = [m[d] for d in self.dates]
        self._cum = {}

    def sma(self, s, n, i):
        if i + 1 < n:
            return None
        if s not in self._cum:
            acc, out = 0.0, [0.0]
            for x in self.c[s]:
                acc += x
                out.append(acc)
            self._cum[s] = out
        cum = self._cum[s]
        return (cum[i + 1] - cum[i + 1 - n]) / n

    def ret(self, s, n, i):
        return self.c[s][i] / self.c[s][i - n] - 1 if i >= n else None

    def basket_vol(self, n, i):
        """标普/纳指各半组合的 n 日年化波动率。"""
        if i < n + 1:
            return None
        rs = []
        for k in range(i - n + 1, i + 1):
            r = 0.5 * (self.c["SPY"][k] / self.c["SPY"][k - 1] - 1) + 0.5 * (self.c["QQQ"][k] / self.c["QQQ"][k - 1] - 1)
            rs.append(r)
        mean = sum(rs) / n
        return math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1) * 252)

    def rsi2(self, s, i):
        if i < 3:
            return None
        ups = downs = 0.0
        for k in (i - 1, i):
            d = self.c[s][k] - self.c[s][k - 1]
            ups += max(d, 0)
            downs += max(-d, 0)
        return 100.0 if downs == 0 else 100 - 100 / (1 + ups / downs)


def weights(e):
    """把档位 e 换成目标权重。e≤1 用普通 ETF，e>1 的部分用两倍 ETF 补足。"""
    e = max(0.0, min(2.0, e))
    if e <= 1:
        w = {"SPY": e / 2, "QQQ": e / 2}
    else:
        x = e - 1  # 两倍 ETF 占比
        w = {"SPY": (1 - x) / 2, "QQQ": (1 - x) / 2, "SSO": x / 2, "QLD": x / 2}
    w = {s: v for s, v in w.items() if v > 1e-6}
    w["BIL"] = max(0.0, 1 - sum(w.values()))
    return w


# ---------------- 各个策略 ----------------

def _trend_votes(H, i, ns=(50, 100, 200)):
    votes = [H.c[s][i] > H.sma(s, n, i) for s in ("SPY", "QQQ") for n in ns if H.sma(s, n, i)]
    return sum(votes) / len(votes) if votes else 0.0


def vol_managed(H, i, tv=0.20, cap=2.0):
    """波动率管理（Moreira & Muir 2017）：市场越平静越加仓，波动一放大立刻减仓。
    tv 是目标年化波动，cap 是最高档位（1 = 不用两倍 ETF）。"""
    v = H.basket_vol(20, i)
    return min(cap, tv / v) if v else 0.0


def trend_vol(H, i, tv=0.20):
    """多周期趋势投票 × 波动率管理：趋势决定方向，波动决定力度。"""
    v = H.basket_vol(20, i)
    return _trend_votes(H, i) * min(2.0, tv / v) if v else 0.0


def vix_term(H, i):
    """VIX 期限结构：短期恐慌低于三个月预期（正常）就进攻，倒挂（恐慌）就撤退。"""
    r = H.c["^VIX"][i] / H.c["^VIX3M"][i]
    return 2.0 if r < 0.9 else 1.0 if r < 1.0 else 0.0


def credit_canary(H, i):
    """信用债金丝雀：高收益债 3 个月跑输国债，说明资金在逃离风险，撤退。"""
    a, b = H.ret("HYG", 63, i), H.ret("IEF", 63, i)
    return 1.5 if a is not None and a > b else 0.0


def dashboard(H, i, tv=0.20):
    """多信号仪表盘：长期趋势、短期趋势、VIX 期限结构、信用金丝雀四票，再乘波动率管理。"""
    if H.sma("SPY", 200, i) is None:
        return 0.0
    sigs = [
        (H.c["SPY"][i] > H.sma("SPY", 200, i)) + (H.c["QQQ"][i] > H.sma("QQQ", 200, i)),
        (H.c["SPY"][i] > H.sma("SPY", 50, i)) + (H.c["QQQ"][i] > H.sma("QQQ", 50, i)),
        2 * (H.c["^VIX"][i] < H.c["^VIX3M"][i]),
        2 * ((H.ret("HYG", 63, i) or 0) > (H.ret("IEF", 63, i) or 0)),
    ]
    score = sum(sigs) / 8
    v = H.basket_vol(20, i)
    return score * min(2.0, tv / v) if v else 0.0


def dip_buyer(H, i):
    """趋势中抄短线（Connors RSI2）：大趋势向上时满仓，连跌两天超卖就临时加到两倍，反弹站上 5 日线就恢复。"""
    if H.sma("SPY", 200, i) is None or H.c["SPY"][i] < H.sma("SPY", 200, i):
        return 0.0
    if H.rsi2("SPY", i) < 10 or H.c["SPY"][i] < H.sma("SPY", 5, i) and H.rsi2("SPY", i - 1) < 10:
        return 2.0
    return 1.0


def turn_of_month(H, i, days_in_month_pos=None):
    """月初效应：趋势向上时满仓，每月最后 1 天到下月前 3 天加到两倍（历史上这几天收益最集中）。"""
    if H.sma("SPY", 200, i) is None or H.c["SPY"][i] < H.sma("SPY", 200, i):
        return 0.0
    d = H.dates
    # 明天是否落在"月末最后一天 ~ 月初第 3 天"窗口（决定今天收盘后的档位，明天开盘执行）
    if i + 1 >= len(d):
        return 1.0
    nxt = i + 1
    first_idx = nxt
    while first_idx > 0 and d[first_idx - 1][:7] == d[nxt][:7]:
        first_idx -= 1
    pos_in_month = nxt - first_idx  # 0 = 当月第一个交易日
    is_last_day = nxt + 1 < len(d) and d[nxt + 1][:7] != d[nxt][:7]
    return 2.0 if pos_in_month <= 2 or is_last_day else 1.0


def monthly_growth(H, i):
    """对照组：现行成长档。月末看 10 个月均线；标普在上方就 SPY+SSO 各 25%，纳指同理；否则那一半放 BIL。"""
    d = H.dates
    if not (i + 1 >= len(d) or d[i + 1][:7] != d[i][:7]):
        return None  # 月中不变
    ends = [k for k in range(i + 1) if k + 1 >= len(d) or d[k + 1][:7] != d[k][:7]][-10:]
    if len(ends) < 10:
        return {"BIL": 1.0}
    w = {}
    for sig, one, two in (("SPY", "SPY", "SSO"), ("QQQ", "QQQ", "QLD")):
        px = [H.c[sig][k] for k in ends]
        if px[-1] > sum(px) / 10:
            w[one], w[two] = 0.25, 0.25
        else:
            w["BIL"] = w.get("BIL", 0) + 0.5
    return w


STRATEGIES = {
    "旧主策略：月度成长档": monthly_growth,
    "波动率管理": vol_managed,
    "趋势投票×波动率": trend_vol,
    "VIX期限结构": vix_term,
    "信用债金丝雀": credit_canary,
    "多信号仪表盘": dashboard,
    "趋势中抄短线RSI2": dip_buyer,
    "月初效应": turn_of_month,
}


# ---------------- 组合：方向慢判断，仓位快调整 ----------------

def _month_end_idx(H):
    if not hasattr(H, "_me"):
        d = H.dates
        H._me = [k for k in range(len(d)) if k + 1 < len(d) and d[k + 1][:7] != d[k][:7]]
    return H._me


def _monthly_on(H, sig, i):
    """最近一个已完成月末：价格是否在 10 个月均线上方（月中保持不变）。"""
    import bisect
    me = _month_end_idx(H)
    k = bisect.bisect_right(me, i) - 1
    if k < 9:
        return False
    px = [H.c[sig][j] for j in me[k - 9:k + 1]]
    return px[-1] > sum(px) / 10


def _half(e, one, two):
    """半个组合（标普或纳指）按档位 e 拆成 1 倍和 2 倍 ETF。"""
    e = max(0.0, min(2.0, e))
    if e <= 1:
        return {one: e / 2}
    x = e - 1
    return {one: (1 - x) / 2, two: x / 2}


def trend_x_vol(H, i, tv=0.20, cap=2.0, n=20):
    """趋势定方向（每月）× 波动定仓位（每周）：趋势在才持有，持有多少看最近波动，市场越平静越敢加。"""
    v = H.basket_vol(n, i)
    if not v:
        return {"BIL": 1.0}
    size = min(cap, tv / v)
    w = {}
    for sig, one, two in (("SPY", "SPY", "SSO"), ("QQQ", "QQQ", "QLD")):
        if _monthly_on(H, sig, i):
            for s, x in _half(size, one, two).items():
                w[s] = w.get(s, 0) + x
    w["BIL"] = max(0.0, 1 - sum(w.values()))
    return w


def blend(*parts):
    """多个策略按比例合成（权重相加）。"""
    def f(H, i):
        out = {}
        for fn, share in parts:
            r = fn(H, i)
            if r is None:
                r = getattr(f, "_last", {}).get(fn.__name__, {"BIL": 1.0})
            f._last = getattr(f, "_last", {})
            f._last[fn.__name__] = r
            w = r if isinstance(r, dict) else weights(r)
            for s, x in w.items():
                out[s] = out.get(s, 0) + x * share
        return out
    return f


COMBOS = {
    "趋势×波动(每月方向,每周仓位)": trend_x_vol,
    "50%月度成长+50%波动率管理": blend((monthly_growth, 0.5), (vol_managed, 0.5)),
    "50%趋势×波动+50%仪表盘": blend((trend_x_vol, 0.5), (dashboard, 0.5)),
}


# ---------------- 实盘接口 ----------------

STRAT_BY_KEY = {
    "vol_managed": vol_managed,
    "trend_x_vol": trend_x_vol,
    "dashboard": dashboard,
    "vix_term": vix_term,
    "dip_buyer": dip_buyer,
}


def live_H():
    """用最新已收盘数据构造 H（实盘每天调用一次）。"""
    import data
    return H({s: data.daily(s) for s in SIGNAL_SYMBOLS})


def live_weights(key, h=None, **params):
    """某个按日策略今天收盘后的目标权重。params 传给策略函数（如 vol_managed 的 tv、cap）。"""
    h = h or live_H()
    out = STRAT_BY_KEY[key](h, len(h.dates) - 1, **params)
    return out if isinstance(out, dict) else weights(out)


# ---------------- 分批判定的月度趋势（消除"判定日运气"） ----------------
# 只在月末一天判断，结果很依赖那一天碰巧是涨是跌（回测里换个判定日，最大回撤能从 -33% 变成 -47%）。
# 分成 4 批，分别在月末、每月第 5、10、15 个交易日判断，各管四分之一，不把结果押在某一天。

TRANCHE_DAYS = (-1, 4, 9, 14)


def _months(H):
    if not hasattr(H, "_months"):
        m = {}
        for k, x in enumerate(H.dates):
            m.setdefault(x[:7], []).append(k)
        H._months = m
    return H._months


def _last_weekday_of_month(day):
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n.month != d.month


def _check_days(H, k, i):
    """第 k 批截至今天（含）的所有判定日下标。k=-1 表示月末。"""
    cur, live_end = H.dates[i][:7], i == len(H.dates) - 1
    out = []
    for mon, v in _months(H).items():
        if mon > cur:
            break
        if k >= 0:
            if len(v) > k and v[k] <= i:
                out.append(v[k])
        elif mon < cur:
            out.append(v[-1])
        elif v[-1] == i and (not live_end or _last_weekday_of_month(H.dates[i])):
            out.append(i)  # 实盘里今天是不是月末只能看日历（遇月末假日会晚一天，无碍）
    return out


def monthly_tranches(H, i, days=TRANCHE_DAYS, two=0.5):
    """two 是趋势在时放进两倍 ETF 的比例：0 = 只买普通 ETF，0.5 = 各半（成长档），1 = 全部两倍。"""
    w = {}
    for k in days:
        cd = _check_days(H, k, i)
        for sig, one, lev in (("SPY", "SPY", "SSO"), ("QQQ", "QQQ", "QLD")):
            px = [H.c[sig][j] for j in cd[-10:]]
            on = len(px) == 10 and px[-1] > sum(px) / 10
            share = 0.5 / len(days)
            if on:
                w[one] = w.get(one, 0) + share * (1 - two)
                w[lev] = w.get(lev, 0) + share * two
            else:
                w["BIL"] = w.get("BIL", 0) + share
    return w


COMBOS["现主策略：分批月度50%+波动率50%"] = blend((monthly_tranches, 0.5), (vol_managed, 0.5))
COMBOS["分批月度（不含波动率）"] = monthly_tranches


def live_monthly_tranches(h=None, two=0.5):
    h = h or live_H()
    w = monthly_tranches(h, len(h.dates) - 1, two=two)
    return {s: x for s, x in w.items() if x > 1e-9}


# ---------------- 风险档位：同一套机制，一个旋钮 ----------------
# 每一档都是"50% 分批月度趋势 + 50% 波动率管理"，只改四个参数：
#   two    趋势在时放进两倍 ETF 的比例
#   tv     波动率管理的目标年化波动
#   cap    波动率管理的最高档位（1 = 不用两倍 ETF）
#   scale  整个组合只拿这么多去投，其余常年放短债 BIL
# 参数按"风险一档比一档高"事先定好，不按回测结果挑选；回测数字见 backtest_flex.py --levels。
# 唯一一次修改：稳健档最初用 tv=0.10、不留现金，回测最大回撤 -22%，和平衡档（-21%）几乎一样，
# 因为慢熊市里波动率不高、波动率管理不会减仓。改成"平衡档 × 60% + 40% 短债"，降风险靠常备现金。
RISK_LEVELS = {
    "conservative": {"name": "稳健", "name_en": "Conservative", "two": 0.0, "tv": 0.15, "cap": 1.0, "scale": 0.6,
                     "desc": "平衡档的 60% + 常备 40% 短债，只用普通 ETF"},
    "balanced": {"name": "平衡", "name_en": "Balanced", "two": 0.0, "tv": 0.15, "cap": 1.0,
                 "desc": "只用普通 ETF，最多满仓，不加杠杆"},
    "growth": {"name": "成长", "name_en": "Growth", "two": 0.5, "tv": 0.20, "cap": 2.0,
               "desc": "作者自己在跑的档位：趋势在时一半用两倍 ETF，平静时最多 2 倍档位"},
    "aggressive": {"name": "激进", "name_en": "Aggressive", "two": 1.0, "tv": 0.25, "cap": 2.0,
                   "desc": "趋势在时全部用两倍 ETF，波动目标 25%；回撤可能超过一半"},
}


def level_strategy(key):
    """某个风险档位的按日策略函数（回测和看板用；实盘由 run.py 按同样参数组合）。"""
    lv = RISK_LEVELS[key]

    def trend_part(H, i):
        return monthly_tranches(H, i, two=lv["two"])

    def vol_part(H, i):
        return vol_managed(H, i, tv=lv["tv"], cap=lv["cap"])

    f = blend((trend_part, 0.5), (vol_part, 0.5))
    return f if lv.get("scale", 1.0) == 1.0 else scaled(f, lv["scale"])


def scaled(f, k):
    """只拿 k 比例去跑策略 f，其余放短债 BIL。"""
    def g(H, i):
        w = f(H, i)
        out = {s: x * k for s, x in w.items() if s != "BIL"}
        out["BIL"] = 1 - sum(out.values())
        return out
    return g
