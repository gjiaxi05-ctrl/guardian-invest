"""行情数据：Yahoo 复权日线（含分红再投资），本地缓存，只用标准库。"""
import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
UA = {"User-Agent": "Mozilla/5.0"}


def _closed_only(rows):
    """美股盘中运行时，去掉今天还没收盘的那根 K 线，只用已收盘的数据。"""
    ny = datetime.now(ZoneInfo("America/New_York"))
    if rows and rows[-1][0] == ny.strftime("%Y-%m-%d") and (ny.hour, ny.minute) < (16, 30):
        return rows[:-1]
    return rows


def _last_close_ts():
    """最近一次已经发生的美股收盘（纽约 16:30，按工作日算，节假日会多下载一次，无碍）的时间戳。"""
    from datetime import timedelta
    ny = datetime.now(ZoneInfo("America/New_York"))
    t = ny.replace(hour=16, minute=30, second=0, microsecond=0)
    if ny < t:
        t -= timedelta(days=1)
    while t.weekday() >= 5:
        t -= timedelta(days=1)
    return t.timestamp()


def daily(symbol, max_age_hours=12):
    """返回 [(YYYY-MM-DD, 复权收盘价), ...]，按日期升序，只含已收盘的交易日。"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{symbol}.json")
    # 缓存必须晚于最近一次收盘才算新鲜（2026-11-18 发现：前一晚美股开盘前刷新过缓存，
    # 第二天早上只看"不到 12 小时"就沿用了，漏掉了前一天的收盘）
    if (os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_hours * 3600
            and os.path.getmtime(path) >= _last_close_ts()):
        return _closed_only([tuple(r) for r in json.load(open(path))])
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?period1=0&period2={int(time.time())}&interval=1d&events=div,split&includeAdjustedClose=true")
    req = urllib.request.Request(url, headers=UA)
    # 刚从睡眠唤醒时网络常常还没就绪，重试几次；仍失败就抛错，不用过期缓存凑数
    for attempt, wait in enumerate((5, 20, 60, None)):
        try:
            res = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
            break
        except OSError:
            if wait is None:
                raise
            time.sleep(wait)
    ts, adj = res["timestamp"], res["indicators"]["adjclose"][0]["adjclose"]
    q = res["indicators"]["quote"][0]
    day = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
    rows = [(day(t), round(p, 6)) for t, p in zip(ts, adj) if p is not None]
    # 复权开盘价 = 开盘价 × 复权系数（复权收盘/收盘），和复权收盘价口径一致，模拟盘按它成交
    opens = [(day(t), round(o * a / c, 6)) for t, o, c, a in zip(ts, q["open"], q["close"], adj)
             if None not in (o, c, a) and c]
    json.dump(rows, open(path, "w"))
    json.dump(opens, open(os.path.join(CACHE, f"{symbol}.open.json"), "w"))
    return _closed_only(rows)


def first_open_after(symbol, day):
    """day 之后第一个交易日的 (日期, 复权开盘价)；还没有这样的交易日就返回 None。"""
    path = os.path.join(CACHE, f"{symbol}.open.json")
    if not os.path.exists(path):
        daily(symbol, max_age_hours=0)
    for d, p in json.load(open(path)):
        if d > day:
            return d, p
    return None


def monthly(symbol):
    """月末复权收盘价 {YYYY-MM: price}。当月未结束的最后一个月也包含在内（实盘用）。"""
    out = {}
    for d, p in daily(symbol):
        out[d[:7]] = p
    return out
