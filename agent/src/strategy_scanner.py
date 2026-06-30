"""策略扫描引擎 — 三层筛选 + 策略路由 + 信号生成."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


class StrategyScanner:
    """策略扫描引擎."""

    def __init__(self):
        self._strategies: Dict[str, dict] = {}
        self._load_strategies()

    def _load_strategies(self):
        if not STRATEGIES_DIR.exists():
            return
        for f in STRATEGIES_DIR.glob("*.json"):
            cfg = json.loads(f.read_text(encoding="utf-8"))
            self._strategies[cfg["id"]] = cfg

    def list_strategies(self) -> List[dict]:
        return [
            {"id": s["id"], "name": s["name"], "version": s["version"]}
            for s in self._strategies.values()
        ]

    def get_strategy(self, strategy_id: str) -> Optional[dict]:
        return self._strategies.get(strategy_id)

    def scan(self, strategy_id: str, data_map: Dict[str, pd.DataFrame],
             positions: Optional[List[dict]] = None,
             total_capital: float = 100000) -> dict:
        cfg = self._strategies.get(strategy_id)
        if not cfg:
            return {"error": f"Strategy '{strategy_id}' not found", "pool": [], "signals": []}

        pool = []
        signals = []
        total = len(data_map)

        # Calculate used capital
        used_capital = 0.0
        if positions:
            for pos in positions:
                used_capital += pos.get("entry_price", 0) * pos.get("quantity", 0)
        available_ratio = 1.0 - (used_capital / total_capital) if total_capital > 0 else 1.0

        for symbol, bars in data_map.items():
            if bars.empty or len(bars) < 200:
                continue

            trend_score = self._calc_trend_score(bars, cfg["layers"]["trend_quality"])
            thresholds = cfg["layers"]["trend_quality"]["thresholds"]
            if trend_score < thresholds["weak"]:
                continue

            adapt_score = self._calc_adapt_score(bars, cfg["layers"]["strategy_fit"])
            volatility = self._calc_volatility(bars)
            is_index = self._is_broad_index(symbol)

            sub = self._route_strategy(
                trend_score, adapt_score, volatility, is_index,
                cfg["sub_strategies"], cfg["layers"]["strategy_fit"]["threshold"]
            )
            if not sub:
                continue

            entry_score = self._calc_entry_score(bars, cfg["layers"]["entry_timing"])

            pool.append({
                "symbol": symbol,
                "name": self._guess_name(symbol),
                "trend_score": round(trend_score, 1),
                "adapt_score": round(adapt_score, 1),
                "entry_score": round(entry_score, 1),
                "volatility": round(volatility * 100, 1),
                "strategy": sub["name"],
                "group": sub.get("label", sub["name"]),
            })

            # Check P0 position cap before generating signal
            active_count = len(positions) if positions else 0
            cap = cfg["risk"]["position_cap"]
            if active_count >= int(1.0 / cap):
                continue  # already at max positions

            signal = self._evaluate_signal(symbol, bars, sub, cfg["risk"])
            if signal:
                signals.append(signal)

        # Apply position cap weighting
        cap = cfg["risk"]["position_cap"]
        buy_count = sum(1 for s in signals if s["type"] == "buy")
        weight = min(cap, 1.0 / max(buy_count, 1))
        for s in signals:
            s["suggested_weight"] = round(weight, 2)

        return {
            "pool": pool,
            "signals": signals,
            "summary": {
                "total_scanned": total,
                "pool_count": len(pool),
                "signal_count": len(signals),
                "scan_time": datetime.now().isoformat(),
                "capital": {"total": total_capital, "used": used_capital,
                            "available_ratio": round(available_ratio, 2)},
            },
        }

    def _calc_trend_score(self, bars: pd.DataFrame, layer_cfg: dict) -> float:
        closes = bars["close"].astype(float)
        if len(closes) < 200:
            return 0.0
        ma200 = closes.rolling(200).mean().iloc[-1]
        current = closes.iloc[-1]
        annual_return = (closes.iloc[-1] / closes.iloc[0]) ** (252 / len(closes)) - 1
        ret_score = min(abs(annual_return) / 0.50, 1.0) * 100
        deviation = (current - ma200) / ma200
        dev_score = min(abs(deviation) / 0.30, 1.0) * 100
        peak = closes.expanding().max()
        drawdown = (closes - peak) / peak
        max_dd = abs(drawdown.min())
        dd_score = max(0, (1.0 - max_dd / 0.50)) * 100
        recent = closes.iloc[-60:]
        x = np.arange(len(recent))
        slope, _ = np.polyfit(x, recent.values, 1)
        norm_slope = slope / recent.iloc[0]
        slope_score = min(abs(norm_slope) / 0.30, 1.0) * 100
        returns = closes.pct_change().dropna()
        annual_vol = returns.std() * np.sqrt(252)
        vol_penalty = max(0, (annual_vol - 0.40) / 0.60) * 100 if annual_vol > 0.40 else 0
        factors = layer_cfg["factors"]
        total = (
            ret_score * factors[0]["weight"]
            + dev_score * factors[1]["weight"]
            + dd_score * factors[2]["weight"]
            + slope_score * factors[3]["weight"]
            - vol_penalty * factors[4]["weight"]
        )
        return round(max(0, min(100, total)), 1)

    def _calc_adapt_score(self, bars: pd.DataFrame, layer_cfg: dict) -> float:
        closes = bars["close"].astype(float)
        peak = closes.expanding().max()
        drawdown = (closes - peak) / peak
        dd_series = drawdown.dropna()
        hist, _ = np.histogram(dd_series, bins=20)
        peak_count = sum(1 for i in range(1, len(hist) - 1)
                         if hist[i] > hist[i - 1] and hist[i] > hist[i + 1])
        bimodal_score = min(peak_count / 3.0, 1.0) * 100
        dd_diff = dd_series.diff().abs()
        gap_score = min(dd_diff.sum() / len(dd_diff) / 0.05, 1.0) * 100
        returns = closes.pct_change().dropna()
        ma60 = closes.rolling(60).mean()
        trend_signal = (closes > ma60).astype(int).diff().abs().sum()
        noise_ratio = trend_signal / len(closes) if len(closes) > 0 else 1.0
        snr_score = max(0, (1.0 - noise_ratio)) * 100
        factors = layer_cfg["factors"]
        return round(
            bimodal_score * factors[0]["weight"]
            + gap_score * factors[1]["weight"]
            + snr_score * factors[2]["weight"], 1)

    def _calc_entry_score(self, bars: pd.DataFrame, layer_cfg: dict) -> float:
        closes = bars["close"].astype(float)
        current = closes.iloc[-1]
        ma200 = closes.rolling(200).mean().iloc[-1]
        ma200_pos = min((current - ma200) / ma200 / 0.30, 1.0) * 100
        peak = closes.expanding().max()
        dd = abs((current - peak.iloc[-1]) / peak.iloc[-1])
        dd_score = (min(dd / 0.20, 1.0) * 80 + 20) if dd > 0 else 30
        returns = closes.pct_change().dropna()
        vol = returns.iloc[-20:].std() * np.sqrt(252)
        vol_score = 100 if 0.15 < vol < 0.40 else 50
        factors = layer_cfg["factors"]
        return round(
            ma200_pos * factors[0]["weight"]
            + dd_score * factors[1]["weight"]
            + vol_score * factors[2]["weight"], 1)

    def _route_strategy(self, trend: float, adapt: float, volatility: float,
                        is_index: bool, sub_strategies: list, fit_threshold: float
                        ) -> Optional[dict]:
        if is_index and trend >= 50:
            for s in sub_strategies:
                if "Confirm3" in s["name"]:
                    return s
        if adapt >= fit_threshold and 50 <= trend < 65:
            for s in sub_strategies:
                if "BearFlat" in s["name"]:
                    return s
        if adapt < fit_threshold and trend >= 65 and volatility <= 0.35:
            for s in sub_strategies:
                if "Pure MA200" == s["name"]:
                    return s
        if adapt < fit_threshold and trend >= 65 and volatility > 0.35:
            for s in sub_strategies:
                if "VolGate" in s["name"]:
                    return s
        return None

    def _evaluate_signal(self, symbol: str, bars: pd.DataFrame, sub: dict, risk: dict) -> Optional[dict]:
        closes = bars["close"].astype(float)
        current = closes.iloc[-1]
        ma200 = closes.rolling(200).mean().iloc[-1]
        name = sub["name"]

        if "BearFlat" in name:
            peak = closes.expanding().max()
            dd = abs((current - peak.iloc[-1]) / peak.iloc[-1])
            if current > ma200 and dd > 0.20:
                return {"symbol": symbol, "type": "buy", "strategy": name,
                        "reason": f"回撤{dd:.1%}触发DD20", "price": round(current, 2)}
            if current < ma200:
                return {"symbol": symbol, "type": "sell", "strategy": name,
                        "reason": "跌破MA200", "price": round(current, 2)}
        elif "Pure MA200" == name:
            prev_close = closes.iloc[-2] if len(closes) >= 2 else current
            prev_ma200 = closes.iloc[:-1].rolling(200).mean().iloc[-1] if len(closes) > 200 else ma200
            if prev_close <= prev_ma200 and current > ma200:
                return {"symbol": symbol, "type": "buy", "strategy": name,
                        "reason": "上穿MA200", "price": round(current, 2)}
            if current < ma200:
                return {"symbol": symbol, "type": "sell", "strategy": name,
                        "reason": "跌破MA200", "price": round(current, 2)}
        elif "VolGate" in name:
            returns = closes.pct_change().dropna()
            vol = float(returns.iloc[-20:].std() * np.sqrt(252))
            if current > ma200 and vol < 0.03:
                return {"symbol": symbol, "type": "buy", "strategy": name,
                        "reason": f"波动率{vol:.1%}+站上MA200", "price": round(current, 2)}
            if current < ma200 or vol > 0.05:
                return {"symbol": symbol, "type": "sell", "strategy": name,
                        "reason": f"波动率{vol:.1%}触发离场", "price": round(current, 2)}
        elif "Confirm3" in name:
            above_count = sum(1 for i in range(-min(3, len(closes)), 0)
                              if closes.iloc[i] > ma200)
            if above_count >= 3:
                return {"symbol": symbol, "type": "buy", "strategy": name,
                        "reason": f"连续{above_count}日站稳MA200", "price": round(current, 2)}
            if current < ma200:
                return {"symbol": symbol, "type": "sell", "strategy": name,
                        "reason": "跌破MA200", "price": round(current, 2)}
        return None

    def _calc_volatility(self, bars: pd.DataFrame) -> float:
        returns = bars["close"].astype(float).pct_change().dropna()
        return round(float(returns.std() * np.sqrt(252)), 4)

    def _is_broad_index(self, symbol: str) -> bool:
        return any(x in symbol for x in ["510300", "510500", "159915", "510050"])

    def _guess_name(self, symbol: str) -> str:
        return symbol

    def check_surge_stop(self, symbol: str, close_series: pd.Series) -> Optional[dict]:
        if len(close_series) < 5:
            return None
        surge = (close_series.iloc[-1] / close_series.iloc[-5]) - 1
        if surge > 0.15:
            return {
                "symbol": symbol, "type": "sell", "strategy": "P1急涨止盈",
                "reason": f"5日涨幅{surge:.1%}触发强制止盈",
                "price": round(float(close_series.iloc[-1]), 2),
            }
        return None


scanner = StrategyScanner()
