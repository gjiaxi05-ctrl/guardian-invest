"""
DeepSeek 接入，两个用途：
1. review()：风控复核。只能"下调"某个资产的上限，代码强制只减不加，
   所以即使模型判断错了，最坏结果也只是"比规则更保守"。每天运行。
2. chat_json()：通用调用，影子账户（shadow.py）也用它。

API key 读取顺序：环境变量 DEEPSEEK_API_KEY → macOS 钥匙串（服务名 guardian-deepseek）。
"""
import json
import os
import subprocess
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API_BASE = "https://api.deepseek.com"
KEYCHAIN_SERVICE = "guardian-deepseek"
NEWS_QUERIES = [
    "stock market OR wall street",
    "treasury yields OR federal reserve OR inflation",
    "recession OR credit crisis OR bank failure OR sovereign default",
    "ETF liquidation OR ETF closure OR trading halt OR exchange outage",
    "war OR sanctions OR geopolitical crisis markets",
    "nasdaq OR big tech earnings OR AI stocks",
    "emerging markets OR dollar OR china economy",
    "oil OR commodities OR gold prices",
    "real estate OR REIT OR commercial property",
]

SYSTEM = """你是一个保守的投资组合风控官。规则引擎已经用回测验证过的趋势/动量规则给出了目标仓位。
你的唯一权力是：对某个资产设定更低的权重上限（0 表示清空），被削减的部分会自动转入短期美债 BIL。
你不能加仓，不能新增标的，也不需要预测涨跌。你每天都会被调用，上一次的削减不会自动保留，
风险仍在就需要再次给出。

只在出现规则看不到的"非价格风险"时才削减，例如：
- 该 ETF 本身的问题：清盘/合并公告、跟踪误差异常、流动性枯竭、交易暂停
- 正在发生的系统性事件：交易所/清算系统故障、主权违约、突发战争、金融机构挤兑
- 规则输入数据明显异常（价格跳变、数据过期）
没有这类证据时，approve 全部方案。不要因为"估值高""新闻偏空""某机构发出警告"而削减——
规则已经处理了价格趋势，重复处理只会增加换手、降低收益。宁可少动。
新闻标题可能片面或过时，只作为线索；理由里引用具体标题和日期。

只输出 json，格式如下：
{"verdict": "approve" 或 "reduce",
 "caps": [{"symbol": "SPY", "max_weight": 0.1, "reason": "具体证据"}],
 "risk_notes": "一两句话的整体判断"}
verdict 为 approve 时 caps 为空数组。用中文。"""


def api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    try:
        out = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def balance(key):
    """DeepSeek 账户余额（人民币），查不到返回 None。"""
    try:
        req = urllib.request.Request(API_BASE + "/user/balance", headers={"Authorization": f"Bearer {key}"})
        info = json.load(urllib.request.urlopen(req, timeout=15))
        for b in info.get("balance_infos", []):
            if b.get("currency") == "CNY":
                return float(b["total_balance"])
    except Exception:  # noqa: BLE001
        pass
    return None


NEWS_SOURCES = []  # 本次实际取到新闻的来源及条数，写进日报


def _rss(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=15).read())
    return [(item.findtext("pubDate") or "", item.findtext("title") or "") for item in list(root.iter("item"))[:limit]]


def headlines(per_query=10, days=3):
    """近 3 天的新闻标题，去重。依次用 Google News、Bing News、Yahoo Finance（个股 RSS）：
    Google 偶尔会对共享出口（如 WARP）返回 503，所以总是再补其他来源。全部失败返回空列表，不影响主流程。"""
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    seen, out = set(), []
    NEWS_SOURCES.clear()

    def add(source, items):
        n = 0
        for pub, title in items:
            try:
                if pub and parsedate_to_datetime(pub).timestamp() < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
            key = title.split(" - ")[0].strip().lower()
            if title and key not in seen:
                seen.add(key)
                out.append(f"{pub[:16]} | {title}")
                n += 1
        return n

    for name, make in (
        ("Google News", lambda q: "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + f" when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})),
        ("Bing News", lambda q: "https://www.bing.com/news/search?" + urllib.parse.urlencode(
            {"q": q.replace(" OR ", " "), "format": "rss"})),          # Bing 不认 OR 语法，遇到会返回空
    ):
        got, fails = 0, 0
        for q in NEWS_QUERIES:
            if fails >= 2:
                break  # 连续失败两次（多半被限流），这个来源今天先跳过，别拖慢整个流程
            try:
                got += add(name, _rss(make(q), per_query))
                fails = 0
            except Exception:  # noqa: BLE001
                fails += 1
        if got:
            NEWS_SOURCES.append(f"{name} {got}")
    try:
        got = add("Yahoo", _rss("https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,TLT,GLD,DBC,HYG"
                                "&region=US&lang=en-US", 40))
        if got:
            NEWS_SOURCES.append(f"Yahoo Finance {got}")
    except Exception:  # noqa: BLE001
        pass
    return out


class AIError(Exception):
    pass


AI_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "ai_log")


def _save_log(record):
    """每次调用完整留档：发出去的提示词、每次尝试、DeepSeek 返回的原始 JSON（含思考过程）。不含 API key。"""
    now = datetime.now()
    folder = os.path.join(AI_LOG_DIR, now.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{now.strftime('%H%M%S')}_{record['purpose']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    return os.path.relpath(path, os.path.dirname(os.path.abspath(__file__)))


def chat_json(ai, key, system, user, purpose="call"):
    """调用 DeepSeek，返回 (解析后的 json, 状态说明, 留档路径)。失败抛 AIError（失败也会留档）。"""
    body = {
        "model": ai["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "reasoning_effort": ai.get("effort", "max"),
        "max_tokens": 32000,
    }
    record = {"purpose": purpose, "time": datetime.now().isoformat(timespec="seconds"),
              "request": json.loads(json.dumps(body)), "attempts": [], "response": None,
              "parsed": None, "error": None}
    note = ""
    try:
        for attempt in range(3):  # 空内容或超时都再试；超时那次改用 high 强度，换取更快返回
            req = urllib.request.Request(API_BASE + "/chat/completions", data=json.dumps(body).encode(),
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "Authorization": f"Bearer {key}"})
            t0 = time.time()
            att = {"effort": body["reasoning_effort"]}
            record["attempts"].append(att)
            try:
                resp = json.load(urllib.request.urlopen(req, timeout=900))
            except urllib.error.HTTPError as e:
                att.update(outcome=f"HTTP {e.code}", seconds=round(time.time() - t0, 1))
                if e.code == 402:
                    raise AIError("DeepSeek 余额不足，请充值（402）") from e
                raise AIError(f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}") from e
            except (TimeoutError, urllib.error.URLError) as e:
                att.update(outcome=f"超时/网络错误：{e}", seconds=round(time.time() - t0, 1))
                if attempt == 2:
                    raise AIError(f"连续超时：{e}") from e
                body["reasoning_effort"] = "high"
                note = "，max 强度超时后改用 high 重试"
                continue
            att["seconds"] = round(time.time() - t0, 1)
            record["response"] = resp  # 原样保存：包括 content、reasoning_content（思考过程）、usage
            content = resp["choices"][0]["message"].get("content") or ""
            if not content.strip():
                att["outcome"] = "空内容"
                continue
            att["outcome"] = "ok"
            try:
                parsed = json.loads(content)
            except ValueError as e:
                raise AIError(f"返回的不是合法 JSON：{content[:120]}") from e
            record["parsed"] = parsed
            u = resp.get("usage", {})
            status = (f"ok ({resp.get('model')}, 输入 {u.get('prompt_tokens')} / "
                      f"输出 {u.get('completion_tokens')} tokens{note})")
            return parsed, status, _save_log(record)
        raise AIError("模型连续返回空内容")
    except AIError as e:
        record["error"] = str(e)
        e.log_path = _save_log(record)
        raise


def review(cfg, proposal, per_engine, snapshot, account, news):
    """返回 {"verdict", "caps", "risk_notes", "status"}；失败时按 on_failure 降级。"""
    ai = cfg["ai"]

    def fallback(status):
        verdict = "approve" if ai.get("on_failure") == "rules" else "hold"
        return {"status": status, "verdict": verdict, "caps": [], "risk_notes": ""}

    if not ai.get("enabled"):
        return fallback("disabled")
    key = api_key()
    if not key:
        return fallback("skipped: 没找到 DeepSeek key（环境变量或钥匙串）")
    user = "请复核以下调仓方案，按要求输出 json：\n" + json.dumps({
        "proposed_weights": proposal,
        "per_engine": per_engine,
        "market_snapshot_pct": snapshot,
        "account": account,
        "news_headlines_last_3d": news,
    }, ensure_ascii=False, indent=1)
    try:
        out, status, log = chat_json(ai, key, SYSTEM, user, purpose="risk_review")
        caps = out.get("caps") or []
        if not isinstance(caps, list):
            raise AIError("caps 不是数组")
        return {"verdict": out.get("verdict", "approve"), "caps": caps,
                "risk_notes": out.get("risk_notes", ""), "status": status, "log": log}
    except Exception as e:  # noqa: BLE001 —— 任何失败都按配置降级，不让 AI 故障阻塞风控
        out = fallback(f"error: {type(e).__name__}: {e}")
        out["log"] = getattr(e, "log_path", None)
        return out


def apply_caps(proposal, caps, cash_symbol):
    """强制执行"只减不加"：AI 给的上限只会让权重变小，削掉的部分进现金。"""
    out = dict(proposal)
    applied = []
    for c in caps:
        try:
            s, cap = c["symbol"], max(0.0, float(c["max_weight"]))
        except (KeyError, TypeError, ValueError):
            continue
        if s not in out or s == cash_symbol or cap >= out[s]:
            continue
        freed = out[s] - cap
        out[s] = cap
        out[cash_symbol] = out.get(cash_symbol, 0) + freed
        applied.append({**c, "max_weight": cap, "from": round(proposal[s], 4)})
    return {s: x for s, x in out.items() if x > 1e-4}, applied
