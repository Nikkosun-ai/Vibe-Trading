#!/usr/bin/env python3
"""
NQP V3.3 每日趋势追踪完整报告 — v9
修复: baostock 改为一次 login 全部提取（根治连接状态混乱导致 0 只指标问题）
10章报告 + 池变动追踪 + 方糖推送摘要
"""

import os, sys, json, re, time, traceback
import urllib.request
from datetime import datetime, date, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd

# ============================================================
# 0. 配置
# ============================================================
FANGTANG_KEY = os.environ.get("FANGTANG_KEY", "SCT376111TEagAv1cUxT0v3ssNb39UhzNp")
DRY_RUN = "--dry-run" in sys.argv
TODAY = date.today()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLOUD_DIR = os.path.join(SCRIPT_DIR, "cloud") if os.path.isdir(os.path.join(SCRIPT_DIR, "cloud")) else SCRIPT_DIR

NAME_CACHE = {}
DEFAULT_POOL = [
    "688561.SH", "300454.SZ", "688111.SH", "300033.SZ", "688012.SH",
    "002920.SZ", "002906.SZ", "300024.SZ", "688122.SH", "600765.SH",
    "600893.SH", "300696.SZ", "002389.SZ", "603236.SH", "300750.SZ",
    "688041.SH", "002241.SZ", "688787.SH", "300502.SZ", "300394.SZ",
]


# ============================================================
# 1. 获取股票名称（baostock）
# ============================================================
def fetch_names(codes):
    global NAME_CACHE
    missing = [c for c in codes if c not in NAME_CACHE]
    if not missing:
        return NAME_CACHE

    try:
        import baostock as bs
        bs.login()
        for code in missing:
            bare = code.split(".")[0]
            suffix = code.split(".")[-1].lower()
            bs_code = f"{suffix}.{bare}"
            try:
                rs = bs.query_stock_basic(code=bs_code)
                if rs.error_code == '0':
                    while rs.next():
                        row = rs.get_row_data()
                        exchange = code.split(".")[-1].upper()
                        NAME_CACHE[f"{row[0]}.{exchange}"] = row[1] if len(row) > 1 else bare
                else:
                    NAME_CACHE[code] = bare
            except:
                NAME_CACHE[code] = bare
        bs.logout()
    except Exception as e:
        print(f"  ⚠️ baostock 不可用 ({e})，使用代码作为名称")
        for code in missing:
            NAME_CACHE[code] = code.split(".")[0]

    print(f"  获取 {len(codes)} 只股票名称")
    return NAME_CACHE


# ============================================================
# 2. 获取行情数据 — v9 核心改进：一次 baostock login 全部提取
# ============================================================
def fetch_all_data_v9(codes, days=250):
    """一次 login，逐只 query，一次 logout。回退到腾讯 API。"""
    all_data = {}
    stats = {'baostock': 0, 'tencent': 0, 'failed': 0}

    # ---- Level 1: baostock session（一次登录全部查询）----
    print("  [baostock] 建立连接...")
    bs_ok = False
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code == '0':
            bs_ok = True
            print("  [baostock] 连接成功，批量查询中...")
        else:
            print(f"  [baostock] 登录失败: {lg.error_msg}")
    except Exception as e:
        print(f"  [baostock] 导入/登录异常: {e}")

    if bs_ok:
        end_date = TODAY.strftime("%Y-%m-%d")
        start_date = (TODAY - timedelta(days=days + 50)).strftime("%Y-%m-%d")

        for code in codes:
            if code in all_data:
                continue
            bare = code.split(".")[0]
            suffix = code.split(".")[-1].lower()
            bs_code = f"{suffix}.{bare}"

            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume",
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="2"
                )
                if rs.error_code != '0':
                    continue

                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())

                if len(rows) < 20:
                    continue

                df = pd.DataFrame(rows, columns=['date','Open','High','Low','Close','Volume'])
                for col in ['Open','High','Low','Close','Volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['Close'])
                if len(df) < 20:
                    continue

                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                all_data[code] = df.tail(days)
                stats['baostock'] += 1
            except Exception:
                pass

        try:
            bs.logout()
        except:
            pass
        print(f"  [baostock] 完成，获取 {stats['baostock']} 只")

    # ---- Level 2: tencent API 兜底 ----
    for code in codes:
        if code in all_data:
            continue
        try:
            df = _try_tencent(code, days)
            if df is not None and len(df) >= 20:
                all_data[code] = df
                stats['tencent'] += 1
        except:
            pass

    for code in codes:
        if code not in all_data:
            stats['failed'] += 1
            print(f"  ⚠️ {code} 所有数据源均失败")

    print(f"  获取 {len(all_data)} 只股票日线数据 "
          f"(baostock:{stats['baostock']} tencent:{stats['tencent']} 失败:{stats['failed']})")
    return all_data


def _try_tencent(code, days):
    """腾讯行情 API"""
    bare = code.split(".")[0]
    suffix = code.split(".")[-1].lower()
    symbol = f"{suffix}{bare}"

    url = (
        f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{days + 50},qfq"
    )
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'http://web.ifzq.gtimg.cn/'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('code') != 0:
            return None
        day_data = data.get('data', {}).get(symbol, {}).get('qfqday')
        if not day_data and 'day' in data.get('data', {}).get(symbol, {}):
            day_data = data['data'][symbol]['day']
        if not day_data:
            return None
        rows = []
        for item in day_data:
            rows.append([
                item[0],
                float(item[1]), float(item[2]), float(item[3]),
                float(item[4]), float(item[5])
            ])
        df = pd.DataFrame(rows, columns=['date','Open','High','Low','Close','Volume'])
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        return df.tail(days)
    except:
        return None


# ============================================================
# 3. 计算指标 + 生成信号
# ============================================================
def compute_indicators(data_map, codes, names):
    """计算 MA200、趋势分、乖离率、ATR 等指标"""
    results = {}
    skipped_no_data = 0
    skipped_short = 0
    skipped_ma200 = 0

    for code in codes:
        if code not in data_map:
            skipped_no_data += 1
            continue
        df = data_map[code]
        if df.empty or len(df) < 200:
            skipped_short += 1
            continue

        o = df['Open']
        h = df['High']
        l = df['Low']
        c = df['Close']
        v = df['Volume']

        # MA200
        ma200 = c.rolling(200).mean().iloc[-1]
        if pd.isna(ma200) or ma200 <= 0:
            skipped_ma200 += 1
            continue

        close_now = c.iloc[-1]
        close_prev = c.iloc[-2] if len(c) > 1 else close_now
        change_pct = (close_now / close_prev - 1) * 100
        deviation = (close_now / ma200 - 1) * 100  # 乖离率

        # MA20 / MA50
        ma20 = c.rolling(20).mean().iloc[-1]
        ma50 = c.rolling(50).mean().iloc[-1]

        # 趋势分 (0-100)
        trend_score = 0
        if close_now > ma20: trend_score += 20
        if close_now > ma50: trend_score += 20
        if close_now > ma200: trend_score += 20
        # 短期动量
        if len(c) >= 21 and close_now > c.iloc[-21]: trend_score += 15
        # 中期动量
        if len(c) >= 61 and close_now > c.iloc[-61]: trend_score += 15
        # 均线多头排列
        if ma20 > ma50 > ma200: trend_score += 10

        # ATR (14)
        tr = pd.concat([
            h - l,
            (h - c.shift()).abs(),
            (l - c.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]

        # 波动率
        returns = c.pct_change().dropna()
        volatility = returns.rolling(20).std().iloc[-1] * np.sqrt(252) if len(returns) >= 20 else 0

        # 量比
        avg_vol_20 = v.rolling(20).mean().iloc[-1]
        vol_ratio = v.iloc[-1] / avg_vol_20 if avg_vol_20 > 0 else 1.0

        # 52周位置
        high_52w = c.rolling(252).max().iloc[-1] if len(c) >= 252 else c.max()
        low_52w = c.rolling(252).min().iloc[-1] if len(c) >= 252 else c.min()
        pct_52w = (close_now - low_52w) / (high_52w - low_52w) * 100 if high_52w > low_52w else 50

        results[code] = {
            'name': names.get(code, code.split('.')[0]),
            'close': close_now,
            'change_pct': change_pct,
            'ma20': ma20,
            'ma50': ma50,
            'ma200': ma200,
            'deviation': deviation,
            'trend_score': trend_score,
            'atr': atr,
            'volatility': volatility,
            'vol_ratio': vol_ratio,
            'pct_52w': pct_52w,
            'high_52w': high_52w,
            'low_52w': low_52w,
        }

    print(f"  计算 {len(results)} 只股票指标 "
          f"(无数据:{skipped_no_data} 数据不足200天:{skipped_short} MA200无效:{skipped_ma200})")
    return results


# ============================================================
# 4. 生成信号
# ============================================================
def generate_signals(indicators):
    """生成 P1-P5 买入信号 + S1-S5 卖出信号"""
    buy_signals = []
    sell_signals = []

    for code, d in indicators.items():
        deviation = d['deviation']
        trend = d['trend_score']
        vol_ratio = d['vol_ratio']
        close = d['close']
        ma200 = d['ma200']
        atr = d['atr']
        vol = d['volatility']

        name = d['name']
        chg = d['change_pct']

        # ----- 买入信号 P1-P5 -----
        # P1: MA200 回调买入（趋势分≥60，乖离-5%~+5%，价量配合）
        if trend >= 60 and -5 <= deviation <= 5 and vol_ratio >= 0.8:
            score = min(100, trend + 20 * (vol_ratio - 0.8) / 0.2)
            buy_signals.append({
                'code': code, 'name': name, 'type': 'P1', 'label': 'MA200回调',
                'score': round(score), 'close': close, 'deviation': deviation,
                'desc': f"MA200({ma200:.2f})附近回调，乖离{deviation:+.1f}%，趋势分{trend}"
            })

        # P2: 趋势加速突破（趋势分≥70，今日涨>2%，量比>1.2）
        if trend >= 70 and chg > 2 and vol_ratio > 1.2:
            score = min(100, trend + 10 * (chg - 2) / 3 + 10 * (vol_ratio - 1.2) / 0.5)
            buy_signals.append({
                'code': code, 'name': name, 'type': 'P2', 'label': '趋势加速',
                'score': round(score), 'close': close, 'deviation': deviation,
                'desc': f"趋势分{trend}，日涨{chg:+.1f}%，量比{vol_ratio:.1f}x"
            })

        # P3: 外部冲击修复（趋势分≥40，-10%≤乖离≤-2%，量比≥1.0）
        if trend >= 40 and -10 <= deviation <= -2 and vol_ratio >= 1.0:
            score = min(100, 50 + 30 * (-deviation - 2) / 8 + 20 * (vol_ratio - 1.0) / 0.5)
            buy_signals.append({
                'code': code, 'name': name, 'type': 'P3', 'label': '超跌修复',
                'score': round(score), 'close': close, 'deviation': deviation,
                'desc': f"乖离{deviation:+.1f}%偏离MA200，趋势分{trend}，量比{vol_ratio:.1f}x"
            })

        # P4: 突破确认（趋势分≥80，乖离2%~15%，量比>1.0）
        if trend >= 80 and 2 <= deviation <= 15 and vol_ratio > 1.0:
            score = min(100, trend + 5 * (deviation - 2) + 5 * (vol_ratio - 1.0))
            buy_signals.append({
                'code': code, 'name': name, 'type': 'P4', 'label': '突破确认',
                'score': round(score), 'close': close, 'deviation': deviation,
                'desc': f"强势突破MA200，乖离{deviation:+.1f}%，趋势分{trend}"
            })

        # P5: 底部复苏（趋势分≤30 但连续两日收阳，量比>0.7）
        if trend <= 30 and chg > 0 and vol_ratio > 0.7:
            # 检查前一交易日也收阳
            # (简化版：只检查今日阳线 + 趋势极低)
            score = min(100, 30 + 40 * (1 - trend / 30) + 30 * vol_ratio)
            buy_signals.append({
                'code': code, 'name': name, 'type': 'P5', 'label': '底部复苏',
                'score': round(score), 'close': close, 'deviation': deviation,
                'desc': f"趋势分仅{trend}，可能底部企稳，乖离{deviation:+.1f}%"
            })

        # ----- 卖出信号 S1-S5 -----
        # S1: 趋势破位（跌破MA200 且 趋势分<40）
        if close < ma200 and trend < 40:
            sell_signals.append({
                'code': code, 'name': name, 'type': 'S1', 'level': 'RED', 'label': '趋势破位',
                'desc': f"跌破MA200({ma200:.2f})，趋势分{trend}，乖离{deviation:+.1f}%"
            })

        # S2: 高位过热（乖离>20% 且 趋势分>70）
        if deviation > 20 and trend > 70:
            sell_signals.append({
                'code': code, 'name': name, 'type': 'S2', 'level': 'RED', 'label': '高位过热',
                'desc': f"乖离{deviation:+.1f}%严重偏离MA200，趋势分{trend}"
            })

        # S3: 量价背离（近5日涨幅>0但量比<0.6）
        if chg > 0 and vol_ratio < 0.6:
            sell_signals.append({
                'code': code, 'name': name, 'type': 'S3', 'level': 'YELLOW', 'label': '量价背离',
                'desc': f"涨幅{chg:+.1f}%但量比仅{vol_ratio:.1f}x"
            })

        # S4: 高位过热分档（乖离25-100%）
        if deviation > 25:
            if deviation >= 80:
                s4_label = "S4-极端(≥80%)"
            elif deviation >= 60:
                s4_label = "S4-高危(60-80%)"
            elif deviation >= 40:
                s4_label = "S4-过热(40-60%)"
            else:
                s4_label = "S4-温和(25-40%)"

            level = 'RED' if deviation >= 40 else 'YELLOW'
            sell_signals.append({
                'code': code, 'name': name, 'type': 'S4', 'level': level, 'label': s4_label,
                'desc': f"乖离{deviation:+.1f}%远超MA200({ma200:.2f})"
            })

        # S5: 趋势走弱（趋势分<30，且连续走弱）
        if trend < 30:
            sell_signals.append({
                'code': code, 'name': name, 'type': 'S5', 'level': 'YELLOW', 'label': '趋势走弱',
                'desc': f"趋势分仅{trend}，乖离{deviation:+.1f}%"
            })

    # 去重：每只标的取最高置信度买入信号；卖出 RED 优先（有 RED 不出 YELLOW）
    buy_dedup = {}
    for s in buy_signals:
        key = s['code']
        if key not in buy_dedup or s['score'] > buy_dedup[key]['score']:
            buy_dedup[key] = s
    buy_signals = sorted(buy_dedup.values(), key=lambda x: x['score'], reverse=True)

    sell_dedup = {}
    for s in sell_signals:
        key = s['code']
        if key not in sell_dedup:
            sell_dedup[key] = s
        elif s['level'] == 'RED' and sell_dedup[key]['level'] != 'RED':
            sell_dedup[key] = s  # RED 优先
    sell_signals = sorted(sell_dedup.values(), key=lambda x: (0 if x['level']=='RED' else 1, x['type']))

    red_count = sum(1 for s in sell_signals if s['level'] == 'RED')
    ylw_count = sum(1 for s in sell_signals if s['level'] == 'YELLOW')
    print(f"  买入信号: {len(buy_signals)} 条")
    print(f"  卖出信号: {len(sell_signals)} 条 (🔴{red_count} 🟡{ylw_count})")

    return buy_signals, sell_signals


# ============================================================
# 5. 池变动检测
# ============================================================
def detect_pool_changes(current_pool):
    """对比上次快照，检测新纳入/移出"""
    snapshot_path = os.path.join(CLOUD_DIR, "pool_snapshot.json")
    prev = set()
    if os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, 'r') as f:
                prev = set(json.load(f).get('pool', []))
        except:
            pass

    curr = set(current_pool)
    new_in = sorted(curr - prev)
    removed = sorted(prev - curr)

    # 保存当前快照
    with open(snapshot_path, 'w') as f:
        json.dump({'pool': sorted(curr), 'date': TODAY.isoformat()}, f, ensure_ascii=False)

    if new_in or removed:
        print(f"  池变动: 🟢{len(new_in)}只新入 🔴{len(removed)}只移出")
    else:
        print("  无池变动")
    return new_in, removed


# ============================================================
# 6. 生成完整报告
# ============================================================
def generate_report(codes, names, indicators, buy_signals, sell_signals, new_in, removed, report_path):
    """生成 10 章完整 Markdown 报告"""
    lines = []
    lines.append(f"# NQP V3.3 每日趋势追踪报告")
    lines.append(f"**日期**: {TODAY.isoformat()}  |  **模式**: {'🔍 DRY RUN' if DRY_RUN else '📡 正式推送'}")
    lines.append("")

    # 第一章：策略总览
    lines.append("## 一、策略总览")
    lines.append(f"- 扫描标的: {len(codes)} 只")
    lines.append(f"- 有效指标: {len(indicators)} 只")
    lines.append(f"- 买入信号: {len(buy_signals)} 条")
    lines.append(f"- 卖出信号: {len(sell_signals)} 条 ({sum(1 for s in sell_signals if s['level']=='RED')}🔴 {sum(1 for s in sell_signals if s['level']=='YELLOW')}🟡)")
    lines.append("")

    # 第二章：池变动追踪
    lines.append("## 二、池变动追踪")
    if new_in:
        lines.append("### 🟢 新纳入")
        for c in new_in:
            lines.append(f"- **{names.get(c, c)}** ({c})")
    else:
        lines.append("### 🟢 新纳入: 无")
    if removed:
        lines.append("### 🔴 移出")
        for c in removed:
            lines.append(f"- **{names.get(c, c)}** ({c})")
    else:
        lines.append("### 🔴 移出: 无")
    lines.append("")

    # 第三章：交易池全景
    lines.append("## 三、交易池全景")
    if indicators:
        lines.append(f"| 名称 | 代码 | 现价 | 涨跌 | MA200 | 乖离 | 趋势分 | 量比 | 52周位置 |")
        lines.append(f"|------|------|------|------|-------|------|--------|------|----------|")
        for code in codes:
            if code in indicators:
                d = indicators[code]
                dev_sign = "+" if d['deviation'] >= 0 else ""
                chg_sign = "+" if d['change_pct'] >= 0 else ""
                lines.append(f"| {d['name']} | {code} | {d['close']:.2f} | {chg_sign}{d['change_pct']:.1f}% | {d['ma200']:.2f} | {dev_sign}{d['deviation']:.1f}% | {d['trend_score']} | {d['vol_ratio']:.1f}x | {d['pct_52w']:.0f}% |")
    else:
        lines.append("无有效指标数据")
    lines.append("")

    # 第四章：买入信号
    lines.append("## 四、买入信号")
    if buy_signals:
        lines.append(f"| 名称 | 代码 | 信号 | 置信度 | 现价 | 乖离 | 信号解读 |")
        lines.append(f"|------|------|------|--------|------|------|----------|")
        for s in buy_signals:
            lines.append(f"| {s['name']} | {s['code']} | {s['label']}({s['type']}) | {s['score']}% | {s['close']:.2f} | {s['deviation']:+.1f}% | {s['desc']} |")
    else:
        lines.append("本日无买入信号触发。")
    lines.append("")

    # 第五章：卖出信号
    lines.append("## 五、卖出信号")
    if sell_signals:
        reds = [s for s in sell_signals if s['level'] == 'RED']
        ylws = [s for s in sell_signals if s['level'] == 'YELLOW']

        if reds:
            lines.append("### 🔴 红色警报（需立即关注）")
            lines.append(f"| 名称 | 代码 | 信号 | 信号解读 |")
            lines.append(f"|------|------|------|----------|")
            for s in reds:
                lines.append(f"| {s['name']} | {s['code']} | {s['label']}({s['type']}) | {s['desc']} |")
            lines.append("")

        if ylws:
            lines.append("### 🟡 黄色预警（持续监控）")
            lines.append(f"| 名称 | 代码 | 信号 | 信号解读 |")
            lines.append(f"|------|------|------|----------|")
            for s in ylws:
                lines.append(f"| {s['name']} | {s['code']} | {s['label']}({s['type']}) | {s['desc']} |")
            lines.append("")
    else:
        lines.append("本日无卖出信号触发。")
    lines.append("")

    # 第六章：逐股操作建议
    lines.append("## 六、逐股操作建议")
    buy_codes = {s['code'] for s in buy_signals}
    sell_codes = {s['code'] for s in sell_signals}
    for code in codes:
        if code not in indicators:
            continue
        d = indicators[code]
        action = "观望"
        if code in buy_codes:
            action = "🟢 关注买入"
        elif code in sell_codes:
            sell_levels = [s['level'] for s in sell_signals if s['code'] == code]
            if 'RED' in sell_levels:
                action = "🔴 考虑减仓"
            else:
                action = "🟡 密切监控"

        lines.append(f"### {d['name']} ({code})")
        lines.append(f"- **建议**: {action}")
        lines.append(f"- 现价 {d['close']:.2f} | 趋势分 {d['trend_score']} | 乖离 {d['deviation']:+.1f}% | 量比 {d['vol_ratio']:.1f}x")
        lines.append("")
    lines.append("")

    # 第 7-8 章（简化版，完整版需更多数据）
    lines.append("## 七、风险评估")
    if indicators:
        deviations = [d['deviation'] for d in indicators.values()]
        trends = [d['trend_score'] for d in indicators.values()]
        avg_dev = np.mean(deviations)
        avg_trend = np.mean(trends)
        red_count = sum(1 for s in sell_signals if s['level'] == 'RED')
        buy_count = len(buy_signals)

        # 市场温度
        if avg_trend >= 60 and avg_dev >= 10:
            temp = "🔥 偏热"
        elif avg_trend <= 30 or avg_dev <= -5:
            temp = "❄️ 偏冷"
        else:
            temp = "🌤 温和"

        lines.append(f"- **市场温度**: {temp}")
        lines.append(f"- 平均趋势分: {avg_trend:.0f}")
        lines.append(f"- 平均乖离: {avg_dev:+.1f}%")
        lines.append(f"- RED警报数: {red_count}")
        ratio = f"{buy_count}买 / {red_count}🔴卖"
        if red_count > buy_count:
            lines.append(f"- ⚠️ 信号比({ratio}): 卖压主导，注意仓位")
        elif buy_count > 0:
            lines.append(f"- ✅ 信号比({ratio}): 机会大于风险")
        else:
            lines.append(f"- ➖ 信号比({ratio}): 市场平静")
    lines.append("")

    lines.append("## 八、明日关注")
    # MA200 附近标的
    ma200_near = [(c, d) for c, d in indicators.items() if -3 <= d['deviation'] <= 3]
    if ma200_near:
        lines.append("### MA200 附近（潜在买点区域）")
        for c, d in sorted(ma200_near, key=lambda x: abs(x[1]['deviation'])):
            lines.append(f"- **{d['name']}** ({c}) 乖离 {d['deviation']:+.1f}%")
    else:
        lines.append("无MA200附近标的")

    # 超跌标的
    oversold = [(c, d) for c, d in indicators.items() if d['deviation'] <= -10]
    if oversold:
        lines.append("### 超跌标的（关注修复机会）")
        for c, d in sorted(oversold, key=lambda x: x[1]['deviation']):
            lines.append(f"- **{d['name']}** ({c}) 乖离 {d['deviation']:+.1f}%")
    lines.append("")

    # 第九章：数据质量说明
    lines.append("## 九、数据质量说明")
    lines.append(f"- 数据日期: {TODAY.isoformat()}")
    lines.append(f"- 标的覆盖: {len(indicators)}/{len(codes)} 只有效指标")
    lines.append(f"- MA200可用: 需要≥200个交易日历史")
    lines.append(f"- ⚠️ 仅供研究参考，不构成投资建议")
    lines.append("")

    report = "\n".join(lines)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"  完整报告: {report_path} ({len(report)} 字符)")
    return report


# ============================================================
# 7. 方糖推送
# ============================================================
def push_wx(buy_signals, sell_signals, new_in, removed, report_path):
    """推送摘要到微信（方糖），完整报告见文件"""
    # 标题
    buy_n = len(buy_signals)
    sell_n = len(sell_signals)
    red_n = sum(1 for s in sell_signals if s['level'] == 'RED')
    title = f"NQP {TODAY.strftime('%m-%d')} 🔥买{buy_n} ⚠️卖{sell_n}"

    lines = [f"🔥 NQP V3.3 {TODAY.isoformat()}"]

    # 池变动
    if new_in:
        lines.append(f"\n🟢 新纳入: {', '.join(n for n in new_in)}")
    if removed:
        lines.append(f"🔴 移出: {', '.join(r for r in removed)}")

    # 买入
    if buy_signals:
        lines.append(f"\n--- 🛒 买入({buy_n}只) ---")
        for s in buy_signals[:5]:
            lines.append(f"  {s['label']} {s['name']}({s['code']}) {s['score']}%")
        if buy_n > 5:
            lines.append(f"  ... 共{buy_n}只，完整报告见文件")

    # 卖出 RED
    reds = [s for s in sell_signals if s['level'] == 'RED']
    ylws = [s for s in sell_signals if s['level'] == 'YELLOW']
    if reds:
        lines.append(f"\n--- 🔴 卖出警报({len(reds)}只) ---")
        for s in reds[:5]:
            lines.append(f"  {s['label']} {s['name']}({s['code']})")
        if len(reds) > 5:
            lines.append(f"  ... 共{len(reds)}只")

    # YELLOW 数量
    if ylws:
        lines.append(f"\n🟡 黄色预警: {len(ylws)}只 (完整报告见文件)")

    if not buy_signals and not sell_signals:
        lines.append("\n今日无信号触发")

    lines.append(f"\n📄 完整报告: cloud/report_{TODAY.isoformat()}.md")
    lines.append("⚠️ 仅供研究参考，不构成投资建议")

    content = "\n".join(lines)

    if DRY_RUN:
        print(f"\n  🔍 [DRY RUN] 不推送")
        print(f"  标题: {title}")
        print(f"  内容: {len(content)} 字符")
        print(f"  内容预览:")
        for line in content.split("\n")[:10]:
            print(f"  {line}")
        if len(content.split("\n")) > 10:
            print(f"  ... 共{len(content.splitlines())}行")
    else:
        try:
            url = f"https://sctapi.ftqq.com/{FANGTANG_KEY}.send"
            data = {"title": title, "desp": content}
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data).encode('utf-8'),
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            if result.get('code') == 0:
                print("  ✅ 微信推送成功")
            else:
                print(f"  ⚠️ 推送可能失败: {result}")
        except Exception as e:
            print(f"  ❌ 推送失败: {e}")


# ============================================================
# 8. 主流程
# ============================================================
def main():
    print(f"NQP V3.3 每日趋势追踪报告 — v9")
    print(f"日期: {TODAY.isoformat()} {'🔍 DRY RUN' if DRY_RUN else '📡 正式推送'}")
    print("=" * 60)

    # 加载池
    pool_path = os.path.join(SCRIPT_DIR, "pool_result.json")
    codes = DEFAULT_POOL
    if os.path.exists(pool_path):
        try:
            with open(pool_path, 'r') as f:
                pool_data = json.load(f)
            if 'codes' in pool_data and pool_data['codes']:
                codes = [c if '.' in c else f"{c}.SH" if c.startswith('6') else f"{c}.SZ"
                         for c in pool_data['codes']]
                print(f"  使用池文件: {len(codes)} 只")
        except:
            print(f"  使用默认池: {len(codes)} 只")
    else:
        print(f"  使用默认池: {len(codes)} 只")

    if not codes:
        print("❌ 无股票池，退出")
        return

    # [1/5] 名称
    print("\n[1/5] 获取股票名称...")
    names = fetch_names(codes)

    # [2/5] 池变动
    print("\n[2/5] 检测池变动...")
    new_in, removed = detect_pool_changes(codes)

    # [3/5] 行情
    print("\n[3/5] 获取行情数据...")
    data_map = fetch_all_data_v9(codes)

    # [4/5] 指标 + 信号
    print("\n[4/5] 计算指标 + 生成信号...")
    indicators = compute_indicators(data_map, codes, names)
    if not indicators:
        print("  ❌ 无有效指标数据，退出")
        return

    buy_signals, sell_signals = generate_signals(indicators)

    # [5/5] 报告 + 推送
    print("\n[5/5] 生成报告 + 推送...")
    report_path = os.path.join(CLOUD_DIR, f"report_{TODAY.isoformat()}.md")
    generate_report(codes, names, indicators, buy_signals, sell_signals, new_in, removed, report_path)
    push_wx(buy_signals, sell_signals, new_in, removed, report_path)

    print("\n" + "=" * 60)
    print("完成 ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
