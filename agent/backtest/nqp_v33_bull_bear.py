"""NQP V3.3 牛熊市对比回测

用法：从 agent/ 目录运行
  cd d:/projects/Vibe-Trading/agent
  python backtest/nqp_v33_bull_bear.py
"""
from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

# 修复 Windows GBK 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import pandas as pd

_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.china_a import ChinaAEngine
from backtest.engines.base import _align, _load_optimizer
from backtest.loaders.tencent_loader import DataLoader as TencentLoader
from backtest.nqp_v33_signal_engine import SignalEngine

# ═══════════════════════════════════════════════════════
# 交易池（与 daily_pipeline.py 一致）
# ═══════════════════════════════════════════════════════
POOL_CODES = [
    "002050.SZ", "601689.SH", "688017.SH", "002747.SZ",
    "300124.SZ", "002472.SZ", "000887.SZ", "603211.SH",
    "300750.SZ", "002594.SZ", "300274.SZ",
    "002371.SZ", "688012.SH", "688981.SH", "603501.SH",
    "603986.SH", "600584.SH", "688072.SH", "301269.SZ",
    "688256.SH", "688041.SH", "002230.SZ", "688111.SH", "603019.SH",
]

# ═══════════════════════════════════════════════════════
# 回测周期定义  (名称, 测试区间, 数据加载起始, 市场环境)
# ═══════════════════════════════════════════════════════
PERIODS = [
    {
        "name":   "🐻 2022 熊市",
        "test_start": "2022-01-01",
        "test_end":   "2022-12-31",
        "data_start": "2020-07-01",   # 提前18个月保证MA200
        "market": "bear",
    },
    {
        "name":   "📊 2023 震荡",
        "test_start": "2023-01-01",
        "test_end":   "2023-12-31",
        "data_start": "2021-07-01",
        "market": "mixed",
    },
    {
        "name":   "🐂 2024-2025 牛市",
        "test_start": "2024-09-24",   # 央行政策底
        "test_end":   "2025-03-31",
        "data_start": "2023-01-01",
        "market": "bull",
    },
]

BASE_CONFIG = {
    "source": "tencent",
    "interval": "1D",
    "initial_cash": 1_000_000,
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax": 0.0005,
    "transfer_fee": 0.00001,
    "slippage": 0.001,
}


def run_one(name: str, test_start: str, test_end: str, data_start: str) -> dict:
    """执行单次回测，返回指标字典。"""
    print(f"\n{'─' * 50}")
    print(f"  {name}: {test_start} → {test_end}")
    print(f"{'─' * 50}")

    config = {**BASE_CONFIG, "codes": POOL_CODES,
              "start_date": test_start, "end_date": test_end}

    # 1. 加载数据（含MA200预热期）
    print("  [1/4] 加载K线...")
    loader = TencentLoader()
    data_map = loader.fetch(codes=POOL_CODES,
                            start_date=data_start, end_date=test_end, interval="1D")
    print(f"        成功: {len(data_map)}/{len(POOL_CODES)} 只")

    if not data_map:
        print("  [ERROR] 无数据")
        return {"error": "no_data"}

    # 2. 生成信号
    print("  [2/4] 生成NQP V3.3信号...")
    se = SignalEngine()
    signal_map = se.generate(data_map)

    buy_days = sum(int((s == 1.0).sum()) for s in signal_map.values())
    total_days = sum(len(s) for s in signal_map.values())
    print(f"        持仓率: {buy_days}/{total_days} ({buy_days/max(total_days,1)*100:.1f}%)")

    # 3. 裁剪数据到回测区间
    for c in list(data_map.keys()):
        df = data_map[c]
        data_map[c] = df[df.index >= test_start]

    codes = [c for c in POOL_CODES if c in data_map and len(data_map[c]) > 20]
    signal_map = {c: s[s.index >= test_start] for c, s in signal_map.items() if c in codes}

    # 4. 运行回测
    print("  [3/4] 运行回测引擎...")
    opt_fn = _load_optimizer(config)
    dates, close_df, target_pos, ret_df = _align(
        data_map, signal_map, codes, optimizer=opt_fn)
    codes = [c for c in codes if c in target_pos.columns]

    engine = ChinaAEngine(config)
    engine._execute_bars(dates, data_map, close_df, target_pos, codes)

    # 5. 计算指标
    print("  [4/4] 计算指标...")
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

    # 基准
    bench_ret_series = ret_df.mean(axis=1)
    bench_total = ((1 + bench_ret_series).prod() - 1) * 100

    daily_ret = eq.pct_change().dropna()
    years = len(daily_ret) / 252
    ann_ret = ((final / initial) ** (1 / max(years, 0.01)) - 1) * 100
    ann_vol = daily_ret.std() * (252 ** 0.5) * 100
    rf_daily = 0.025 / 252
    sharpe = (daily_ret.mean() - rf_daily) / max(daily_ret.std(), 1e-10) * (252 ** 0.5)

    peak = eq.cummax()
    max_dd = ((eq - peak) / peak * 100).min()

    n_trades = len(engine.trades)
    n_wins = sum(1 for t in engine.trades if t.pnl > 0)
    win_rate = n_wins / max(n_trades, 1) * 100
    total_pnl = sum(t.pnl for t in engine.trades)
    total_comm = sum(t.commission for t in engine.trades)

    avg_win = sum(t.pnl for t in engine.trades if t.pnl > 0) / max(n_wins, 1)
    avg_loss = abs(sum(t.pnl for t in engine.trades if t.pnl < 0) / max(n_trades - n_wins, 1))
    pf = avg_win / max(avg_loss, 0.01)

    # 最长连续盈利/亏损
    streak_win = streak_loss = cur_win = cur_loss = 0
    for t in engine.trades:
        if t.pnl > 0:
            cur_win += 1; cur_loss = 0
            streak_win = max(streak_win, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            streak_loss = max(streak_loss, cur_loss)

    return {
        "name": name, "test_start": test_start, "test_end": test_end,
        "final_equity": final, "total_return": total_ret,
        "bench_return": bench_total, "alpha": total_ret - bench_total,
        "ann_return": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "max_dd": max_dd, "n_trades": n_trades, "win_rate": win_rate,
        "profit_factor": pf, "total_pnl": total_pnl, "total_comm": total_comm,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "streak_win": streak_win, "streak_loss": streak_loss,
        "n_codes": len(codes),
    }


def main():
    print("=" * 60)
    print(f"NQP V3.3 牛熊市对比回测 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    results = []
    for p in PERIODS:
        try:
            r = run_one(p["name"], p["test_start"], p["test_end"], p["data_start"])
            results.append(r)
        except Exception as e:
            print(f"  [FAIL] {e}")
            results.append({"name": p["name"], "error": str(e)})

    # ═══════════════════════════════════════════════════
    # 汇总输出
    # ═══════════════════════════════════════════════════
    print("\n\n")
    print("╔" + "═" * 68 + "╗")
    print("║  📊 NQP V3.3 牛熊市表现对比" + " " * 39 + "║")
    print("╠" + "═" * 68 + "╣")

    header = f"║ {'指标':<16} │" + "│".join(f" {r.get('name','?')[:12]:^12} " for r in results) + "║"
    print(header)
    print("╠" + "═" * 68 + "╣")

    def row(label, key, fmt=".1f", unit="%"):
        vals = []
        for r in results:
            v = r.get(key)
            if v is None or isinstance(v, str):
                vals.append(f"{'—':^12}")
            elif fmt == ",.0f":
                vals.append(f"RMB{v:>8,.0f} ")
            elif fmt == ".0f":
                vals.append(f"{v:>12.0f}  ")
            elif fmt == ".2f":
                vals.append(f"{v:>12.2f}  ")
            else:
                vals.append(f"{v:>+11.1f}% ")
        print(f"║ {label:<16} │" + "│".join(vals) + "║")

    row("总收益率",     "total_return")
    row("基准收益率",   "bench_return")
    row("超额收益",     "alpha")
    row("年化收益率",   "ann_return")
    row("年化波动率",   "ann_vol")
    row("Sharpe Ratio", "sharpe", fmt=".2f")
    row("最大回撤",     "max_dd")
    print("╟" + "─" * 68 + "╢")
    row("交易次数",     "n_trades", fmt=".0f")
    row("胜率",         "win_rate")
    row("盈亏比",       "profit_factor", fmt=".2f")
    row("总盈亏(RMB)",  "total_pnl", fmt=",.0f")
    row("手续费(RMB)",  "total_comm", fmt=",.0f")
    row("平均盈利(RMB)","avg_win", fmt=",.0f")
    row("平均亏损(RMB)","avg_loss", fmt=",.0f")
    row("最长连赢",     "streak_win", fmt=".0f")
    row("最长连亏",     "streak_loss", fmt=".0f")
    print("╚" + "═" * 68 + "╝")

    # ── 诊断分析 ──
    print("\n## 牛熊表现诊断")
    for r in results:
        if r.get("error"):
            print(f"\n### {r['name']}: 数据不足，跳过")
            continue
        name = r["name"]
        alpha = r["alpha"]
        dd = r["max_dd"]
        wr = r["win_rate"]

        print(f"\n### {name}")
        print(f"  超额收益: {alpha:+.1f}% | 最大回撤: {dd:+.1f}% | 胜率: {wr:.0f}%")

        if alpha > 5:
            print("  ✅ 策略显著跑赢基准")
        elif alpha > -5:
            print("  ⚠️ 策略与基准持平")
        else:
            print("  ❌ 策略跑输基准")

        if dd > -10:
            print("  🛡️ 回撤控制良好 (<10%)")
        elif dd > -20:
            print("  ⚠️ 回撤中等 (10-20%)")
        else:
            print("  🔴 回撤较大 (>20%)，需优化止损")


if __name__ == "__main__":
    main()
