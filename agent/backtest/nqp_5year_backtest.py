"""NQP 近五年回测 — V3 vs V5 vs V6 逐年对比

用法：cd d:/projects/Vibe-Trading/agent && python backtest/nqp_5year_backtest.py
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd
from pathlib import Path
from datetime import datetime

_AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.china_a import ChinaAEngine
from backtest.engines.base import _align, _load_optimizer
from backtest.loaders.tencent_loader import DataLoader
from backtest.nqp_v33_signal_engine import SignalEngine as V3
from backtest.nqp_v5_signal_engine import SignalEngine as V5
from backtest.nqp_v6_signal_engine import SignalEngine as V6

POOL = ['002050.SZ','601689.SH','688017.SH','002747.SZ','300124.SZ','002472.SZ','000887.SZ','603211.SH',
        '300750.SZ','002594.SZ','300274.SZ','002371.SZ','688012.SH','688981.SH','603501.SH',
        '603986.SH','600584.SH','688072.SH','301269.SZ','688256.SH','688041.SH','002230.SZ','688111.SH','603019.SH']
POOL_HS = POOL + ['000300.SH']
BASE = {'source':'tencent','interval':'1D','initial_cash':1_000_000,'commission_rate':0.00025,
        'commission_min':5.0,'stamp_tax':0.0005,'transfer_fee':0.00001,'slippage':0.001}

DATA_START = "2020-01-01"  # MA200 warmup
FULL_START = "2021-07-01"  # 回测起点
FULL_END   = "2026-07-18"  # 回测终点


def run_backtest(t0, t1, se_cls, dm):
    se = se_cls()
    sm_full = se.generate(dm)
    # 裁剪数据到 [t0, t1]
    dm2 = {}
    for c, df in dm.items():
        sliced = df[(df.index >= t0) & (df.index <= t1)]
        if len(sliced) > 20:
            dm2[c] = sliced
    codes = [c for c in POOL if c in dm2]
    sm = {}
    for c, s in sm_full.items():
        if c in codes:
            sliced = s[(s.index >= t0) & (s.index <= t1)]
            sm[c] = sliced
    if not codes:
        return None

    cfg = {**BASE, 'codes': POOL, 'start_date': t0, 'end_date': t1}
    dates, close_df, tp, ret_df = _align(dm2, sm, codes, optimizer=_load_optimizer(cfg))
    codes = [c for c in codes if c in tp.columns]

    eng = ChinaAEngine(cfg)
    eng._execute_bars(dates, dm2, close_df, tp, codes)

    eq = pd.Series(
        [s.equity for s in eng.equity_snapshots],
        index=pd.to_datetime([s.timestamp for s in eng.equity_snapshots]),
    )
    if len(eq) < 5:
        return None

    total_ret = (eq.iloc[-1] / 1e6 - 1) * 100
    bench_ret = ((1 + ret_df.mean(axis=1)).prod() - 1) * 100
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    dr = eq.pct_change().dropna()
    y = max(len(dr) / 252, 0.01)
    ann_ret = ((eq.iloc[-1] / 1e6) ** (1 / y) - 1) * 100
    ann_vol = dr.std() * (252 ** 0.5) * 100
    sharpe = (dr.mean() - 0.025 / 252) / max(dr.std(), 1e-10) * (252 ** 0.5)
    n_trades = len(eng.trades)
    n_wins = sum(1 for t in eng.trades if t.pnl > 0)
    wr = n_wins / max(n_trades, 1) * 100
    total_pnl = sum(t.pnl for t in eng.trades)
    total_comm = sum(t.commission for t in eng.trades)
    avg_win = sum(t.pnl for t in eng.trades if t.pnl > 0) / max(n_wins, 1)
    avg_loss = abs(sum(t.pnl for t in eng.trades if t.pnl < 0) / max(n_trades - n_wins, 1))
    pf = avg_win / max(avg_loss, 0.01)

    # 持仓率
    bd = sum(int((s == 1.0).sum()) for s in sm.values())
    td = sum(len(s) for s in sm.values())
    pos_rate = bd / max(td, 1) * 100

    # CSI300 regime distribution
    if "000300.SH" in dm:
        hs300 = dm["000300.SH"]
        hs_c = hs300["close"]
        ma200 = hs_c.rolling(200).mean()
        dev = (hs_c - ma200) / ma200 * 100
        score = pd.Series(50.0, index=hs_c.index)
        score[dev > 20] += (dev[dev > 20] - 20) * 1.25
        score[dev > 5] += dev.clip(upper=20) * 1.0
        score[dev < -15] -= (-dev[dev < -15] - 15).clip(upper=25) * 1.25
        score[dev < 0] += dev * 0.5
        trend = score.clip(0, 100)
        above = hs_c > ma200
        in_range = (hs_c.index >= t0) & (hs_c.index <= t1)
        n_days = in_range.sum()
        bull_pct = (above[in_range]).sum() / max(n_days, 1) * 100
        bear_pct = ((~above[in_range]) & (trend[in_range] < 35)).sum() / max(n_days, 1) * 100
        weak_pct = 100 - bull_pct - bear_pct
    else:
        bull_pct = bear_pct = weak_pct = 0

    return {
        "total_ret": total_ret, "bench_ret": bench_ret, "alpha": total_ret - bench_ret,
        "ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe, "max_dd": dd,
        "trades": n_trades, "wr": wr, "pf": pf, "pnl": total_pnl, "comm": total_comm,
        "pos_rate": pos_rate,
        "regime_bull": bull_pct, "regime_weak": weak_pct, "regime_bear": bear_pct,
    }


def main():
    print("=" * 70)
    print(f"NQP 近五年回测 (2021-07 → 2026-07) — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 70)

    # ── 一次性加载五年数据 ──
    print("\n[加载] 全周期数据 (2020-01 → 2026-07)...")
    loader = DataLoader()
    dm_v3 = loader.fetch(codes=POOL, start_date=DATA_START, end_date=FULL_END, interval="1D")
    dm_both = loader.fetch(codes=POOL_HS, start_date=DATA_START, end_date=FULL_END, interval="1D")
    print(f"  V3数据: {len(dm_v3)}/{len(POOL)} 只")
    print(f"  V5/V6数据: {len(dm_both)}/{len(POOL_HS)} 只 (含沪深300)")

    # ── 年度切片 ──
    years = [
        ("2021 H2", "2021-07-01", "2021-12-31"),
        ("2022",    "2022-01-01", "2022-12-31"),
        ("2023",    "2023-01-01", "2023-12-31"),
        ("2024",    "2024-01-01", "2024-12-31"),
        ("2025",    "2025-01-01", "2025-12-31"),
        ("2026 H1", "2026-01-01", "2026-07-18"),
    ]

    all_results = []
    for label, t0, t1 in years:
        print(f"\n  [{label}] {t0} → {t1}")
        r3 = run_backtest(t0, t1, V3, dm_v3)
        r5 = run_backtest(t0, t1, V5, dm_both)
        r6 = run_backtest(t0, t1, V6, dm_both)
        all_results.append({"label": label, "v3": r3, "v5": r5, "v6": r6})

    # ── 全周期 ──
    print(f"\n  [全周期] {FULL_START} → {FULL_END}")
    r3f = run_backtest(FULL_START, FULL_END, V3, dm_v3)
    r5f = run_backtest(FULL_START, FULL_END, V5, dm_both)
    r6f = run_backtest(FULL_START, FULL_END, V6, dm_both)
    all_results.append({"label": "📊 五年总计", "v3": r3f, "v5": r5f, "v6": r6f})

    # ═══════════════════════════════════════════════════
    # 输出
    # ═══════════════════════════════════════════════════
    print("\n\n")
    print("╔" + "═" * 100 + "╗")
    print("║  📊 NQP 近五年回测 — V3.3 → V5 → V6 逐年对比" + " " * 48 + "║")
    print("╠" + "═" * 100 + "╣")
    print(f"║ {'年份':<12} │ {'V3.3收益':>8} {'V5收益':>8} {'V6收益':>8} │ {'基准':>8} │ {'V6超额':>7} {'V6回撤':>7} {'V6胜率':>6} │ {'市场(bull/weak/bear)':<28} ║")
    print("╠" + "═" * 100 + "╣")

    for ar in all_results:
        label = ar["label"]
        r3, r5, r6 = ar["v3"], ar["v5"], ar["v6"]
        if r3 is None or r6 is None:
            continue

        regime_str = ""
        if r6.get("regime_bull", 0) > 0:
            regime_str += f"🟢{r6['regime_bull']:.0f}%/🟡{r6['regime_weak']:.0f}%/🔴{r6['regime_bear']:.0f}%"

        is_total = "总计" in label
        sep = "╠" if is_total else "║"
        marker = " ▶" if is_total else ""

        print(f"║ {label+marker:<12} │ {r3['total_ret']:>+7.1f}% {r5['total_ret']:>+7.1f}% {r6['total_ret']:>+7.1f}% │ {r6['bench_ret']:>+7.1f}% │ {r6['alpha']:>+6.1f}% {r6['max_dd']:>+6.1f}% {r6['wr']:>5.0f}% │ {regime_str:<28} ║")

    print("╠" + "═" * 100 + "╣")
    # 全周期详细指标
    if r6f:
        print(f"║ 五年Sharpe: V3={r3f['sharpe']:.2f}  V5={r5f['sharpe']:.2f}  V6={r6f['sharpe']:.2f}                             ║")
        print(f"║ 五年交易:   V3={r3f['trades']}笔  V5={r5f['trades']}笔  V6={r6f['trades']}笔                                     ║")
        print(f"║ 五年盈亏:   V3=RMB{r3f['pnl']:,.0f}  V5=RMB{r5f['pnl']:,.0f}  V6=RMB{r6f['pnl']:,.0f}                          ║")
    print("╚" + "═" * 100 + "╝")

    print(f"\n## 解读")
    print(f"- V3.3 原版：全信号不管牛熊 → 牛市能赚、熊市大亏")
    print(f"- V5 二档：RISK-OFF时只留P4/P5 → 熊市少亏、牛市基本不变")
    print(f"- V6 三档：深度熊市直接空仓 → 五种市场状态自适应")
    print(f"- 红色区域(🔴bear)占比越高，V6相对V3的优势越大")


if __name__ == "__main__":
    main()
