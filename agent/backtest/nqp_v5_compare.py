"""NQP V3 vs V5 牛熊对比 — 市场宏观开关版

V5 改进：沪深300 MA200 作为总开关，risk-off 时只允许 P4/P5（最强趋势）
"""
from __future__ import annotations

import io, sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.china_a import ChinaAEngine
from backtest.engines.base import _align, _load_optimizer
from backtest.loaders.tencent_loader import DataLoader as TencentLoader
from backtest.nqp_v33_signal_engine import SignalEngine as SEv3
from backtest.nqp_v5_signal_engine import SignalEngine as SEv5

POOL = [
    "002050.SZ", "601689.SH", "688017.SH", "002747.SZ",
    "300124.SZ", "002472.SZ", "000887.SZ", "603211.SH",
    "300750.SZ", "002594.SZ", "300274.SZ",
    "002371.SZ", "688012.SH", "688981.SH", "603501.SH",
    "603986.SH", "600584.SH", "688072.SH", "301269.SZ",
    "688256.SH", "688041.SH", "002230.SZ", "688111.SH", "603019.SH",
]

PERIODS = [
    ("🐻 2022 熊市",   "2022-01-01", "2022-12-31", "2020-07-01"),
    ("📊 2023 震荡",   "2023-01-01", "2023-12-31", "2021-07-01"),
    ("🐂 2024-25 牛市","2024-09-24", "2025-03-31", "2023-01-01"),
]

BASE = {"source":"tencent","interval":"1D","initial_cash":1_000_000,
        "commission_rate":0.00025,"commission_min":5.0,
        "stamp_tax":0.0005,"transfer_fee":0.00001,"slippage":0.001}


def backtest(name, t0, t1, d0, se_cls, dm):
    se = se_cls()
    sm = se.generate(dm)
    dm2 = {c: df[df.index >= t0] for c, df in dm.items()}
    codes = [c for c in POOL if c in dm2 and len(dm2[c]) > 20]
    sm = {c: s[s.index >= t0] for c, s in sm.items() if c in codes}

    config = {**BASE, "codes": POOL, "start_date": t0, "end_date": t1}
    dates, close_df, tp, ret_df = _align(dm2, sm, codes, optimizer=_load_optimizer(config))
    codes = [c for c in codes if c in tp.columns]

    eng = ChinaAEngine(config)
    eng._execute_bars(dates, dm2, close_df, tp, codes)
    eq = pd.Series([s.equity for s in eng.equity_snapshots],
                   index=pd.to_datetime([s.timestamp for s in eng.equity_snapshots]))
    if len(eq) < 5: return {"error": True}

    initial = eng.initial_capital
    f = eq.iloc[-1]
    total_ret = (f/initial - 1) * 100
    bench = ((1 + ret_df.mean(axis=1)).prod() - 1) * 100
    dr = eq.pct_change().dropna()
    y = max(len(dr)/252, 0.01)
    ann = ((f/initial)**(1/y)-1)*100
    vol = dr.std()*(252**0.5)*100
    sharpe = (dr.mean()-0.025/252)/max(dr.std(),1e-10)*(252**0.5)
    dd = ((eq-eq.cummax())/eq.cummax()*100).min()
    t = len(eng.trades)
    w = sum(1 for x in eng.trades if x.pnl>0)/max(t,1)*100
    pnl = sum(x.pnl for x in eng.trades)
    comm = sum(x.commission for x in eng.trades)
    aw = sum(x.pnl for x in eng.trades if x.pnl>0)/max(sum(1 for x in eng.trades if x.pnl>0),1)
    al = abs(sum(x.pnl for x in eng.trades if x.pnl<0)/max(t-sum(1 for x in eng.trades if x.pnl>0),1))
    pf = aw/max(al,0.01)

    bd = sum(int((s==1.0).sum()) for s in sm.values())
    td = sum(len(s) for s in sm.values())
    pos = bd/max(td,1)*100

    return {"ret":total_ret,"bench":bench,"alpha":total_ret-bench,
            "ann":ann,"vol":vol,"sharpe":sharpe,"dd":dd,
            "trades":t,"wr":w,"pf":pf,"pnl":pnl,"comm":comm,
            "aw":aw,"al":al,"pos":pos}


def main():
    print("=" * 70)
    print(f"NQP V3 vs V5 市场开关对比 — {datetime.now().strftime('%H:%M')}")
    print("V5: 沪深300<MA200时只允许P4/P5强趋势 | V3: 全信号")
    print("=" * 70)

    loader = TencentLoader()
    for name, t0, t1, d0 in PERIODS:
        print(f"\n{'─'*60}\n  {name}: {t0} → {t1}\n{'─'*60}")

        # 加载数据（含沪深300）
        codes_all = POOL + ["000300.SH"]
        dm = loader.fetch(codes=codes_all, start_date=d0, end_date=t1, interval="1D")
        dm.pop("000300.SH", None)  # V3 不需要
        print(f"  数据: {len(dm)}/{len(POOL)} 只")

        # V3（不含沪深300）
        print("  [V3.3] 运行中...")
        r3 = backtest(name, t0, t1, d0, SEv3, dm)

        # V5（含沪深300）
        print("  [V5]   运行中...")
        dm5 = loader.fetch(codes=codes_all, start_date=d0, end_date=t1, interval="1D")
        r5 = backtest(name, t0, t1, d0, SEv5, dm5)

        if r3.get("error") or r5.get("error"):
            print("  ⚠️ 数据不足，跳过")
            continue

        print(f"\n  {'指标':<16} {'V3.3':>10} {'V5':>10} {'效果':>8}")
        print(f"  {'─'*44}")

        metrics = [
            ("总收益率","ret"),("超额收益","alpha"),("Sharpe","sharpe"),
            ("最大回撤","dd"),("胜率","wr"),("盈亏比","pf"),
            ("交易次","trades"),("持仓率","pos"),
        ]
        prev_good = None
        for label, key in metrics:
            v3, v5 = r3[key], r5[key]
            if key in ("ret","alpha","ann","wr","pos"):
                good = v5 > v3
            elif key in ("dd",):
                good = v5 > v3  # 回撤改善 = 负数变小
            elif key in ("sharpe","pf"):
                good = v5 > v3
            else:
                good = None

            fs = ".2f" if key in ("sharpe","pf") else ".1f"
            u = "%" if key not in ("sharpe","pf","trades") else ""
            arrow = "✅" if good is True else ("❌" if good is False else "—")
            if key == "trades":
                print(f"  {label:<16} {v3:>10.0f} {v5:>10.0f} {arrow:>6}")
            elif key in ("sharpe","pf"):
                print(f"  {label:<16} {v3:>10.2f} {v5:>10.2f} {arrow:>6}")
            else:
                print(f"  {label:<16} {v3:>+9.1f}% {v5:>+9.1f}% {arrow:>6}")

        a3, a5 = r3["alpha"], r5["alpha"]
        d3, d5 = r3["dd"], r5["dd"]
        print(f"\n  ✨ 超额: {a3:+.1f}% → {a5:+.1f}% | 回撤: {d3:+.1f}% → {d5:+.1f}%")


if __name__ == "__main__":
    main()
