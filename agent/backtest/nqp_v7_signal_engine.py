"""NQP V7 — 牛市激进 + 弱势保守 + 熊市空仓

V6 基础上，🟢牛市放宽入场/止盈，让利润奔跑
"""
from __future__ import annotations
import numpy as np, pandas as pd

class SignalEngine:
    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        # ── CSI300 三档判定 ──
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
        else:
            regime = None

        signals = {}
        for code, df in data_map.items():
            if code in ("000300.SH", "510300.SH"): continue
            signals[code] = self._gen(df, regime)
        return signals

    def _gen(self, df, regime):
        if len(df) < 250: return pd.Series(0.0, index=df.index)
        close, high, low = df["close"], df.get("high", df["close"]), df.get("low", df["close"])
        vol = df.get("volume", pd.Series(1.0, index=df.index))
        ma200 = close.rolling(200).mean()
        dev = (close - ma200) / ma200 * 100
        slope = close.pct_change(20) * 100
        vr = vol.rolling(5).mean() / vol.rolling(20).mean()
        h10, l10 = high.rolling(10).max(), low.rolling(10).min()
        r10 = (h10 - l10) / close * 100

        # ── 牛市放宽 / 弱势收紧 ──
        if regime is not None:
            r = regime.reindex(df.index).ffill().fillna("bull")
            is_bull = r == "bull"
            is_weak = r == "weak"
            # is_bear = ~is_bull & ~is_weak → no buys

            # P1: 牛市放宽乖离范围
            p1_bull = (ma200 > 0) & (dev.abs() < 8) & (slope > 0)
            p1_weak = (ma200 > 0) & (dev.abs() < 5) & (slope > 0)

            # P2: 牛市放宽量比
            p2_bull = (dev < -18) & (close.diff() > 0) & (slope > -30)
            p2_weak = (dev < -18) & (close.diff() > 0) & (slope > -30) & (vr > 1.2)

            # P3: 牛市放宽确认
            p3_bull = (dev > 2) & (dev < 15) & (vr > 1.15) & (close > close.shift(3))
            p3_weak = (dev > 2) & (dev < 15) & (vr > 1.3) & (close > close.shift(5))

            # P4: 牛市放宽斜率
            p4_bull = (dev > 5) & (dev < 35) & (slope > 2) & (ma200 > 0) & (close > close.shift(10))
            p4_weak = (dev > 5) & (dev < 35) & (slope > 3) & (ma200 > 0) & (close > close.shift(10))

            # P5: 不变
            p5 = (dev > 10) & (dev < 60) & (slope > 5) & (vr > 1.5) & (r10 < 8)

            # P6: 牛市放宽
            p6_bull = dev < -30
            p6_weak = (dev < -30) & (slope > -20)

            # S1: 牛市放宽止损（让利润奔跑）
            s1_bull = (ma200 > 0) & (close < ma200 * 0.94)
            s1_weak = (ma200 > 0) & (close < ma200 * 0.98)

            # S2: 不变
            s2 = (ma200 > 0) & (close < ma200) & (slope < 0)
            # S3: 不变
            s3 = (ma200 > 0) & (close < ma200 * 0.95)
            # S4: 牛市放宽过热阈值
            s4_bull = dev > 35
            s4_weak = dev > 25
            # S5: 不变
            s5 = (dev < -5) & (close < close.shift(10))

            # 组装
            p1 = is_bull & p1_bull | is_weak & p1_weak
            p2 = is_bull & p2_bull | is_weak & p2_weak
            p3 = is_bull & p3_bull | is_weak & p3_weak
            p4 = is_bull & p4_bull | is_weak & p4_weak
            p6 = is_bull & p6_bull | is_weak & p6_weak
            s1 = is_bull & s1_bull | is_weak & s1_weak
            s4 = is_bull & s4_bull | is_weak & s4_weak

            buy = p1 | p2 | p3 | p4 | p5 | p6
            sell = s1 | s2 | s3 | s4 | s5
            # bear → no buys
            buy = buy & ~(r == "bear")
        else:
            buy = (ma200>0)&(dev.abs()<5)&(slope>0) | \
                  (dev<-18)&(close.diff()>0)&(slope>-30) | \
                  (dev>2)&(dev<15)&(vr>1.3)&(close>close.shift(5)) | \
                  (dev>5)&(dev<35)&(slope>3)&(ma200>0)&(close>close.shift(10)) | \
                  (dev>10)&(dev<60)&(slope>5)&(vr>1.5)&(r10<8) | \
                  (dev<-30)
            sell = (ma200>0)&(close<ma200*0.98) | \
                   (ma200>0)&(close<ma200)&(slope<0) | \
                   (ma200>0)&(close<ma200*0.95) | \
                   (dev>25) | \
                   (dev<-5)&(close<close.shift(10))

        result = pd.Series(0.0, index=df.index)
        st = 0.0
        for i in range(len(df)):
            if pd.notna(sell.iloc[i]) and sell.iloc[i]: st = 0.0
            elif pd.notna(buy.iloc[i]) and buy.iloc[i]: st = 1.0
            result.iloc[i] = st
        return result
