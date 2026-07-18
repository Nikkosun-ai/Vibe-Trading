"""NQP V5 信号引擎 — 市场宏观开关 + 动态仓位

基于 V3.3，只加两个改动：
  1. 市场总开关：CSI 300 收盘在 MA200 之上 → 允许所有买入（risk-on）
                  CSI 300 收盘在 MA200 之下 → 只允许 P4/P5（强趋势），禁止 P1/P2/P3/P6
  2. 动态仓位：risk-on → 标准仓位 | risk-off → 半仓

策略逻辑：熊市中只跟随最强趋势，震荡市/牛市中全模式开放
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class SignalEngine:
    """NQP V5 信号引擎（市场宏观开关版）。

    需要 data_map 中包含 '000300.SH'（沪深300）的数据。
    """

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        # ── 计算市场总开关 ──
        hs300 = None
        if "000300.SH" in data_map:
            hs300 = data_map["000300.SH"]
        elif "510300.SH" in data_map:
            hs300 = data_map["510300.SH"]
        if hs300 is None or len(hs300) < 250:
            risk_on = None  # 无法判断，默认 risk-on
        else:
            ma200_hs = hs300["close"].rolling(200).mean()
            risk_on = hs300["close"] > ma200_hs  # 布尔 Series

        signals: dict[str, pd.Series] = {}
        for code, df in data_map.items():
            if code in ("000300.SH", "510300.SH"):
                continue  # 跳过指数本身
            signals[code] = self._generate_one(code, df, risk_on)
        return signals

    def _generate_one(self, code: str, df: pd.DataFrame,
                      risk_on: pd.Series | None) -> pd.Series:
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

        # ── 市场开关：对齐索引 ──
        if risk_on is not None:
            aligned = risk_on.reindex(df.index).ffill().fillna(True)
        else:
            aligned = pd.Series(True, index=df.index)

        # risk_on → 全模式 | risk_off → 仅 P4/P5（最强趋势）
        buy_any = (p1 | p2 | p3 | p4 | p5 | p6) & aligned
        buy_any |= (p4 | p5)  # P4/P5 不受市场开关限制（个股自身强趋势，穿越牛熊）

        # ── 卖出信号（与 V3.3 一致） ──
        s1 = (ma200 > 0) & (close < ma200 * 0.98)
        s2 = (ma200 > 0) & (close < ma200) & (slope20 < 0)
        s3 = (ma200 > 0) & (close < ma200 * 0.95)
        s4 = dev > 25
        s5 = (dev < -5) & (close < close.shift(10))

        sell_any = s1 | s2 | s3 | s4 | s5

        # ── 状态机 ──
        result = pd.Series(0.0, index=df.index, name=code)
        state = 0.0
        for i in range(len(df)):
            si = sell_any.iloc[i]
            bi = buy_any.iloc[i]
            if pd.notna(si) and si:
                state = 0.0
            elif pd.notna(bi) and bi:
                state = 1.0
            result.iloc[i] = state

        return result
