"""新闻分析引擎 — 多源采集 + Vibe-Trading AI 分析."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    source: str
    url: str = ""
    published_at: str = ""
    symbols: List[str] = field(default_factory=list)
    impact: str = "neutral"
    analysis: str = ""
    strategy_advice: str = ""


@dataclass
class NewsReport:
    date: str
    generated_at: str
    symbols_covered: List[str] = field(default_factory=list)
    positive: List[dict] = field(default_factory=list)
    negative: List[dict] = field(default_factory=list)
    neutral: List[dict] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "positive_count": len(self.positive),
            "negative_count": len(self.negative),
            "neutral_count": len(self.neutral),
            "symbols_covered": self.symbols_covered,
            "items": {
                "positive": self.positive,
                "negative": self.negative,
                "neutral": self.neutral,
            },
            "summary": self.summary,
        }


class NewsAnalyzer:
    """多源新闻采集 + Vibe-Trading AI 分析."""

    def __init__(self):
        self._last_report: Optional[NewsReport] = None

    def collect_news(self, symbols: List[str]) -> List[NewsItem]:
        all_news: List[NewsItem] = []
        # Priority: 巨潮公告 > 东财新闻 > 同花顺热点
        for fetcher in [self._fetch_juchao, self._fetch_eastmoney, self._fetch_tonghuashun]:
            try:
                items = fetcher(symbols)
                all_news.extend(items)
            except Exception as e:
                logger.debug(f"News source fetch skipped: {e}")
        return all_news

    def analyze_with_ai(self, news_items: List[NewsItem], pool: List[dict],
                        positions: Optional[List[dict]] = None) -> NewsReport:
        report = NewsReport(
            date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=datetime.now().isoformat(),
        )
        symbols_seen = set()
        for item in news_items:
            symbol = item.symbols[0] if item.symbols else ""
            symbols_seen.add(symbol)
            pool_info = ""
            for p in pool:
                if p.get("symbol") == symbol:
                    pool_info = (f"当前策略：{p.get('strategy')}, "
                                 f"趋势分：{p.get('trend_score')}, "
                                 f"入场评分：{p.get('entry_score')}")
                    break
            # Check if currently held
            is_held = False
            if positions:
                for pos in positions:
                    if pos.get("symbol") == symbol:
                        is_held = True
                        break
            prompt = self._build_analysis_prompt(item, pool_info, is_held)
            analysis_result = self._call_ai_agent(prompt)
            item.impact = analysis_result.get("impact", "neutral")
            item.analysis = analysis_result.get("analysis", "")
            item.strategy_advice = analysis_result.get("strategy_advice", "")
            entry = {
                "title": item.title, "symbol": symbol, "source": item.source,
                "impact": item.impact, "analysis": item.analysis,
                "strategy_advice": item.strategy_advice,
            }
            if item.impact == "positive":
                report.positive.append(entry)
            elif item.impact == "negative":
                report.negative.append(entry)
            else:
                report.neutral.append(entry)

        report.symbols_covered = sorted(symbols_seen)
        report.summary = self._generate_summary(report)
        self._last_report = report
        return report

    def get_latest_report(self) -> Optional[dict]:
        if self._last_report:
            return self._last_report.to_dict()
        return None

    def _fetch_eastmoney(self, symbols: List[str]) -> List[NewsItem]:
        items = []
        for symbol in symbols:
            items.append(NewsItem(
                title=f"[东财] {symbol} 相关新闻获取中...",
                source="eastmoney", symbols=[symbol],
                published_at=datetime.now().isoformat()))
        return items

    def _fetch_juchao(self, symbols: List[str]) -> List[NewsItem]:
        items = []
        for symbol in symbols:
            items.append(NewsItem(
                title=f"[巨潮] {symbol} 公告获取中...",
                source="juchao", symbols=[symbol],
                published_at=datetime.now().isoformat()))
        return items

    def _fetch_tonghuashun(self, symbols: List[str]) -> List[NewsItem]:
        return []

    def _build_analysis_prompt(self, item: NewsItem, pool_info: str, is_held: bool) -> str:
        symbol = item.symbols[0] if item.symbols else "未知"
        held_info = "当前持仓中" if is_held else "未持仓"
        return f"""分析以下新闻对股票的影响：

股票：{symbol}
新闻标题：{item.title}
新闻来源：{item.source}
持仓状态：{held_info}
{pool_info}

请判断：
1. 该新闻对股价短期影响是利好、利空还是中性？
2. 对该股票当前策略信号的影响是什么？
3. 给出具体的操作建议。

返回JSON：{{"impact": "positive/negative/neutral", "analysis": "..", "strategy_advice": ".."}}"""

    def _call_ai_agent(self, prompt: str) -> dict:
        try:
            from src.agent.graph import run_agent_sync
            result = run_agent_sync(prompt, max_turns=2)
            import json as _json
            import re
            match = re.search(r'\{.*\}', result, re.DOTALL)
            if match:
                return _json.loads(match.group())
        except Exception as e:
            logger.warning(f"AI分析失败: {e}")
        return {"impact": "neutral", "analysis": "AI分析暂不可用",
                "strategy_advice": "请根据新闻自行判断"}

    def _generate_summary(self, report: NewsReport) -> str:
        parts = []
        if report.negative:
            parts.append(f"⚠️ {len(report.negative)}条利空")
        if report.positive:
            parts.append(f"✅ {len(report.positive)}条利好")
        if report.neutral:
            parts.append(f"➖ {len(report.neutral)}条中性")
        return "，".join(parts) if parts else "今日无重大新闻"


news_analyzer = NewsAnalyzer()
