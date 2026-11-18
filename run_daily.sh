#!/bin/zsh
# 定时任务入口：由 launchd（示例见 deploy/）每个美股交易日的次日早上调用，输出追加到 logs/daily.log。
# Mac 刚唤醒时网络可能没就绪：先最多等 10 分钟网络；整次运行失败的话，10 分钟后再跑一次。
# 重跑是安全的：IBKR 里还有本系统未成交的订单时，系统不会再下新单。
# 整个任务用 caffeinate -i 包住：运行期间阻止 Mac 闲置睡眠（结束即失效，不改系统设置）。
# 否则 Mac 在 AI 调用途中睡着，连接会卡住几个小时（2026-10-24 发生过）。
if [ -z "$GUARDIAN_AWAKE" ]; then
  export GUARDIAN_AWAKE=1
  exec /usr/bin/caffeinate -i /bin/zsh "$0" "$@"
fi
cd "$(dirname "$0")" || exit 1
mkdir -p logs
# 用哪个 Python：项目里的 .venv → 上一级的 .venv → 系统 python3（不连 IBKR 时不需要任何第三方库）
PY=python3
for p in .venv/bin/python ../.venv/bin/python; do
  [ -x "$p" ] && PY="$p" && break
done
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  for i in {1..20}; do
    /usr/bin/curl -s -o /dev/null -m 10 https://query1.finance.yahoo.com && break
    echo "网络未就绪，30 秒后重试（$i/20）"
    /bin/sleep 30
  done
  $PY run.py
  code=$?
  if [ $code -ne 0 ]; then
    echo "运行失败（exit=$code），10 分钟后重试一次"
    /bin/sleep 600
    echo "===== 重试 $(date '+%Y-%m-%d %H:%M:%S') ====="
    $PY run.py
    code=$?
  fi
  echo "exit=$code"
  # 更新策略看板；配置里 publish_dashboard 为 true 时同时发布在线版（失败都不影响交易）
  $PY dashboard.py --publish || echo "看板生成失败"
} >> logs/daily.log 2>&1
