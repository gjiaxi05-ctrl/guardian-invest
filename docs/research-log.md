# Research log

> These notes were written in date order while the system was being built. They keep the reason behind each change, the conclusions that were later overturned, and the numbers as they stood at the time.
> Some figures (for example the early drawdowns measured only at month-ends) were later corrected by stricter backtests; **the [README](../README.md) has the current numbers**.
> Read together, the entries show how the strategy evolved from "monthly trend following" into today's four-level "tranched trend + volatility management" design.
> [中文版](research-log.zh-CN.md)

## 2026-11-17: split into four risk levels, prepared for publication

The parameters of the main strategy (50% tranched monthly trend + 50% volatility management) were turned into a single dial with four settings: Conservative, Balanced, Growth and Aggressive.
Each level changes only four numbers: the share of 2x ETFs when the trend is up, the volatility target, the maximum exposure, and a permanent cash reserve.
Growth is the main strategy that had been running all along; its target weights and backtest results are identical, to the last digit, before and after the change.

The first version of Conservative used "volatility target 10%, no cash reserve". Its backtest max drawdown was -22%, almost the same as Balanced (-21%):
in a slow bear market like 2022 volatility is not especially high, so volatility management does not cut exposure, and lowering the target did not help.
Changing it to "60% of Balanced + a permanent 40% in short-term Treasuries" brought the max drawdown to -15%, and the four levels finally rose steadily in both return and drawdown.
This is the only time a parameter was changed after looking at results, and it is recorded here for that reason.

| Level | Annual return | Max drawdown | Worst 12 months |
|---|---:|---:|---:|
| Conservative | 6.5% | -15.0% | -12.3% |
| Balanced | 10.4% | -21.4% | -16.3% |
| Growth | 14.2% | -31.6% | -22.2% |
| Aggressive | 16.4% | -40.8% | -29.9% |
| 100% SPY | 11.0% | -51.9% | -47.4% |

(2008-01 to 2026-11-16, next-open fills, $0.35 per order + 0.05% slippage, trade only when 10% off target. Growth is a little below the 14.7% reported on 10-23 because three more weeks of data were added.)

Offline unit tests were added at the same time (AI reduce-only, config guard, hard guardrails, valid weights at every level, no look-ahead), and personal settings moved into a private, uncommitted `config.local.json`.

On 11-18 a data-cache bug turned up: cached prices used to count as fresh for 12 hours. The cache had been refreshed the evening before, before the US market opened, so the next morning's scheduled run reused it,
missed the previous day's close and rebalanced one day late. The rule is now "the cache is fresh only if it was written after the most recent market close", and that day's run was repeated.

## Starting point: a low-drawdown automated investing system for stock and bond ETFs

The pipeline: rules propose → AI reviews → hard guardrails → execution → daily report.
The key principle is that **the AI can only reduce positions, never add**. The reason: an LLM's judgement cannot be backtested (the model saw historical prices during training),
so its power is limited so that, at worst, it is more conservative than the rules.

## Components

| File | Purpose |
|---|---|
| `strategies.py` | Strategies: Guardian (own design: trend voting + risk parity + volatility target), Keller HAA, Faber, dual momentum and others, shared by backtest and live runs |
| `backtest.py` | Monthly backtest including trading costs; `--robust` runs a parameter-sensitivity check |
| `engine.py` | Computes target weights from the last complete month of data (combining several engines according to the `profile` in config) |
| `ai_review.py` | DeepSeek review (with news headlines); it may only output "lower the cap on asset X to Y", and the code enforces reduce-only |
| `shadow.py` | AI shadow account: a separate paper account fully controlled by DeepSeek, to test forward whether the AI can beat the rules |
| `run.py` | Daily pipeline: emergency stop → data checks → drawdown breaker → rebalance → guardrail limits → fill or approve → report |
| `broker.py` | Local paper broker + IBKR (via IB Gateway, `ib_async` library) |
| `config.json` | All parameters and risk thresholds |

## Backtest (2007-03 to 2026-09, 0.1% one-way cost)

| | Annual return | Max drawdown | Worst 12 months | Chance of any 12-month loss |
|---|---:|---:|---:|---:|
| 100% SPY | 11.0% | -50.8% | -43.4% | 16.6% |
| 60/40 stocks/bonds | 8.1% | -29.5% | -26.1% | 13.5% |
| conservative: Guardian 50 + HAA 50 | 8.0% | -9.2% | -6.8% | 9.9% |
| balanced: Guardian 25 + HAA 40 + US trend 35 | 9.6% | -9.7% | -9.7% | 9.4% |
| aggressive: HAA 50 + US trend 50 | 10.8% | -11.6% | -11.6% | 11.7% |
| **growth (default): US trend 50 + 2x trend 50** | **14.8%** | **-25.0%** | **-22.3%** | **25.1%** |

Switch with `profile` in `config.json`. "US trend" = half SPY, half QQQ, each switched to bonds/cash when it falls below its 10-month moving average.
"2x trend" = the same signal, but holding the 2x ETFs (SSO/QLD). 2x ETFs do not borrow and cannot be margin-called, but they move about twice as much and lose value in choppy markets.
Note: the table above is measured on **month-end** values and cannot see falls within a month. Re-running the backtest daily with next-open fills (`backtest_daily.py`)
gives growth about 13.4% a year with a **daily max drawdown of about -32%**, which is the number a holder would actually experience.
So the drawdown breaker for growth is set at 35%, deeper than the historical worst of -32%, and triggers only in a situation beyond anything in the data.

**How often to check the signal**: daily backtests compared checking at each month-end, each week or each day, adding a buffer band, and "buy at month-end, sell as soon as it breaks".
Annual returns ranged from 12.7% to 14.7% and max drawdowns from -31% to -36%; none was clearly better, and checking daily traded more without reducing drawdowns.
So the signal kept being checked at month-end, with fills at the next trading day's open.

## 2026-10-23 (evening): monthly part switched to "four tranches"

Testing the criticisms raised in another AI's review found:
- **Whipsaws are real**: since 2008 the S&P 500 signal sold 15 times and 14 times bought back later at a higher price (6.8% more on average); the Nasdaq-100 14 out of 16 times (7.7%).
  The strategy's value comes from the few times it avoided a big fall (23.8% less loss in 2008, 18.3% less in 2022). In essence it pays a small, frequent premium for insurance against a big bear market.
- **Check-day luck is large**: checking only at month-end gave a max drawdown of -33%; checking on the 1st/5th/10th/15th trading day of the month instead gave -47%/-35%/-42%/-44%.
  Month-end happened to be the luckiest (it got out on 2020-02-28 just in time). With the then-current main strategy, changing the check day moved the max drawdown between -27.5% and -37.6%.
- **Buffer bands (sell only after falling 2–3% below the average) do not work well**: they exit later in fast crashes, and the 2020 max drawdown worsened to -44%.
- **Four tranches work**: month-end and the 5th/10th/15th trading day each decide a quarter, so the result does not depend on any single day.

Main strategy = 50% tranched monthly + 50% volatility management: 14.7% a year, max drawdown -31.6%, worst 12 months -22.2%, chance of a 12-month loss 21%, about 103 orders a year.
-31.6% is the more honest expectation (it does not rely on check-day luck); the earlier -27.5% was optimistic.

## Previous main strategy (2026-10-23, daytime): 50% monthly trend + 50% daily volatility management

"Slow on direction, fast on position size":
- **Monthly part** (growth level): hold 1x + 2x ETFs while the S&P 500 / Nasdaq-100 is above its 10-month moving average, otherwise short-term Treasuries. Checked once at each month-end (trends change slowly; checking daily only causes whipsaws)
- **Daily part** (volatility management, Moreira & Muir 2017): each day, look at the 20-day volatility of the S&P 500 / Nasdaq-100 basket;
  exposure = 20% ÷ volatility (capped at 2x). The calmer the market, the more it invests; as soon as volatility jumps, it cuts back
- Trade only when 10% or more off target (`rebalance_band: 0.10`) to limit turnover

Daily backtest (`backtest_flex.py`, from 2008, next-open fills, $0.35 per order + 0.05% slippage):

| | Annual return | Max drawdown | Worst 12 months | Chance of a 12-month loss | Orders per year |
|---|---:|---:|---:|---:|---:|
| Old: monthly growth | 14.3% | -33.0% | -29.9% | 26.8% | 9 |
| **New: 50% monthly + 50% volatility** | **14.0%** | **-27.5%** | **-23.9%** | **22.3%** | 100 |

Varying the parameters (volatility target 15–25%, 20- or 60-day window, band 5–15%, commission $0–1) kept annual returns between 12% and 16%, and the chance of a loss stayed below the old strategy's.
This is an improvement in "similar return, smoother ride", not a breakthrough in return.

Daily strategies that were tested but not used: VIX term structure and a credit-spread canary (they lost 48% and 60% respectively in the slow, rate-hike bear market of 2022; they only protect against panic crashes),
a multi-signal dashboard (smallest drawdown but low return), buying dips within an uptrend (RSI2), and the turn-of-the-month effect. **At $1 commission per order, strategies that trade daily lose 7–9% a year to costs**,
so small accounts must keep turnover low.

## Strategy dashboard

`reports/dashboard.html`: historical value of every strategy (log scale), drawdowns, metrics (full period / first half / second half), year-by-year returns,
and the forward paper accounts (main rules account, AI shadow account, every arena account). It is rebuilt by `dashboard.py` after each scheduled daily run.

## Strategy arena

Six strategies each got a $4,200 virtual account (fractional shares, next-open fills, the same costs) and rebalance every day on their own signals; the daily report ranks them by return:
monthly growth (old main), monthly + volatility (new main, without AI), volatility management, trend × volatility, multi-signal dashboard, VIX term structure.
Together with the main rules account (with AI review) and the AI shadow account, eight curves run forward at the same time, to be compared on real prices after a few months.

## Making the local paper account as close to real trading as possible

In `config.json`: `paper_like_ibkr: true`, `paper_fill: next_open`, `paper_capital_usd: 4200`:
- Orders placed in the morning settle at the **next trading day's open** (booked first at the close, then re-settled at the real opening price on the next run), matching the timing of a real order
- **Whole shares only**, with SPY/QQQ replaced by the cheaper SPYM/QQQM, as on a real IBKR account
- Capital matches the size the author plans to trade with (about $4,200)
- When run manually during US market hours, today's unfinished price bar is dropped automatically
- The AI shadow account keeps $10,000 and fractional shares, because it measures the AI's judgement; it is compared with the rules account over the same period from 2026-10-23
- "The effect of the AI review" is measured with two virtual accounts (pure rules vs following the AI) booked the same way, so the difference reflects only the AI's contribution

The old $10,000 paper records are archived in `state/archive_2026-10-23_before_realistic/`.

## Config guard (`config_guard.py`)

`config.json` is checked before every run, so even a hand-edited config cannot produce absurd values. Corrections apply only to that run, the file is never rewritten, and each correction is written into the daily report's events until it is fixed:
- Every numeric parameter has hard limits (for example single order 5%–60%, drawdown breaker 5%–50%, 1–30 orders a day); values outside are pulled back to the limit
- Option parameters can only take valid values; an invalid value is replaced with the safer option (for example a misspelled execution mode becomes "requires approval")
- The whitelist may only contain index/bond/commodity ETFs plus SSO and QLD; single stocks, 3x and inverse ETFs are always removed; cash can only be BIL
- Strategy weights summing to more than 1 (leverage in disguise) are scaled back
- **Real-money mode** requires `"live_confirmation"` set to the confirmation phrase in the config plus `ibkr.max_capital_usd`; otherwise the connection is forced read-only

## AI call archive (`state/ai_log/DATE/TIME_PURPOSE.json`)

Every DeepSeek call (risk review `risk_review`, shadow account `shadow`) is saved in full: the prompt and data sent, the effort/time/outcome of each attempt,
DeepSeek's raw JSON response (including its reasoning `reasoning_content` and token usage) and the parsed result; failed calls are archived too. The API key is never saved.
The daily report links that day's archive files. View them with `python3 ai_log.py` (last 20 calls) or `python3 ai_log.py <file>` (one call in full).

## Risk layer (neither the AI nor the rules can bypass it)

- Whitelist: only 13 ETFs can be bought; no single stocks, no leverage beyond the two 2x ETFs, no short selling
- No trade when less than 2% off target
- Each order at most 35% of net value (except cash-like BIL), at most 12 orders a day, daily trading value at most 100% of net value
- Drawdown breaker: more than 12% below the peak halves risky assets until the drawdown narrows to within 6%
- Data checks: if prices have not updated for more than 4 days, or move more than 25% in a day (likely bad data), stop trading that day
- Emergency stop: put a `STOP` file in the folder and the system does nothing

(These were the early settings; later the band became 10% and the breaker 35%, see the README.)

## Running it

```bash
python3 run.py              # once a day after the US close
python3 backtest.py         # re-run the monthly backtest
python3 run.py --approve    # approve pending orders when execution is set to "approve"
```

The AI is DeepSeek (`deepseek-v4-pro`, reasoning effort max), called twice a day; both calls include the rules' proposal, a market summary and headlines from the last three days on nine Google News topics (about 90):
- **Risk review**: re-evaluated every day on top of the rules' target and may only cut; when the risk passes, positions return to the rules' target automatically.
- **AI shadow account**: another $10,000 paper account where DeepSeek decides all positions (whitelist only, long only, at most 50% in any one ETF),
  compared side by side with the rules account in the daily report; its decisions are logged in `state/shadow_journal.jsonl`.
  Only after at least six months of consistently beating the rules account would giving the AI the power to add risk be considered.
- Cost is about ¥0.25 a day (about ¥5 a month); the balance is shown at the bottom of the report, with a reminder to top up below ¥3.
The key lives in the macOS Keychain and is never written to a file:
`security add-generic-password -U -s guardian-deepseek -a guardian -w` (or use the environment variable `DEEPSEEK_API_KEY`).
If the balance runs out, the report says so and the system continues on rules alone.
Without a key the system runs on rules alone, and the report states that the AI was skipped.
Telegram notifications: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Checklist before real money

1. Run the paper account for at least 3 months, including at least one signal change
2. Confirm the gap between actual fills and the backtest (slippage, whole shares, dividends) is acceptable
3. Read every AI cut and its reason in `state/journal.jsonl` and confirm it is not cutting at random
4. Give the broker API trading permission only, never deposit or withdrawal permission
5. Start the first real money small, with `execution` in `approve` mode

## Scheduling

A launchd job `~/Library/LaunchAgents/com.guardian-invest.daily.plist`
runs `run_daily.sh` at 7:30 a.m. Beijing time, Tuesday to Saturday (the morning after each US trading day), appending output to `logs/daily.log`.
If the Mac is asleep, the run happens after it wakes; a run missed while the Mac is shut down is not made up.
The network is often not ready right after waking, so the script first waits for it (up to 10 minutes), price downloads retry automatically, and a failed run is retried once after 10 minutes.
Re-running never duplicates orders: while IBKR still has an unfilled order from this system (tagged `guardian`), no new orders are placed that day.
The job runs under `caffeinate -i`, which keeps the Mac from idle-sleeping during the run (on 2026-10-24 it fell asleep in the middle of an AI call and hung for 2 hours).
News comes from Google News, then Bing News, then Yahoo Finance (Google returns 503 to shared exit IPs such as VPNs); the report lists how many headlines each source returned and warns if all fail.

```bash
launchctl kickstart gui/$(id -u)/com.guardian-invest.daily                             # run once now
launchctl bootout gui/$(id -u)/com.guardian-invest.daily                               # disable the schedule
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.guardian-invest.daily.plist # enable it again
```

## Connecting to IBKR

`mode` in `config.json`: `paper` (local) → `ibkr_paper` (IBKR paper account) → `ibkr_live` (real money).
**IBKR Lite accounts cannot use the API** (only IBKR Pro can, and Pro charges commission per order). The author has not upgraded, so the local paper account is used for now;
on 2026-10-21 one full order cycle was tested on an IBKR paper account, the adapter code is kept, and switching `mode` is enough after upgrading to Pro.
IB Gateway must stay logged in; turning on auto-restart under Gateway Configure → "Lock and Exit" means logging in again only once a week.

- The IBKR API does not support fractional shares, so orders use SPYM instead of SPY and QQQM instead of QQQ (same index, lower price); signals are still computed on SPY/QQQ
- Only whitelisted ETFs are managed; other holdings in the account are never touched; `ibkr.max_capital_usd` caps how much money can be used
- With `ibkr.dry_run: true` the connection is read-only: orders are computed but not sent
- Orders are market-on-open (OPG), submitted at 7:30 a.m. and filled at the US open; on days with sell orders it only sells and buys the next day

IB Gateway settings: choose Paper Trading when logging in; under Configure → Settings → API → Settings tick
"Enable ActiveX and Socket Clients", set Socket port to 4002, tick "Read-Only API" at first, and add 127.0.0.1 to Trusted IPs.
