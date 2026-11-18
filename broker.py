"""
券商接口。每个券商实现同样的方法：
cash() / positions() / quote(symbol) / place(symbol, qty) / save() / pending_orders()

- PaperBroker：本地模拟盘
- IBKRBroker：盈透证券，经本机运行的 IB Gateway / TWS 连接（ib_async 库）
"""
import atexit
import json
import os

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "paper_account.json")


class PaperBroker:
    """
    本地模拟盘，账户存在 state/*.json。
    paper_fill = "next_open"（默认）：和真实下单一样，早上提交的单要等下一个交易日开盘才成交。
      下单时先按最新收盘价预记账，下次运行时用真实的复权开盘价重新结算，差额计入现金。
    paper_fill = "close"：按最新收盘价立即成交（旧方式，偏乐观）。
    """
    whole_shares_only = False
    sells_settle_later = False

    def __init__(self, cfg, prices, path=STATE, market_day=None):
        self.prices, self.path, self.market_day = prices, path, market_day
        self.slip = cfg["costs"]["slippage"]
        self.fee = cfg["costs"]["commission_per_order_usd"]
        self.next_open = cfg.get("paper_fill", "next_open") == "next_open" and market_day
        if os.path.exists(path):
            self.acct = json.load(open(path))
        else:
            self.acct = {"cash": float(cfg["paper_capital_usd"]), "positions": {}}
        self.acct.setdefault("pending", [])

    def settle(self):
        """把上次按收盘价预记账的订单，改按其后第一个交易日的开盘价结算。返回结算记录。"""
        import data
        done, keep = [], []
        for p in self.acct["pending"]:
            hit = data.first_open_after(p["symbol"], p["after"])
            if not hit:
                keep.append(p)
                continue
            day, px_open = hit
            fill = px_open * (1 + self.slip if p["qty"] > 0 else 1 - self.slip)
            diff = p["qty"] * (p["est_price"] - fill)
            self.acct["cash"] += diff
            done.append({**p, "fill_day": day, "fill_price": round(fill, 4), "cash_adjust": round(diff, 2)})
        self.acct["pending"] = keep
        return done

    def cash(self):
        return self.acct["cash"]

    def positions(self):
        return dict(self.acct["positions"])

    def quote(self, symbol):
        return self.prices[symbol]

    def pending_orders(self):
        return []

    def place(self, symbol, qty):
        """qty>0 买，<0 卖。返回成交记录。"""
        px = self.quote(symbol) * (1 + self.slip if qty > 0 else 1 - self.slip)
        value = qty * px
        self.acct["cash"] -= value + self.fee
        pos = self.acct["positions"].get(symbol, 0) + qty
        if abs(pos) < 1e-9:
            self.acct["positions"].pop(symbol, None)
        else:
            self.acct["positions"][symbol] = pos
        rec = {"symbol": symbol, "qty": round(qty, 6), "price": round(px, 4),
               "value": round(value, 2), "fee": self.fee}
        if self.next_open:
            self.acct["pending"].append({"symbol": symbol, "qty": qty, "est_price": px, "after": self.market_day})
            rec["status"] = "按收盘价预记账，下一交易日开盘价结算"
        return rec

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump(self.acct, open(self.path, "w"), indent=1)


class IBKRBroker:
    """
    盈透证券。安全设计：
    - 只管理白名单里的 ETF，账户里其他持仓一律视而不见，永远不会被卖掉
    - max_capital_usd 限制系统最多动用多少钱，超出的现金不参与
    - dry_run=true 时以只读方式连接，只算出订单、不发送
    - 模拟/实盘模式和账户号必须对得上（IBKR 模拟账户号以 D 开头）
    - API 不支持零股，只能整股；卖出要到开盘才成交，所以有卖单的那天不提交买单
    """
    whole_shares_only = True
    sells_settle_later = True
    ORDER_TAG = "guardian"

    def __init__(self, cfg, prices):
        from ib_async import IB
        c = cfg["ibkr"]
        self.cfg, self.c, self.prices = cfg, c, prices
        self.dry_run = c.get("dry_run", True)
        self.managed = set(cfg["whitelist"])
        self.ib = IB()
        self.ib.connect(c["host"], c["port"], clientId=c["client_id"], readonly=self.dry_run, timeout=20)
        atexit.register(self.ib.disconnect)
        accounts = self.ib.managedAccounts()
        self.account = c.get("account") or accounts[0]
        if self.account not in accounts:
            raise RuntimeError(f"Gateway 登录的账户里没有配置的 {self.account}")
        # Gateway 有时"连得上但不回数据"（例如账户套餐不支持 API、会话失效），这时必须停下，
        # 否则现金会被读成 0，后面的仓位计算全错
        if not self.ib.accountValues(self.account):
            raise RuntimeError("Gateway 已连接但不返回账户数据（可能会话失效，或账户套餐不支持 API）")
        is_paper_acct = self.account.startswith("D")
        if cfg["mode"] == "ibkr_paper" and not is_paper_acct:
            raise RuntimeError(f"配置是 ibkr_paper，但连上的是实盘账户 {self.account[:2]}***，已停止")
        if cfg["mode"] == "ibkr_live" and is_paper_acct:
            raise RuntimeError("配置是 ibkr_live，但连上的是模拟账户，已停止")

    def _usd_cash(self):
        vals = self.ib.accountValues(self.account)
        for tag in ("CashBalance", "TotalCashValue"):
            for v in vals:
                if v.tag == tag and v.currency == "USD":
                    return float(v.value)
        return 0.0

    def positions(self):
        out = {}
        for p in self.ib.positions(self.account):
            if p.contract.symbol in self.managed and p.position:
                out[p.contract.symbol] = float(p.position)
        return out

    def cash(self):
        real = self._usd_cash()
        cap = self.c.get("max_capital_usd")
        if not cap:
            return real
        managed_value = sum(q * self.prices[s] for s, q in self.positions().items())
        return max(0.0, min(real, cap - managed_value))

    def quote(self, symbol):
        return self.prices[symbol]

    def pending_orders(self):
        return [t for t in self.ib.openTrades() if t.order.orderRef == self.ORDER_TAG]

    def place(self, symbol, qty):
        from ib_async import MarketOrder, Stock
        px = self.prices[symbol]
        rec = {"symbol": symbol, "qty": qty, "price": round(px, 4), "value": round(qty * px, 2), "fee": 0}
        if self.dry_run:
            rec["status"] = "只读模式，未发送"
            return rec
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        order = MarketOrder("BUY" if qty > 0 else "SELL", abs(qty), tif=self.c.get("tif", "OPG"),
                            account=self.account, orderRef=self.ORDER_TAG)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(2)
        rec["status"] = f"已提交（{trade.orderStatus.status}），开盘成交"
        return rec

    def save(self):
        pass  # 持仓以券商为准，不需要本地保存


def make(cfg, prices, market_day=None):
    if cfg["mode"] == "paper":
        b = PaperBroker(cfg, prices, market_day=market_day)
        if cfg.get("paper_like_ibkr"):  # 让模拟盘和将来的 IBKR 实盘一样：只能整股
            b.whole_shares_only = True
        return b
    if cfg["mode"] in ("ibkr_paper", "ibkr_live"):
        return IBKRBroker(cfg, prices)
    raise ValueError(f"未知 mode：{cfg['mode']}")
