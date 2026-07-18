"""NQP V3.3 策略 2023年度回测

用法：从 agent/ 目录运行
  cd d:/projects/Vibe-Trading/agent
  python backtest/nqp_v33_2023_backtest.py

输出：终端打印关键指标 + artifacts/ 目录保存详细数据
"""
from __future__ import annotations

import json
import sys
import io
from datetime import datetime
from pathlib import Path

# 修复 Windows GBK 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import pandas as pd

# Ensure agent/ is on sys.path
_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.china_a import ChinaAEngine
from backtest.loaders.tencent_loader import DataLoader as TencentLoader
from backtest.nqp_v33_signal_engine import SignalEngine

# ═══════════════════════════════════════════════════════
# 交易池（daily_pipeline.py 24只）
# ═══════════════════════════════════════════════════════
POOL_CODES = [
    # 机器人 (8只)
    "002050.SZ", "601689.SH", "688017.SH", "002747.SZ",
    "300124.SZ", "002472.SZ", "000887.SZ", "603211.SH",
    # 新能源 (3只)
    "300750.SZ", "002594.SZ", "300274.SZ",
    # 半导体 (8只)
    "002371.SZ", "688012.SH", "688981.SH", "603501.SH",
    "603986.SH", "600584.SH", "688072.SH", "301269.SZ",
    # AI/算力 (5只)
    "688256.SH", "688041.SH", "002230.SZ", "688111.SH", "603019.SH",
]

CONFIG = {
    "codes": POOL_CODES,
    "start_date": "2023-01-01",
    "end_date": "2023-12-31",
    "source": "tencent",
    "interval": "1D",
    "engine": "daily",
    "initial_cash": 1_000_000,       # 100万初始资金
    "commission_rate": 0.00025,      # 万2.5 佣金
    "commission_min": 5.0,           # 最低5元
    "stamp_tax": 0.0005,             # 万5 印花税（卖出）
    "transfer_fee": 0.00001,         # 万0.1 过户费
    "slippage": 0.001,              # 0.1% 滑点
}


def main():
    print("=" * 60)
    print(f"NQP V3.3 2023年度回测 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"交易池: {len(POOL_CODES)} 只标的")
    print(f"区间: {CONFIG['start_date']} → {CONFIG['end_date']}")
    print(f"初始资金: RMB{CONFIG['initial_cash']:,}")
    print(f"数据源: 腾讯财经 (免费, qfq前复权)")
    print()

    # 1. 加载数据
    print("[1/3] 加载K线数据...")
    loader = TencentLoader()
    data_map = loader.fetch(
        codes=CONFIG["codes"],
        start_date="2022-07-01",  # 从2022下半年开始，保证MA200有足够历史
        end_date=CONFIG["end_date"],
        interval="1D",
    )
    n_loaded = len(data_map)
    n_missing = len(CONFIG["codes"]) - n_loaded
    print(f"  成功加载: {n_loaded}/{len(CONFIG['codes'])} 只")
    if n_missing > 0:
        missing = set(CONFIG["codes"]) - set(data_map.keys())
        print(f"  缺失({n_missing}只): {', '.join(sorted(missing))}")

    if not data_map:
        print("[ERROR] 无数据可用，终止")
        return 1

    # 2. 生成信号
    print("[2/3] 生成NQP V3.3信号...")
    signal_engine = SignalEngine()
    signal_map = signal_engine.generate(data_map)

    # 统计信号
    buy_days = sell_days = 0
    for s in signal_map.values():
        buy_days += int((s == 1.0).sum())
        sell_days += int((s == 0.0).sum())
    total_days = sum(len(s) for s in signal_map.values())
    print(f"  信号统计: 持仓 {buy_days} 天 ({buy_days/max(total_days,1)*100:.1f}%) | 空仓 {sell_days - buy_days} 天")

    # 3. 运行回测
    print("[3/3] 运行回测引擎...")
    run_dir = _AGENT_DIR / "backtest" / "runs" / "nqp_v33_2023"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)
    (run_dir / "code").mkdir(exist_ok=True)

    engine = ChinaAEngine(CONFIG)

    # 由于 run_backtest 内部调用 loader.fetch() 和 signal_engine.generate()，
    # 而我们已经有了 data_map 和 signal_map，需要绕过。
    # 直接手动执行回测流程。

    codes = list(data_map.keys())
    start_date = CONFIG["start_date"]
    end_date = CONFIG["end_date"]

    # 裁剪到回测区间
    for c in codes:
        df = data_map[c]
        data_map[c] = df[df.index >= start_date]

    # 使用 engine 的 _align 和 _execute_bars
    from backtest.engines.base import _align, _load_optimizer

    opt_fn = _load_optimizer(CONFIG)
    dates, close_df, target_pos, ret_df = _align(
        data_map, signal_map, codes, optimizer=opt_fn,
    )
    codes = [c for c in codes if c in target_pos.columns]

    engine._execute_bars(dates, data_map, close_df, target_pos, codes)

    # ── 计算指标 ──
    equity_series = pd.Series(
        [s.equity for s in engine.equity_snapshots],
        index=[s.timestamp for s in engine.equity_snapshots],
    )
    equity_series.index = pd.to_datetime(equity_series.index)

    # 基准：等权持有
    bench_ret = ret_df.mean(axis=1)
    bench_equity = engine.initial_capital * (1 + bench_ret).cumprod()

    # ── 核心指标 ──
    initial = engine.initial_capital
    final = equity_series.iloc[-1] if len(equity_series) > 0 else initial

    total_return = (final / initial - 1) * 100
    bench_return = (bench_equity.iloc[-1] / initial - 1) * 100 if len(bench_equity) > 0 else 0

    # 年化收益率
    days = len(equity_series)
    years = days / 252
    ann_return = ((final / initial) ** (1 / max(years, 0.01)) - 1) * 100

    # 日收益率
    daily_ret = equity_series.pct_change().dropna()
    ann_vol = daily_ret.std() * (252 ** 0.5) * 100

    # Sharpe（假设无风险利率 2.5%）
    rf_daily = 0.025 / 252
    sharpe = (daily_ret.mean() - rf_daily) / daily_ret.std() * (252 ** 0.5)

    # 最大回撤
    peak = equity_series.cummax()
    drawdown = (equity_series - peak) / peak * 100
    max_dd = drawdown.min()

    # 胜率
    n_trades = len(engine.trades)
    n_wins = sum(1 for t in engine.trades if t.pnl > 0)
    win_rate = n_wins / max(n_trades, 1) * 100

    # 盈亏比
    avg_win = sum(t.pnl for t in engine.trades if t.pnl > 0) / max(n_wins, 1)
    avg_loss = abs(sum(t.pnl for t in engine.trades if t.pnl < 0) / max(n_trades - n_wins, 1))
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    total_pnl = sum(t.pnl for t in engine.trades)
    total_comm = sum(t.commission for t in engine.trades)

    # ── 输出 ──
    print()
    print("=" * 60)
    print("📊 回测结果")
    print("=" * 60)
    print(f"  初始资金:       RMB{initial:>12,.0f}")
    print(f"  最终权益:       RMB{final:>12,.0f}")
    print(f"  总收益率:       {total_return:>+11.2f}%")
    print(f"  基准(等权持有): {bench_return:>+11.2f}%")
    print(f"  超额收益:       {total_return - bench_return:>+11.2f}%")
    print(f"  ─────────────────────────────")
    print(f"  年化收益率:     {ann_return:>+11.2f}%")
    print(f"  年化波动率:     {ann_vol:>11.2f}%")
    print(f"  Sharpe Ratio:   {sharpe:>11.2f}")
    print(f"  最大回撤:       {max_dd:>+11.2f}%")
    print(f"  ─────────────────────────────")
    print(f"  交易次数:       {n_trades:>11}")
    print(f"  胜率:           {win_rate:>10.1f}%")
    print(f"  平均盈利:       RMB{avg_win:>11,.0f}")
    print(f"  平均亏损:       RMB{avg_loss:>11,.0f}")
    print(f"  盈亏比:         {profit_factor:>11.2f}")
    print(f"  ─────────────────────────────")
    print(f"  总盈亏:         RMB{total_pnl:>+11,.0f}")
    print(f"  总手续费:       RMB{total_comm:>11,.0f}")
    print(f"  净盈亏:         RMB{total_pnl - total_comm:>+11,.0f}")

    # ── 逐年分拆 ──
    print()
    print("── 月度收益 ──")
    monthly = equity_series.resample("ME").last().pct_change() * 100
    for m, r in monthly.items():
        if pd.notna(r):
            bar = "█" * max(1, int(abs(r) / 2))
            print(f"  {m.strftime('%Y-%m')}: {r:>+7.2f}%  {bar}")

    # ── 保存 artifacts ──
    artifacts_dir = run_dir / "artifacts"
    equity_df = pd.DataFrame({
        "equity": equity_series,
        "drawdown": drawdown,
        "benchmark_equity": bench_equity,
    }, index=equity_series.index)
    equity_df.index.name = "timestamp"
    equity_df.to_csv(artifacts_dir / "equity.csv")

    trade_rows = []
    for t in engine.trades:
        trade_rows.append({
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_date": str(t.entry_time.date()) if hasattr(t.entry_time, "date") else str(t.entry_time),
            "exit_date": str(t.exit_time.date()) if hasattr(t.exit_time, "date") else str(t.exit_time),
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "size": t.size,
            "pnl": round(t.pnl, 2),
            "pnl_pct": round(t.pnl_pct, 2),
            "exit_reason": t.exit_reason,
            "holding_days": t.holding_bars,
        })
    pd.DataFrame(trade_rows).to_csv(artifacts_dir / "trades.csv", index=False)

    print()
    print(f"📁 详细数据已保存到: {artifacts_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
