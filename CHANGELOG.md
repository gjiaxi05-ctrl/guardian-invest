# Changelog · 更新日志

## v0.1.0 · 2026-11-17 · First public release · 首次公开

**English**

- Four risk levels (Conservative, Balanced, Growth, Aggressive) built on one mechanism, tranched monthly trend + volatility management, switched with one line in `config.local.json`
- Daily backtest engine: next-open fills, commissions and slippage, split-sample statistics (`backtest_flex.py --levels --split`)
- DeepSeek AI risk review that can only lower position caps (reduce-only enforced in code, every call archived), plus an AI shadow account with full control as a comparison
- Hard guardrails: ETF whitelist, single-order cap, daily order and buy limits, drawdown breaker, data checks, emergency stop file
- Config guard: every parameter has hard limits; real-money mode needs a confirmation phrase and a capital cap
- Local paper account that mirrors real trading (next-open settlement, whole shares) and a strategy arena that runs several strategies forward
- Dashboard with value, drawdown, year-by-year returns and forward results for the four levels and benchmarks, with an English / 中文 switch
- Offline unit tests and GitHub Actions

**中文**

- 四个风险档位：稳健、平衡、成长、激进，同一套"分批月度趋势 + 波动率管理"机制，`config.local.json` 里一行切换
- 日线回测引擎：次日开盘成交、含佣金和滑点、分段统计（`backtest_flex.py --levels --split`）
- DeepSeek AI 风控：只能下调仓位上限，代码强制只减不加，完整留档；另有 AI 全权操盘的影子账户做对照
- 硬性风控：ETF 白名单、单笔上限、每日笔数和买入上限、回撤熔断、数据体检、急停文件
- 配置守门：每个参数都有不可突破的范围，真钱模式需要确认口令和资金上限
- 本地模拟盘贴近实盘：次日开盘价结算、只买整股；策略擂台同时向前运行多个策略
- 看板网页：四个档位和对照组的净值、回撤、逐年收益、向前实测，可切换中英文
- 离线单元测试和 GitHub Actions

Development history: [research log](docs/research-log.md) · [研究日志](docs/research-log.zh-CN.md). The paper account has run every day since 2026-10-19.
