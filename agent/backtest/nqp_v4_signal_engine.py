"""NQP V4 信号引擎 — 熊市优化版

基于 V3.3 改进：
  1. MA200 趋势过滤：下跌趋势中禁用 P2/P6（不接飞刀）
  2. S1 放宽 + 趋势确认：0.98→0.95，需MA200转负才触发
  3. P2 量能确认：放量>1.2x 才认定恐慌抛售
  4. 最小持仓期：入场后至少持有 5 个交易日
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class SignalEngine:
    """NQP V4 信号引擎（熊市优化版）。"""

    MIN_HOLD_BARS = 5  # 最少持仓交易日

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals: dict[str, pd.Series] = {}
        for code, df in data_map.items():
            signals[code] = self._generate_one(code, df)
        return signals

    def _generate_one(self, code: str, df: pd.DataFrame) -> pd.Series:
        if len(df) < 250:
            return pd.Series(0.0, index=df.index, name=code)

        close = df["close"]
        high = df.get("high", close)
        low = df.get("low", close)
        volume = df.get("volume", pd.Series(1.0, index=df.index))

        # ── 技术指标 ──
        ma200 = close.rolling(200).mean()
        dev = (close - ma200) / ma200 * 100
        slope20 = close.pct_change(20) * 100

        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()
        vol_ratio = vol_ma5 / vol_ma20

        # ★ V4 新增：MA200 20日斜率（判断趋势方向）
        ma200_slope = ma200.pct_change(20) * 100   # MA200 自身变化率
        trend_is_up = ma200_slope > -2              # 容忍-2%以内的横盘

        # ── P1: MA200回调买点 ──
        p1 = (ma200 > 0) & (dev.abs() < 5) & (slope20 > 0) & trend_is_up

        # ── P2: 超跌反弹（V4：趋势向上时才接，且放量确认） ──
        p2 = (dev < -18) & (close.diff() > 0) & (slope20 > -30) & trend_is_up & (vol_ratio > 1.2)

        # ── P3: 突破确认 ──
        p3 = (dev > 2) & (dev < 15) & (vol_ratio > 1.3) & (close > close.shift(5))

        # ── P4: 趋势延续 ──
        p4 = (dev > 5) & (dev < 35) & (slope20 > 3) & (ma200 > 0) & (close > close.shift(10))

        # ── P5: 加速突破 ──
        high_10 = high.rolling(10).max()
        low_10 = low.rolling(10).min()
        range_10_pct = (high_10 - low_10) / close * 100
        p5 = (dev > 10) & (dev < 60) & (slope20 > 5) & (vol_ratio > 1.5) & (range_10_pct < 8)

        # ── P6: 深度价值（V4：趋势向上才接） ──
        p6 = (dev < -30) & trend_is_up

        # ── S1: 硬止损（V4：0.95 + 趋势转负双重确认） ──
        s1 = (ma200 > 0) & (close < ma200 * 0.95) & (ma200_slope < -1)

        # ── S2: MA200破位 ──
        s2 = (ma200 > 0) & (close < ma200) & (slope20 < 0)

        # ── S3: 深度回调 ──
        s3 = (ma200 > 0) & (close < ma200 * 0.95)

        # ── S4: 高位过热 ──
        s4 = dev > 25

        # ── S5: 趋势弱化（V4：需MA200转负确认） ──
        s5 = (dev < -5) & (close < close.shift(10)) & (ma200_slope < 0)

        # ── 汇总 ──
        buy_any = p1 | p2 | p3 | p4 | p5 | p6
        sell_any = s1 | s2 | s3 | s4 | s5

        # ── 状态机（含最小持仓期） ──
        result = pd.Series(0.0, index=df.index, name=code)
        state = 0.0
        bars_held = 0

        for i in range(len(df)):
            if state == 1.0:
                bars_held += 1

            if state == 1.0 and bars_held >= self.MIN_HOLD_BARS:
                # 持有满最小期后才允许卖出
                si = sell_any.iloc[i]
                if pd.notna(si) and si:
                    state = 0.0
                    bars_held = 0
            elif state == 1.0:
                # 最小持有期内，不理卖出信号
                pass
            elif state == 0.0:
                bi = buy_any.iloc[i]
                si = sell_any.iloc[i]
                if pd.notna(si) and si:
                    state = 0.0  # 卖出覆盖买入
                elif pd.notna(bi) and bi:
                    state = 1.0
                    bars_held = 0

            result.iloc[i] = state

        return result
