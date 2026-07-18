"""NQP V3.3 趋势追踪策略 — 回测用 SignalEngine

复刻 daily_pipeline.py 中的 detect_signals() 逻辑：
  买入 P1-P6 | 卖出 S1-S5 | 卖出信号覆盖买入（交叉去重）

用法：python -m backtest.runner <run_dir>
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class SignalEngine:
    """NQP V3.3 趋势追踪信号引擎。

    对每只标的逐日计算信号：
      1.0 = 持仓（任一买入信号触发且无卖出信号）
      0.0 = 空仓（卖出信号触发或无买入信号）
    """

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals: dict[str, pd.Series] = {}
        for code, df in data_map.items():
            signals[code] = self._generate_one(code, df)
        return signals

    # ── 单标的信号生成 ──────────────────────────────────

    def _generate_one(self, code: str, df: pd.DataFrame) -> pd.Series:
        if len(df) < 250:
            return pd.Series(0.0, index=df.index, name=code)

        close = df["close"]
        high = df.get("high", close)
        low = df.get("low", close)
        volume = df.get("volume", pd.Series(1.0, index=df.index))

        # ── 技术指标 ──
        ma200 = close.rolling(200).mean()
        dev = (close - ma200) / ma200 * 100                     # MA200乖离率（%）
        slope20 = close.pct_change(20) * 100                     # 20日斜率（%）

        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()
        vol_ratio = vol_ma5 / vol_ma20                           # 量比

        # ── P1: MA200回调买点 ──
        p1 = (ma200 > 0) & (dev.abs() < 5) & (slope20 > 0)

        # ── P2: 超跌反弹 ──
        p2 = (dev < -18) & (close.diff() > 0) & (slope20 > -30)

        # ── P3: 突破确认 ──
        p3 = (dev > 2) & (dev < 15) & (vol_ratio > 1.3) & (close > close.shift(5))

        # ── P4: 趋势延续 ──
        p4 = (dev > 5) & (dev < 35) & (slope20 > 3) & (ma200 > 0) & (close > close.shift(10))

        # ── P5: 加速突破 ──
        high_10 = high.rolling(10).max()
        low_10 = low.rolling(10).min()
        range_10_pct = (high_10 - low_10) / close * 100
        p5 = (dev > 10) & (dev < 60) & (slope20 > 5) & (vol_ratio > 1.5) & (range_10_pct < 8)

        # ── P6: 深度价值 ──
        p6 = dev < -30

        # ── S1: 硬止损 ──
        s1 = (ma200 > 0) & (close < ma200 * 0.98)

        # ── S2: MA200破位 ──
        s2 = (ma200 > 0) & (close < ma200) & (slope20 < 0)

        # ── S3: 深度回调 ──
        s3 = (ma200 > 0) & (close < ma200 * 0.95)

        # ── S4: 高位过热（任一档） ──
        s4 = dev > 25

        # ── S5: 趋势弱化 ──
        s5 = (dev < -5) & (close < close.shift(10))

        # ── 汇总：卖出覆盖买入（交叉去重） ──
        buy_any = p1 | p2 | p3 | p4 | p5 | p6
        sell_any = s1 | s2 | s3 | s4 | s5

        # 逐日状态机：卖出优先
        result = pd.Series(0.0, index=df.index, name=code)
        state = 0.0
        for i in range(len(df)):
            if pd.notna(sell_any.iloc[i]) and sell_any.iloc[i]:
                state = 0.0
            elif pd.notna(buy_any.iloc[i]) and buy_any.iloc[i]:
                state = 1.0
            result.iloc[i] = state

        return result
