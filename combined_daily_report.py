#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Combined Daily Report: Zhao Laoge + NQP V6
Runs after market close, generates unified report + WeChat push
"""
import sys, io, os, json, time, random, requests, re, urllib.request
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "daily_reports"
REPORTS_DIR.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"
DT_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# NQP V6 Stock Pool (from daily_push.py)
NQP_POOL = [
    "688561.SH","300454.SZ","688111.SH","300033.SZ","688012.SH",
    "002920.SZ","002906.SZ","300024.SZ","688122.SH","600765.SH",
    "600893.SH","300696.SZ","002389.SZ","603236.SH","300750.SZ",
    "688041.SH","002241.SZ","688787.SH","300502.SZ","300394.SZ",
]

# ── Rate Limiting ─────────────────────────────
_session = requests.Session()
_session.headers.update({"User-Agent": UA})
_last_call = [0.0]

def _get(url, timeout=15, **kw):
    wait = 0.8 - (time.time() - _last_call[0])
    if wait > 0: time.sleep(wait + random.uniform(0.05, 0.3))
    try:
        return _session.get(url, timeout=timeout, **kw)
    finally:
        _last_call[0] = time.time()

# =============================================================
# PART 1: ZHAO LAOGE STRATEGY (首板回封)
# =============================================================
def zhao_screening(target_date):
    """Run Zhao Laoge first-board screening. Returns summary dict."""
    # Fetch today's dragon tiger
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
        "filter": f"(TRADE_DATE>='{target_date}')(TRADE_DATE<='{target_date}')",
        "pageNumber": "1", "pageSize": "500",
        "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1",
        "source": "WEB", "client": "WEB",
    }
    try:
        r = _get(DT_URL, params=params, timeout=20, headers={"Referer": "https://data.eastmoney.com/"})
        today_data = (r.json().get("result") or {}).get("data") or []
    except Exception as e:
        return {"error": str(e), "picks": [], "total": 0}

    if not today_data:
        return {"error": "no_data", "picks": [], "total": 0}

    # Past 5 days for first-board filter
    past_start = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    past_end = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        r2 = _get(DT_URL, params={
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
            "filter": f"(TRADE_DATE>='{past_start}')(TRADE_DATE<='{past_end}')",
            "pageNumber": "1", "pageSize": "500",
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }, timeout=20, headers={"Referer": "https://data.eastmoney.com/"})
        past_data = (r2.json().get("result") or {}).get("data") or []
    except Exception:
        past_data = []
    recent_codes = {r.get("SECURITY_CODE","") for r in past_data}

    # Filter
    candidates, seen = [], set()
    for row in today_data:
        code = row.get("SECURITY_CODE","")
        name = row.get("SECURITY_NAME_ABBR","")
        if code in seen: continue
        seen.add(code)
        if "ST" in name.upper(): continue
        chg = float(row.get("CHANGE_RATE") or 0)
        if chg < 9: continue
        turnover = float(row.get("TURNOVERRATE") or 0)
        if turnover < 3: continue
        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if net_buy <= 0: continue
        close = float(row.get("CLOSE_PRICE") or 0)
        if close <= 0: continue
        reason = row.get("EXPLANATION","")
        is_first = code not in recent_codes
        is_multi = any(kw in reason for kw in ["连续","三日","累计"])

        candidates.append({
            "code": code, "name": name, "close": close, "change": chg,
            "turnover": turnover, "net_buy": net_buy,
            "buy": (row.get("BILLBOARD_BUY_AMT") or 0)/10000,
            "sell": (row.get("BILLBOARD_SELL_AMT") or 0)/10000,
            "reason": reason, "is_first": is_first, "is_multi": is_multi,
        })

    first_boards = [c for c in candidates if c["is_first"] and not c["is_multi"]]
    # Score
    for c in first_boards:
        s = 0
        nb = c["net_buy"]
        if nb >= 10000: s += 20
        elif nb >= 5000: s += 15
        elif nb >= 2000: s += 10
        elif nb >= 1000: s += 5
        else: s += 2
        to = c["turnover"]
        if 10 <= to <= 30: s += 10
        elif 5 <= to <= 40: s += 5
        if c["sell"] > 0:
            ratio = c["buy"] / c["sell"]
            if ratio >= 3: s += 8
            elif ratio >= 2: s += 5
            elif ratio >= 1.5: s += 3
        c["score"] = round(s, 1)

    first_boards.sort(key=lambda x: -x["score"])
    top = first_boards[:10]  # 返回前10，由主流程按市场分级过滤
    top_net_buy = sorted(candidates, key=lambda x: -x["net_buy"])[:5]
    return {"picks": top, "total": len(today_data), "first_count": len(first_boards),
            "top_net_buy": top_net_buy}

# =============================================================
# PART 2: NQP V6 STRATEGY (趋势追踪)
# =============================================================
def nqp_v6_analysis():
    """Run NQP V6 analysis on the stock pool. Returns summary dict."""
    results = {}
    tencent_codes = []

    # Fetch quotes from Tencent
    for raw in NQP_POOL:
        code_only = raw.split(".")[0]
        prefix = "sh" if code_only.startswith(("5","6","9")) else "sz"
        tencent_codes.append(f"{prefix}{code_only}")

    url = "https://qt.gtimg.cn/q=" + ",".join(tencent_codes)
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        return {"error": str(e), "stocks": [], "regime": "unknown"}

    # Parse
    stocks = []
    for line in data.strip().split(";"):
        if "=" not in line or '"' not in line: continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 50: continue
        code = key[2:]
        try:
            price = float(vals[3]) if vals[3] else 0
            name = vals[1]
            pe = float(vals[39]) if vals[39] else 0
            change = float(vals[32]) if vals[32] else 0
            stocks.append({"code": code, "name": name, "price": price, "pe": pe, "change": change})
        except (ValueError, IndexError):
            continue

    if not stocks:
        return {"error": "no_quote", "stocks": [], "regime": "unknown"}

    # Get K-lines for trend analysis
    kline_data = {}
    for s in stocks[:5]:  # Limit to 5 for speed
        code = s["code"]
        prefix = "sh" if code.startswith(("5","6","9")) else "sz"
        try:
            kr = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                            params={"param": f"{prefix}{code},day,,,250,qfq"},
                            headers={"User-Agent": UA}, timeout=8)
            kd = kr.json()
            kls = kd.get("data",{}).get(f"{prefix}{code}",{}).get("qfqday",[]) or \
                  kd.get("data",{}).get(f"{prefix}{code}",{}).get("day",[])
            if kls and len(kls) >= 200:
                closes = [float(k[2]) for k in kls]
                ma200 = sum(closes[-200:]) / 200
                dev = (closes[-1] - ma200) / ma200 * 100
                slope20 = (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0
                vol_5 = sum(float(k[5]) for k in kls[-5:]) / 5 if len(kls) >= 5 else 1
                vol_20 = sum(float(k[5]) for k in kls[-20:]) / 20 if len(kls) >= 20 else 1
                vr = vol_5 / vol_20 if vol_20 > 0 else 1
                s["ma200"] = round(ma200, 2)
                s["deviation"] = round(dev, 1)
                s["slope20"] = round(slope20, 1)
                s["volume_ratio"] = round(vr, 2)
        except Exception:
            pass

    # Market regime (simplified: check if most stocks above MA200)
    above_ma = sum(1 for s in stocks if s.get("deviation", -999) > 0)
    regime = "bull" if above_ma > len(stocks)/2 else ("bear" if above_ma < 3 else "weak")

    # Generate signals
    for s in stocks:
        signals = []
        dev = s.get("deviation", 0)
        slope = s.get("slope20", 0)
        vr = s.get("volume_ratio", 1)
        price = s["price"]

        # P1-P6 buy signals
        if abs(dev) < 5 and slope > 0: signals.append("P1")
        if dev < -18 and price > 0: signals.append("P2")
        if 2 < dev < 15 and vr > 1.3: signals.append("P3")
        if 5 < dev < 35 and slope > 3: signals.append("P4")
        if 10 < dev < 60 and slope > 5 and vr > 1.5: signals.append("P5")
        if dev < -30: signals.append("P6")

        # Apply V6 filter
        if regime == "bear":
            signals = []  # No buys
        elif regime == "weak":
            signals = [s for s in signals if s in ("P4","P5")]  # Only P4/P5

        # Sell signals
        sells = []
        if dev < -2: sells.append("S1")
        if dev < 0 and slope < 0: sells.append("S2")
        if dev < -5: sells.append("S3")
        if dev > 25: sells.append("S4")
        if dev < -5 and price < s.get("ma200", price)*0.98: sells.append("S5")

        s["buy_signals"] = signals
        s["sell_signals"] = sells

    return {"stocks": stocks, "regime": regime, "above_ma": above_ma, "total": len(stocks)}

# =============================================================
# REPORT GENERATION
# =============================================================
def generate_report(zhao_result, nqp_result, target_date, tomorrow):
    """Generate combined markdown report."""
    lines = []
    lines.append(f"# Vibe-Trading 每日策略报告 — {target_date}")
    lines.append(f"")
    lines.append(f"> 生成时间: {datetime.now().strftime('%H:%M')} | 目标交易日: **{tomorrow}**")
    lines.append(f"> 策略组合: 赵老哥首板回封（短线T+1）+ NQP V6趋势追踪（中长线）")
    lines.append(f"")

    # ── Section 1: Market Overview ──
    regime = nqp_result.get("regime", "unknown")
    regime_label = {"bull": "🟢 牛市", "weak": "🟡 弱势/震荡", "bear": "🔴 熊市"}.get(regime, "❓未知")
    lines.append("## 一、市场状态")
    lines.append(f"")
    lines.append(f"| 维度 | 状态 |")
    lines.append(f"|------|------|")
    lines.append(f"| NQP V6 市场分级 | **{regime_label}** |")
    lines.append(f"| 赵老哥短线环境 | {'✅ 可操作' if zhao_result.get('picks') else '❌ 无信号'} |")
    lines.append(f"| 今日龙虎榜 | {zhao_result.get('total', 0)} 条记录 |")
    lines.append(f"")

    # ── Section 2: Zhao Picks ──
    lines.append("## 二、赵老哥短线打板（明日操作）")
    lines.append(f"")
    lines.append(f"> 策略: 新题材龙头 + 近5日首板 + 高换手 + 高净买 + 回封确认")
    lines.append(f"")

    picks = zhao_result.get("picks", [])
    if not picks:
        lines.append("⚠️ 今日无符合条件首板标的，建议观望。")
        top_lhb = zhao_result.get("top_net_buy", [])
        if top_lhb:
            lines.append(f"")
            lines.append(f"**今日龙虎榜净买入榜（情绪参考）:**")
            lines.append(f"")
            lines.append(f"| 代码 | 名称 | 涨幅 | 净买入 | 换手 |")
            lines.append(f"|------|------|------|--------|------|")
            for t in top_lhb[:5]:
                nb_yi = t['net_buy'] / 10000
                nb_str = f"{nb_yi:.2f}亿" if nb_yi >= 1 else f"{t['net_buy']:.0f}万"
                lines.append(f"| {t['code']} | {t['name']} | {t['change']:+.1f}% | {nb_str} | {t['turnover']:.1f}% |")
            lines.append(f"")
            lines.append(f"> 注: 以上个股不满足全部首板条件，仅作情绪观察，不建议操作")
    else:
        if zhao_result.get("filter_note"):
            lines.append(f"")
            lines.append(f"> ⚠️ {zhao_result['filter_note']}")
        # 按市场分级定仓位
        if regime == "bear":
            pos3, pos1 = "5%试探仓", "3%迷你仓"
        elif regime == "weak":
            pos3, pos1 = "10%仓位", "5%仓位"
        else:
            pos3, pos1 = "20%仓位", "10%仓位"
        for i, c in enumerate(picks):
            emoji = ["🥇","🥈","🥉"][i]
            lines.append(f"### {emoji} #{i+1} {c['code']} {c['name']} — 评分 {c['score']:.0f}")
            lines.append(f"")
            lines.append(f"| 指标 | 数值 |")
            lines.append(f"|------|------|")
            lines.append(f"| 收盘 | ¥{c['close']:.2f} |")
            lines.append(f"| 涨幅 | **{c['change']:+.1f}%** |")
            lines.append(f"| 换手 | {c['turnover']:.1f}% |")
            lines.append(f"| 净买 | **{c['net_buy']:.0f}万** |")
            if c['sell'] > 0:
                lines.append(f"| 买/卖比 | {c['buy']/c['sell']:.1f}:1 |")
            lines.append(f"")
            lines.append(f"**明日操作:**")
            lines.append(f"| 条件 | 操作 |")
            lines.append(f"|------|------|")
            lines.append(f"| 高开≥{c['close']*1.03:.2f} | {pos3} |")
            lines.append(f"| 高开≥{c['close']*1.01:.2f} | {pos1} |")
            lines.append(f"| 低开<{c['close']*0.99:.2f} | 放弃 |")
            lines.append(f"| 止损 | ¥{c['close']*0.95:.2f} |")
            lines.append(f"")
        lines.append(f"*完整排名: {zhao_result.get('first_count', 0)} 只首板候选*")
    lines.append(f"")

    # ── Section 3: NQP V6 Pool ──
    lines.append("## 三、NQP V6 趋势追踪（持仓跟踪）")
    lines.append(f"")
    lines.append(f"> 市场分级: **{regime_label}** | 策略: {'全信号 P1-P6' if regime == 'bull' else '仅P4/P5趋势延续' if regime == 'weak' else '空仓'}")

    stocks = nqp_result.get("stocks", [])
    if not stocks:
        lines.append("⚠️ 行情获取失败，请检查网络。")
    else:
        # Sort: buy signals first, then by deviation
        stocks_with_buy = [s for s in stocks if s.get("buy_signals")]
        stocks_no_signal = [s for s in stocks if not s.get("buy_signals")]
        stocks_with_buy.sort(key=lambda x: -abs(x.get("deviation",0)))
        stocks_no_signal.sort(key=lambda x: x.get("deviation",0))

        # Buy signals table
        if stocks_with_buy:
            lines.append(f"### 🟢 买入信号")
            display_stocks = stocks_with_buy[:8] + stocks_no_signal[:4]
        else:
            lines.append(f"### 交易池概览")
            display_stocks = stocks_with_buy + stocks_no_signal[:12]

        lines.append(f"")
        lines.append(f"| 代码 | 名称 | 现价 | 涨跌 | PE | 乖离 | 趋势 | 信号 |")
        lines.append(f"|------|------|------|------|----|------|------|------|")
        for s in display_stocks:
            dev = s.get("deviation", 0)
            slope = s.get("slope20", 0)
            buys = ",".join(s.get("buy_signals",[])) or "—"
            sells = ",".join(s.get("sell_signals",[]))
            trend_icon = "↑" if slope > 3 else ("→" if slope > 0 else "↓")
            lines.append(f"| {s['code']} | {s['name']} | {s['price']:.2f} | {s['change']:+.1f}% | {s['pe']:.0f} | {dev:+.1f}% | {trend_icon} | {buys} |")

        if sells:
            sell_stocks = [s for s in display_stocks if s.get("sell_signals")]
            if sell_stocks:
                lines.append(f"")
                lines.append(f"**⚠️ 卖出预警:** " + ", ".join(f"{s['code']}({','.join(s.get('sell_signals',[]))})" for s in sell_stocks[:5]))
        lines.append(f"")

    # ── Section 4: Execution Rules ──
    lines.append("## 四、执行铁律")
    lines.append(f"")
    lines.append(f"**赵老哥短线:**")
    lines.append(f"- 只在高开≥1%时买入，低开直接跳过")
    lines.append(f"- 最多持有3只，单只≤20%仓位")
    lines.append(f"- T+1 竞价弱立即止损，不持股过周末")
    lines.append(f"")
    lines.append(f"**NQP V6 趋势:**")
    lines.append(f"- 严格遵守市场分级: {regime_label} → {'仅P4/P5' if regime == 'weak' else '全信号' if regime == 'bull' else '空仓等待'}")
    lines.append(f"- 硬止损12%，不补仓")
    lines.append(f"- 高位过热(S4)触发时减仓至半仓")
    lines.append(f"")

    # Footer
    lines.append("---")
    lines.append(f"*自动生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 赵老哥首板回封 + NQP V6趋势追踪*")
    lines.append(f"*⚠️ AI算法筛选，不构成投资建议*")

    return "\n".join(lines)

# =============================================================
# PUSH
# =============================================================
def push_wechat(title, content):
    if not SERVERCHAN_KEY:
        print("[Push] Skipped: SERVERCHAN_SENDKEY not set")
        return False
    try:
        r = requests.post(SERVERCHAN_URL.format(SERVERCHAN_KEY),
                         data={"title": title, "desp": content}, timeout=15)
        d = r.json()
        if d.get("code") == 0:
            print(f"[Push] OK: {title}")
            return True
        else:
            print(f"[Push] FAIL: {d.get('message','?')}")
            return False
    except Exception as e:
        print(f"[Push] Error: {e}")
        return False

# =============================================================
# MAIN
# =============================================================
if __name__ == "__main__":
    today = date.today().strftime("%Y-%m-%d")
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"Combined Daily Report: {today} -> {tomorrow}")

    # Part 1: Zhao
    print("Running Zhao Laoge screening...")
    zhao = zhao_screening(today)

    # Part 2: NQP V6
    print("Running NQP V6 analysis...")
    nqp = nqp_v6_analysis()

    # 市场分级过滤赵老哥短线（8月回测: bear下打板胜率仅32.7%/收益-2.48%）
    regime = nqp.get("regime", "?")
    picks_before = zhao.get("picks", [])
    if regime == "bear":
        zhao["picks"] = [p for p in picks_before if p["score"] >= 33][:1]
        zhao["filter_note"] = "熊市环境: 8月回测打板胜率32.7%，仅保留最高分标的(≥33)做5%试探仓，其余观望"
    elif regime == "weak":
        zhao["picks"] = [p for p in picks_before if p["score"] >= 28][:3]
        zhao["filter_note"] = "弱势环境: 评分≥28才入选，仓位减半"
    else:
        zhao["picks"] = picks_before[:3]
        zhao["filter_note"] = ""
    if len(picks_before) > len(zhao["picks"]):
        print(f"[Zhao] {regime}过滤: {len(picks_before)} -> {len(zhao['picks'])} 只")

    # Generate report
    report = generate_report(zhao, nqp, today, tomorrow)

    # Save
    report_path = REPORTS_DIR / f"combined_{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report: {report_path}")

    # Push
    picks = zhao.get("picks", [])
    pick_names = "/".join([p["name"] for p in picks[:3]]) if picks else "观望"
    regime = nqp.get("regime", "?")
    regime_emoji = {"bull":"🟢","weak":"🟡","bear":"🔴"}.get(regime,"❓")
    title = f"Vibe日报 {today} | {regime_emoji}{regime} | 短线:{pick_names}"

    # Generate commentary for each pick
    def commentary(c):
        notes = []
        nb = c['net_buy']
        if nb >= 10000: notes.append("断层领先")
        elif nb >= 5000: notes.append("主力强势")
        if c.get('sell', 0) > 0:
            ratio = c['buy'] / c['sell']
            if ratio >= 3: notes.append("买盘碾压")
            elif ratio >= 2: notes.append("买压显著")
        to = c['turnover']
        if 20 <= to <= 30: notes.append("最优换手区间")
        elif 10 <= to <= 35: notes.append("换手健康")
        chg = c['change']
        if chg >= 19.9: notes.append("20cm涨停")
        elif chg >= 10: notes.append("10cm涨停")
        return "，".join(notes) if notes else ""

    # Push content
    push_lines = [f"## Vibe日报 {today}", ""]
    push_lines.append(f"**市场状态** {regime_emoji} {regime} | 龙虎榜 {zhao.get('total',0)}条")
    push_lines.append("")

    if picks:
        push_lines.append("---")
        push_lines.append("## 📈 明日短线（赵老哥首板）")
        push_lines.append("")
        if zhao.get("filter_note"):
            push_lines.append(f"> ⚠️ {zhao['filter_note']}")
            push_lines.append("")
        for i, c in enumerate(picks[:3]):
            buy_1pct = round(c['close'] * 1.01, 2)
            buy_3pct = round(c['close'] * 1.03, 2)
            stop_at = round(c['close'] * 0.95, 2)
            target_at = round(c['close'] * 1.05, 2)
            bs_ratio_num = c['buy'] / c['sell'] if c.get('sell',0) > 0 else 0
            bs_ratio = f"{bs_ratio_num:.0f}:1" if bs_ratio_num > 0 else "—"
            net_buy_yi = c['net_buy'] / 10000
            net_buy_str = f"{net_buy_yi:.2f}亿" if net_buy_yi >= 1 else f"{c['net_buy']:.0f}万"
            buy_str = f"{c['buy']/10000:.2f}亿" if c['buy'] >= 10000 else f"{c['buy']:.0f}万"
            sell_str = f"{c['sell']/10000:.2f}亿" if c['sell'] >= 10000 else f"{c['sell']:.0f}万"
            cmt = commentary(c)
            emoji = ['🥇','🥈','🥉'][i]

            # 按市场分级定仓位
            if regime == "bear":
                pos3, pos1, pos0 = "5%试探仓", "3%迷你仓", "**不买**"
            elif regime == "weak":
                pos3, pos1, pos0 = "10%仓位", "5%仓位", "2%试探"
            else:
                pos3, pos1, pos0 = "直接20%仓位", "买入10%仓位，观察15分钟", "5%试探仓"

            push_lines.append(f"### {emoji} #{i+1} {c['name']} {c['code']} — 评分 {c['score']:.0f}/40" + (f"（{cmt}）" if cmt else ""))
            push_lines.append("")
            push_lines.append("| 指标 | 数值 |")
            push_lines.append("|------|------|")
            push_lines.append(f"| 今日收盘 | ¥{c['close']:.2f} |")
            chg_note = ""
            if c['change'] >= 19.9: chg_note = "（20cm涨停）"
            elif c['change'] >= 9.9: chg_note = "（涨停）"
            push_lines.append(f"| 涨幅 | **{c['change']:+.1f}%**{chg_note} |")
            push_lines.append(f"| 换手率 | {c['turnover']:.1f}% |")
            push_lines.append(f"| 净买入 | **{net_buy_str}** |")
            push_lines.append(f"| 买/卖比 | {buy_str} / {sell_str} ≈ {bs_ratio} |")
            push_lines.append("")
            push_lines.append("**明日操作计划：**")
            push_lines.append("")
            push_lines.append(f"> 高开 ≥ ¥{buy_3pct} (+3%) → {pos3}")
            push_lines.append(f"> 高开 ≥ ¥{buy_1pct} (+1%) → {pos1}")
            push_lines.append(f"> 平开/微高 → {pos0}")
            push_lines.append(f"> 低开 < ¥{round(c['close']*0.99,2)} (-1%) → **放弃**")
            push_lines.append(f"> 止损：¥{stop_at} (-5%)　|　目标：¥{target_at} (+5%)")
            push_lines.append("")
    else:
        push_lines.append("---")
        push_lines.append("## 📈 明日短线（赵老哥首板）")
        push_lines.append("")
        push_lines.append("⚠️ 今日无符合首板条件标的（需：涨停+高换手+净买入为正+近5日首板），**建议观望**")
        push_lines.append("")
        top_lhb = zhao.get("top_net_buy", [])
        if top_lhb:
            push_lines.append("**今日龙虎榜净买入榜（情绪参考）：**")
            push_lines.append("")
            push_lines.append("| 代码 | 名称 | 涨幅 | 净买入 | 换手 |")
            push_lines.append("|------|------|------|--------|------|")
            for t in top_lhb[:5]:
                nb_yi = t['net_buy'] / 10000
                nb_str = f"{nb_yi:.2f}亿" if nb_yi >= 1 else f"{t['net_buy']:.0f}万"
                push_lines.append(f"| {t['code']} | {t['name']} | {t['change']:+.1f}% | {nb_str} | {t['turnover']:.1f}% |")
            push_lines.append("")
            push_lines.append("> 注：以上个股不满足全部首板条件，仅作情绪观察，**不建议操作**")
            push_lines.append("")

    if nqp.get("stocks"):
        buy_stocks = [s for s in nqp["stocks"] if s.get("buy_signals")]
        sell_stocks = [s for s in nqp["stocks"] if s.get("sell_signals")]
        regime_hint = "仅P4/P5趋势延续" if regime=='weak' else ("全信号P1-P6" if regime=='bull' else "空仓等待")
        push_lines.append("---")
        push_lines.append("## 📊 NQP V6 趋势追踪")
        push_lines.append(f"**{regime_emoji} {regime_hint}**")
        push_lines.append("")

        if buy_stocks:
            push_lines.append("| 代码 | 名称 | 现价 | 乖离 | 信号 | 买入 | 止损 | 目标 |")
            push_lines.append("|------|------|------|------|------|------|------|------|")
            for s in buy_stocks[:5]:
                dev = s.get('deviation', 0)
                ma = s.get('ma200', 0)
                sigs = ','.join(s.get('buy_signals',[]))
                buy_p = f"¥{s['price']:.0f}" if s['price'] else "—"
                stop_p = f"¥{ma*0.95:.0f}" if ma else "—"
                target_p = f"¥{s['price']*1.10:.0f}" if s['price'] else "—"
                push_lines.append(f"| {s['code']} | {s['name']} | ¥{s['price']:.2f} | {dev:+.1f}% | {sigs} | {buy_p} | {stop_p} | {target_p} |")
            push_lines.append("")
        else:
            # 无买入信号：展示池子概览（卖出预警优先，其余按乖离最负）
            watch = sorted(sell_stocks, key=lambda x: x.get('deviation', 0))[:3]
            rest = [s for s in nqp["stocks"] if not s.get('sell_signals') and not s.get('buy_signals')]
            rest = sorted(rest, key=lambda x: x.get('deviation', 0))[:5 - len(watch)]
            watch.extend(rest)
            if watch:
                push_lines.append("**交易池观察（无买入信号，仅供跟踪）：**")
                push_lines.append("")
                push_lines.append("| 代码 | 名称 | 现价 | 乖离 | 信号 | 止损 | 目标 |")
                push_lines.append("|------|------|------|------|------|------|------|")
                for s in watch[:5]:
                    dev = s.get('deviation', 0)
                    ma = s.get('ma200', 0)
                    sells = ','.join(s.get('sell_signals', [])) or "—"
                    stop_p = f"¥{ma*0.95:.0f}" if ma else "—"
                    target_p = f"¥{s['price']*1.10:.0f}" if s['price'] else "—"
                    push_lines.append(f"| {s['code']} | {s['name']} | ¥{s['price']:.2f} | {dev:+.1f}% | {sells} | {stop_p} | {target_p} |")
                push_lines.append("")

        if sell_stocks:
            sc = ', '.join(f"{s['code']} {s['name']}(¥{s['price']:.2f})" for s in sell_stocks[:5])
            push_lines.append(f"⚠️ **卖出预警:** {sc}")
            push_lines.append("")

    push_lines.append("---")
    push_lines.append(f"*{today} 自动生成 | 赵老哥首板回封 + NQP V6趋势追踪*")
    push_wechat(title, "\n".join(push_lines))

    print("Done.")
