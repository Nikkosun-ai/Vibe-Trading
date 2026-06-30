"""定时任务调度器 — 交易日14:00新闻分析 + 14:30策略扫描."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()
_last_scan_result: dict = {}


def is_trading_day() -> bool:
    today = datetime.now()
    if today.weekday() >= 5:
        return False
    return True


def _load_market_data():
    from datetime import timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    from src.market_data import detect_source, get_loader
    codes = _get_scan_universe()
    data_map = {}
    for code in codes:
        try:
            source = detect_source(code)
            loader_cls = get_loader(source)
            loader = loader_cls(code, start, end, "1D")
            bars = loader.load()
            if bars is not None and not bars.empty:
                data_map[code] = bars
        except Exception:
            continue
    return data_map


def _get_scan_universe() -> list:
    hs300 = [
        "000333.SZ", "601318.SH", "300750.SZ", "601899.SH",
        "600519.SH", "000858.SZ", "002415.SZ", "300059.SZ",
        "600036.SH", "601166.SH",
    ]
    etfs = ["512480.SH", "159915.SZ", "159806.SZ", "510300.SH", "510500.SH"]
    return hs300 + etfs


def run_news_analysis():
    if not is_trading_day():
        logger.info("非交易日，跳过新闻分析")
        return
    logger.info("=== 开始执行新闻分析 (14:00) ===")
    try:
        from src.strategy_scanner import scanner
        from src.news_analyzer import news_analyzer
        data_map = _load_market_data()
        scan_result = scanner.scan("trend-matrix-v3", data_map)
        pool = scan_result.get("pool", [])
        symbols = [p["symbol"] for p in pool]
        news_items = news_analyzer.collect_news(symbols)
        report = news_analyzer.analyze_with_ai(news_items, pool)
        logger.info(f"新闻分析完成: {report.summary}")
    except Exception as e:
        logger.error(f"新闻分析失败: {e}")


def run_strategy_scan():
    global _last_scan_result
    if not is_trading_day():
        logger.info("非交易日，跳过策略扫描")
        return
    logger.info("=== 开始执行策略扫描 (14:30) ===")
    try:
        from src.strategy_scanner import scanner
        data_map = _load_market_data()
        result = scanner.scan("trend-matrix-v3", data_map)
        _last_scan_result = result
        signals = result.get("signals", [])
        pool = result.get("pool", [])
        logger.info(f"扫描完成: 股票池{len(pool)}只, 信号{len(signals)}个")
    except Exception as e:
        logger.error(f"策略扫描失败: {e}")


def get_last_scan_result() -> dict:
    global _last_scan_result
    return _last_scan_result


def start_scheduler():
    scheduler.add_job(
        run_news_analysis,
        CronTrigger(hour=14, minute=0, day_of_week="mon-fri"),
        id="news_analysis", name="每日新闻分析", replace_existing=True)
    scheduler.add_job(
        run_strategy_scan,
        CronTrigger(hour=14, minute=30, day_of_week="mon-fri"),
        id="strategy_scan", name="每日策略扫描", replace_existing=True)
    scheduler.start()
    logger.info("定时任务已启动: 新闻分析14:00, 策略扫描14:30 (周一至周五)")


def stop_scheduler():
    scheduler.shutdown(wait=False)
    logger.info("定时任务已停止")
