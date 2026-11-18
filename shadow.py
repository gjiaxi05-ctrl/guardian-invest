"""
AI 影子账户：一个独立的模拟盘，让 DeepSeek 全权决定仓位（可以加仓、可以偏离规则），
和规则账户同时间、同价格、同成本对比。它不影响规则账户，只用来积累证据：
LLM 的判断没法做历史回测（模型训练时见过历史行情），只能这样向前验证。

硬性限制仍然有效：只能买白名单 ETF、只做多、不加杠杆、单只上限 max_single_weight。
"""
import json
import os

import ai_review

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "shadow_account.json")
JOURNAL = os.path.join(HERE, "state", "shadow_journal.jsonl")

SYSTEM = """你是一个主动管理的多资产组合经理，管理一个美元模拟账户，目标是长期风险调整后收益最大化：
既要尽量多赚，也要控制回撤（最大回撤超过 15% 视为失败）。
你的成绩会每天和一个"规则账户"对比。规则账户用回测验证过的趋势/动量规则配置，
你能看到它今天的目标仓位。你可以完全照抄、部分偏离或彻底不同——只有在你有明确理由认为能做得更好时才偏离。

限制：
- 只能持有这些 ETF：{whitelist}；不在其中的部分自动放入短期美债 BIL（相当于现金，有利息）
- 只做多，权重之和不超过 1，单只不超过 {max_single:.0%}
- 每次调仓有交易成本，权重变化小于 2% 的不会执行，所以不要频繁小幅调整
- 你每天都会被调用。下面会给你最近几次自己的决定和理由，保持一致性，除非信息真的变了

只输出 json，格式如下：
{{"weights": {{"SPY": 0.3, "IEF": 0.2}},
 "thesis": "当前判断的核心逻辑，两三句话",
 "vs_rules": "和规则账户的主要差异及理由；照抄就写'跟随规则'",
 "confidence": 0 到 1 之间的数字}}
用中文。"""


def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else None


def recent_decisions(n=5):
    if not os.path.exists(JOURNAL):
        return []
    rows = [json.loads(line) for line in open(JOURNAL)][-n:]
    return [{k: r.get(k) for k in ("date", "weights", "thesis", "vs_rules", "nav")} for r in rows]


def sanitize(weights, cfg):
    """把模型输出压进硬性限制里：白名单、非负、单只上限、总和不超过 1。"""
    cap = cfg["shadow"]["max_single_weight"]
    out = {}
    for s, w in (weights or {}).items():
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if s in cfg["whitelist"] and s != cfg["cash_symbol"] and w > 0:
            out[s] = min(w, cap)
    total = sum(out.values())
    if total > 1:
        out = {s: w / total for s, w in out.items()}
    out[cfg["cash_symbol"]] = 1 - sum(out.values())
    return {s: round(w, 4) for s, w in out.items() if w > 1e-4}


def decide(cfg, key, today, rule_target, snapshot, news, account):
    sys_prompt = SYSTEM.format(whitelist=", ".join(cfg["whitelist"]),
                               max_single=cfg["shadow"]["max_single_weight"])
    user = "今天的信息如下，请给出目标仓位 json：\n" + json.dumps({
        "date": today,
        "your_account": account,
        "rules_account_target": rule_target,
        "market_snapshot_pct": snapshot,
        "news_headlines_last_3d": news,
        "your_recent_decisions": recent_decisions(),
    }, ensure_ascii=False, indent=1)
    out, status, log = ai_review.chat_json(cfg["ai"], key, sys_prompt, user, purpose="shadow")
    return out, status + f"，原始记录 {log}"
