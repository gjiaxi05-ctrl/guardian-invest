"""
查看 AI 调用留档（state/ai_log/）。
  ../.venv/bin/python ai_log.py            # 列出最近 20 次调用
  ../.venv/bin/python ai_log.py <文件>     # 看某一次的完整内容：思考过程、原始回答、耗时
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def summary(path):
    r = json.load(open(path, encoding="utf-8"))
    resp = r.get("response") or {}
    u = resp.get("usage", {})
    tries = " → ".join(f"{a.get('effort')}:{a.get('outcome')}({a.get('seconds', '?')}s)" for a in r["attempts"])
    verdict = (r.get("parsed") or {}).get("verdict") or ("自定仓位" if r.get("parsed") else "")
    err = f"  错误：{r['error']}" if r.get("error") else ""
    return (f"{r['time']}  {r['purpose']:12s} {verdict:8s} 输入 {u.get('prompt_tokens', '-')} / 输出 "
            f"{u.get('completion_tokens', '-')}  [{tries}]{err}\n    {os.path.relpath(path, HERE)}")


def show(path):
    r = json.load(open(path, encoding="utf-8"))
    msg = ((r.get("response") or {}).get("choices") or [{}])[0].get("message", {})
    print(summary(path), "\n")
    print("=== 思考过程（reasoning_content）===\n", msg.get("reasoning_content") or "（无）", "\n")
    print("=== 原始回答（content）===\n", msg.get("content") or "（无）", "\n")
    print("=== 发给模型的数据（user，截取前 3000 字）===\n", r["request"]["messages"][1]["content"][:3000])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        show(sys.argv[1] if os.path.isabs(sys.argv[1]) else os.path.join(HERE, sys.argv[1]))
    else:
        files = sorted(glob.glob(os.path.join(HERE, "state", "ai_log", "*", "*.json")))[-20:]
        print("\n".join(summary(f) for f in files) or "还没有留档")
