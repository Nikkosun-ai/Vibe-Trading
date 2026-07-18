"""NQP V8 — 动态仓位（信号不变，下注大小随市场缩放）

V6 信号逻辑完全保留，只加仓位缩放：
  🟢 牛市 → 信号权重 1.0（满仓参与）
  🟡 弱势 → 信号权重 0.5（半仓试探）
  🔴 熊市 → 信号权重 0.0（空仓）

效果：弱势时少数信号触发 → 自然降低总仓位（如1只触发→50%仓）
      牛市时全信号 → 满仓参与
"""
from __future__ import annotations
import numpy as np, pandas as pd

class SignalEngine:
    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        # ── CSI300 三档判定（同 V6） ──
        hs300 = None
        if "000300.SH" in data_map: hs300 = data_map["000300.SH"]
        elif "510300.SH" in data_map: hs300 = data_map["510300.SH"]

        if hs300 is not None and len(hs300) >= 200:
            c = hs300["close"]
            ma200 = c.rolling(200).mean()
            dev = (c - ma200) / ma200 * 100
            score = pd.Series(50.0, index=c.index)
            score[dev > 20] += (dev[dev > 20] - 20) * 1.25
            score[dev > 5] += dev.clip(upper=20) * 1.0
            score[dev < -15] -= (-dev[dev < -15] - 15).clip(upper=25) * 1.25
            score[dev < 0] += dev * 0.5
            trend = score.clip(0, 100)
            above = c > ma200
            regime = pd.Series("bear", index=c.index)
            regime[above] = "bull"
            regime[(~above) & (trend >= 35)] = "weak"
            # ★ V8: 仓位缩放因子
            weight = pd.Series(0.0, index=c.index)
            weight[regime == "bull"] = 1.0
            weight[regime == "weak"] = 0.5
        else:
            regime = None
            weight = None

        signals = {}
        for code, df in data_map.items():
            if code in ("000300.SH", "510300.SH"): continue
            signals[code] = self._gen(df, regime, weight)
        return signals

    def _gen(self, df, regime, weight):
        if len(df) < 250: return pd.Series(0.0, index=df.index)
        close, high, low = df["close"], df.get("high", df["close"]), df.get("low", df["close"])
        vol = df.get("volume", pd.Series(1.0, index=df.index))
        ma200 = close.rolling(200).mean()
        dev = (close - ma200) / ma200 * 100
        slope = close.pct_change(20) * 100
        vr = vol.rolling(5).mean() / vol.rolling(20).mean()
        h10, l10 = high.rolling(10).max(), low.rolling(10).min()
        r10 = (h10 - l10) / close * 100

        # ── 信号条件（与 V6 完全一致） ──
        p1 = (ma200 > 0) & (dev.abs() < 5) & (slope > 0)
        p2 = (dev < -18) & (close.diff() > 0) & (slope > -30)
        p3 = (dev > 2) & (dev < 15) & (vr > 1.3) & (close > close.shift(5))
        p4 = (dev > 5) & (dev < 35) & (slope > 3) & (ma200 > 0) & (close > close.shift(10))
        p5 = (dev > 10) & (dev < 60) & (slope > 5) & (vr > 1.5) & (r10 < 8)
        p6 = dev < -30

        s1 = (ma200 > 0) & (close < ma200 * 0.98)
        s2 = (ma200 > 0) & (close < ma200) & (slope < 0)
        s3 = (ma200 > 0) & (close < ma200 * 0.95)
        s4 = dev > 25
        s5 = (dev < -5) & (close < close.shift(10))

        # ── 市场过滤（同 V6） ──
        if regime is not None:
            r = regime.reindex(df.index).ffill().fillna("weak")
            w = weight.reindex(df.index).ffill().fillna(0.5)
            is_bull = r == "bull"
            # bull → P1-P6 全开放
            # weak → 仅 P4/P5
            # bear → 无买入
            buy = (p4 | p5) | (is_bull & (p1 | p2 | p3 | p6))
            buy = buy & (r != "bear")
        else:
            buy = p1 | p2 | p3 | p4 | p5 | p6
            w = pd.Series(1.0, index=df.index)

        sell = s1 | s2 | s3 | s4 | s5

        # ★ V8: 信号值 = 仓位权重（0.5/1.0），而非简单 0/1
        result = pd.Series(0.0, index=df.index)
        st = 0.0
        for i in range(len(df)):
            if pd.notna(sell.iloc[i]) and sell.iloc[i]:
                st = 0.0
            elif pd.notna(buy.iloc[i]) and buy.iloc[i]:
                st = float(w.iloc[i])  # ★ 动态权重
            result.iloc[i] = st
        return result
