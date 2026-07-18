"""NQP V3.3 vs V4 牛熊市优化对比

用法：从 agent/ 目录运行
  cd d:/projects/Vibe-Trading/agent
  python backtest/nqp_v4_compare.py
"""
from __future__ import annotations

import io
import sys
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
from backtest.nqp_v4_signal_engine import SignalEngine as SEv4

POOL_CODES = [
    "002050.SZ", "601689.SH", "688017.SH", "002747.SZ",
    "300124.SZ", "002472.SZ", "000887.SZ", "603211.SH",
    "300750.SZ", "002594.SZ", "300274.SZ",
    "002371.SZ", "688012.SH", "688981.SH", "603501.SH",
    "603986.SH", "600584.SH", "688072.SH", "301269.SZ",
    "688256.SH", "688041.SH", "002230.SZ", "688111.SH", "603019.SH",
]

PERIODS = [
    {
        "name": "🐻 2022 熊市",
        "test_start": "2022-01-01", "test_end": "2022-12-31",
        "data_start": "2020-07-01",
    },
    {
        "name": "📊 2023 震荡",
        "test_start": "2023-01-01", "test_end": "2023-12-31",
        "data_start": "2021-07-01",
    },
    {
        "name": "🐂 2024-25 牛市",
        "test_start": "2024-09-24", "test_end": "2025-03-31",
        "data_start": "2023-01-01",
    },
]

BASE_CONFIG = {
    "source": "tencent", "interval": "1D",
    "initial_cash": 1_000_000,
    "commission_rate": 0.00025, "commission_min": 5.0,
    "stamp_tax": 0.0005, "transfer_fee": 0.00001, "slippage": 0.001,
}


def run_one(name: str, test_start: str, test_end: str, data_start: str,
            signal_engine, label: str) -> dict:
    print(f"    [{label}] 信号生成...")
    se = signal_engine()
    signal_map = se.generate(data_map_global[name])

    # 裁剪
    dm = {}
    for c, df in data_map_global[name].items():
        dm[c] = df[df.index >= test_start]
    codes = [c for c in POOL_CODES if c in dm and len(dm[c]) > 20]
    sm = {c: s[s.index >= test_start] for c, s in signal_map.items() if c in codes}

    buy_days = sum(int((s == 1.0).sum()) for s in sm.values())
    total_days = sum(len(s) for s in sm.values())
    pos_rate = buy_days / max(total_days, 1) * 100

    config = {**BASE_CONFIG, "codes": POOL_CODES,
              "start_date": test_start, "end_date": test_end}

    opt_fn = _load_optimizer(config)
    dates, close_df, target_pos, ret_df = _align(dm, sm, codes, optimizer=opt_fn)
    codes = [c for c in codes if c in target_pos.columns]

    engine = ChinaAEngine(config)
    engine._execute_bars(dates, dm, close_df, target_pos, codes)

    eq = pd.Series(
        [s.equity for s in engine.equity_snapshots],
        index=[s.timestamp for s in engine.equity_snapshots],
    )
    eq.index = pd.to_datetime(eq.index)

    if len(eq) < 5:
        return {"error": "too_few_bars"}

    initial = engine.initial_capital
    final = eq.iloc[-1]
    total_ret = (final / initial - 1) * 100
    bench_total = ((1 + ret_df.mean(axis=1)).prod() - 1) * 100

    daily_ret = eq.pct_change().dropna()
    years = max(len(daily_ret) / 252, 0.01)
    ann_ret = ((final / initial) ** (1 / years) - 1) * 100
    ann_vol = daily_ret.std() * (252 ** 0.5) * 100
    rf_daily = 0.025 / 252
    sharpe = (daily_ret.mean() - rf_daily) / max(daily_ret.std(), 1e-10) * (252 ** 0.5)
    max_dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()

    n_trades = len(engine.trades)
    n_wins = sum(1 for t in engine.trades if t.pnl > 0)
    win_rate = n_wins / max(n_trades, 1) * 100
    total_pnl = sum(t.pnl for t in engine.trades)
    total_comm = sum(t.commission for t in engine.trades)

    avg_win = sum(t.pnl for t in engine.trades if t.pnl > 0) / max(n_wins, 1)
    avg_loss = abs(sum(t.pnl for t in engine.trades if t.pnl < 0) / max(n_trades - n_wins, 1))
    pf = avg_win / max(avg_loss, 0.01)

    streak_win = streak_loss = cur_win = cur_loss = 0
    for t in engine.trades:
        if t.pnl > 0:
            cur_win += 1; cur_loss = 0
            streak_win = max(streak_win, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            streak_loss = max(streak_loss, cur_loss)

    return {
        "total_return": total_ret, "bench_return": bench_total,
        "alpha": total_ret - bench_total,
        "ann_return": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "max_dd": max_dd, "n_trades": n_trades, "win_rate": win_rate,
        "profit_factor": pf, "total_pnl": total_pnl, "total_comm": total_comm,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "streak_win": streak_win, "streak_loss": streak_loss,
        "pos_rate": pos_rate,
    }


# ── 全局数据缓存（加载一次，跑两个引擎） ──
data_map_global: dict[str, dict] = {}


def main():
    print("=" * 70)
    print(f"NQP V3.3 vs V4 牛熊市对比 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    print()
    print("V4 改进：趋势过滤 | S1放宽(0.98→0.95+趋势确认) | P2量能确认 | 最少持仓5日")
    print()

    # ── 加载所有周期数据（只加载一次） ──
    loader = TencentLoader()
    for p in PERIODS:
        key = p["name"]
        print(f"[加载] {key} ({p['data_start']}→{p['test_end']}) ...")
        data_map_global[key] = loader.fetch(
            codes=POOL_CODES,
            start_date=p["data_start"], end_date=p["test_end"], interval="1D")
        print(f"        {len(data_map_global[key])}/{len(POOL_CODES)} 只")

    # ── 运行所有回测 ──
    all_results = []
    for p in PERIODS:
        key = p["name"]
        print(f"\n{'─' * 50}")
        print(f"  {key}: {p['test_start']} → {p['test_end']}")
        print(f"{'─' * 50}")

        r3 = run_one(key, p["test_start"], p["test_end"], p["data_start"], SEv3, "V3.3")
        r4 = run_one(key, p["test_start"], p["test_end"], p["data_start"], SEv4, "V4")

        all_results.append({"period": key, "v3": r3, "v4": r4})

    # ═══════════════════════════════════════════════════
    # 输出
    # ═══════════════════════════════════════════════════
    METRICS = [
        ("总收益率",     "total_return", "%", ".1f"),
        ("基准收益率",   "bench_return", "%", ".1f"),
        ("✨ 超额收益",  "alpha",        "%", ".1f"),
        ("年化收益率",   "ann_return",   "%", ".1f"),
        ("Sharpe",       "sharpe",       "",  ".2f"),
        ("最大回撤",     "max_dd",       "%", ".1f"),
        ("胜率",         "win_rate",     "%", ".1f"),
        ("盈亏比",       "profit_factor","",  ".2f"),
        ("交易次数",     "n_trades",     "",  ".0f"),
        ("持仓率",       "pos_rate",     "%", ".1f"),
        ("最长连赢",     "streak_win",   "",  ".0f"),
        ("最长连亏",     "streak_loss",  "",  ".0f"),
    ]

    for ar in all_results:
        period = ar["period"]
        v3, v4 = ar["v3"], ar["v4"]
        if v3.get("error") or v4.get("error"):
            continue

        print(f"\n{'─' * 70}")
        print(f"  {period}")
        print(f"{'─' * 70}")
        print(f"  {'指标':<16} {'V3.3':>10} {'V4':>10} {'改善':>10}")
        print(f"  {'─' * 46}")

        for label, key, unit, fmt in METRICS:
            v3v = v3.get(key)
            v4v = v4.get(key)
            if v3v is None or v4v is None:
                continue

            if unit == "%":
                v3s = f"{v3v:+.1f}%"
                v4s = f"{v4v:+.1f}%"
                diff = v4v - v3v
                if key in ("max_dd", "streak_loss"):
                    diff_s = f"{'✅' if diff > 0 else '❌' if diff < 0 else '—'} {diff:+.1f}%"
                elif key in ("total_return", "alpha", "ann_return", "sharpe", "win_rate", "profit_factor", "pos_rate"):
                    diff_s = f"{'✅' if diff > 0 else '❌' if diff < 0 else '—'} {diff:+.1f}{'%' if unit == '%' else ''}"
                else:
                    diff_s = f"{diff:+.1f}%"
            elif unit == "":
                if fmt == ".2f":
                    v3s = f"{v3v:.2f}"
                    v4s = f"{v4v:.2f}"
                else:
                    v3s = f"{v3v:.0f}"
                    v4s = f"{v4v:.0f}"
                diff = v4v - v3v
                if key == "streak_loss":
                    diff_s = f"{'✅' if diff <= 0 else '❌'} {diff:+.0f}"
                else:
                    diff_s = f"{'✅' if diff >= 0 else '❌'} {diff:+.0f}"
            else:
                v3s = f"{v3v:,}"
                v4s = f"{v4v:,}"
                diff_s = "—"

            print(f"  {label:<16} {v3s:>10} {v4s:>10} {diff_s:>10}")

        # 摘要
        alpha3 = v3["alpha"]
        alpha4 = v4["alpha"]
        dd3 = v3["max_dd"]
        dd4 = v4["max_dd"]
        print(f"\n  📊 超额: {alpha3:+.1f}% → {alpha4:+.1f}% | 回撤: {dd3:+.1f}% → {dd4:+.1f}%")

    print(f"\n{'═' * 70}")
    print("  V4 核心改进：")
    print("  1. MA200 下跌趋势中禁用 P2/P6 → 熊市不接飞刀")
    print("  2. S1 止损从 0.98 放宽到 0.95 + MA200趋势转负确认 → 减少假止损")
    print("  3. P2 需放量 >1.2x 确认恐慌抛售")
    print("  4. 入场后至少持有 5 日 → 避免噪音止损")
    print(f"{'═' * 70}")


if __name__ == "__main__":
    main()
