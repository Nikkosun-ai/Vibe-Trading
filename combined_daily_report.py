#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Combined Daily Report: Daban (打板复盘) + NQP V6
Runs after market close, generates unified report + WeChat push
打板部分: 收盘口径复盘 + 明日开盘卖出提醒 (盘中执行见 daban_daily.py 14:32推送)
"""
import sys, io, os, time, random, requests, urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from daban_core import fetch_zt_pool, screen_candidates, is_trading_day

# ── Config ────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "daily_reports"
REPORTS_DIR.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"

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
# PART 1: DABAN REVIEW (打板复盘)
# =============================================================
def daban_review(target_date):
    """收盘后复盘: 重拉当日涨停池 + 收盘口径筛选. Returns summary dict."""
    if not is_trading_day(target_date):
        return {"error": "holiday", "day_zt": 0, "tier": "非交易日",
                "note": "非交易日, 跳过复盘", "cands": []}
    rows = fetch_zt_pool(target_date)
    if not rows:
        # 对比昨日盘中信号文件, 判断是否非交易日
        return {"error": "no_data", "day_zt": 0, "tier": "无数据",
                "note": "涨停池为空(非交易日或数据源异常)", "cands": []}
    res = screen_candidates(rows, target_date)
    return {"day_zt": res["day_zt"], "tier": res["tier"], "pos": res["pos"],
            "note": res["note"], "cands": res["cands"], "error": None}

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
def generate_report(daban_result, nqp_result, target_date, tomorrow):
    """Generate combined markdown report."""
    lines = []
    lines.append(f"# Vibe-Trading 每日策略报告 — {target_date}")
    lines.append(f"")
    lines.append(f"> 生成时间: {datetime.now().strftime('%H:%M')} | 目标交易日: **{tomorrow}**")
    lines.append(f"> 策略组合: 打板短线（首板T+1）+ NQP V6趋势追踪（中长线）")
    lines.append(f"")

    # ── Section 1: Market Overview ──
    regime = nqp_result.get("regime", "unknown")
    regime_label = {"bull": "🟢 牛市", "weak": "🟡 弱势/震荡", "bear": "🔴 熊市"}.get(regime, "❓未知")
    dz = daban_result.get("day_zt", 0)
    daban_env = "✅ 可操作" if dz >= 80 else ("⚠️ 空仓观望" if dz > 0 else "❌ 无数据")
    lines.append("## 一、市场状态")
    lines.append(f"")
    lines.append(f"| 维度 | 状态 |")
    lines.append(f"|------|------|")
    lines.append(f"| NQP V6 市场分级 | **{regime_label}** |")
    lines.append(f"| 今日涨停家数 | **{dz} 家** |")
    lines.append(f"| 打板短线环境 | {daban_env} |")
    lines.append(f"")

    # ── Section 2: Daban Review ──
    lines.append("## 二、打板复盘（今日）")
    lines.append(f"")
    lines.append(f"> 情绪: 涨停 **{dz} 家** → {daban_result.get('tier','?')} → "
                 f"单票 {daban_result.get('pos',0)*100:.0f}% 仓")
    lines.append(f"> {daban_result.get('note','')}")
    lines.append(f"")

    cands = daban_result.get("cands", [])
    if not cands:
        lines.append("⚠️ 今日无打板信号，空仓观望。明日条件不变：涨停 ≥80 家才出手。")
        lines.append(f"")
    else:
        lines.append("**今日收盘口径候选（若尾盘已按盘中推送买入，明日开盘无条件卖出）：**")
        lines.append(f"")
        lines.append(f"| # | 代码 | 名称 | 涨停价 | 量比 | 题材(联动) | 流通市值 |")
        lines.append(f"|---|------|------|--------|------|-----------|----------|")
        for i, c in enumerate(cands, 1):
            ltsz = f"{c['ltsz_yi']:.0f}亿" if c["ltsz_yi"] else "—"
            lines.append(f"| {i} | {c['code']} | {c['name']} | {c['price']:.2f} | "
                         f"{c['vol_ratio']:.2f} | {c['tag1']}({c['tag1_cnt']}家) | {ltsz} |")
        lines.append(f"")
        lines.append(f"⏰ **明日开盘无条件卖出**，不恋战、不补仓、不持有过夜。")
        lines.append(f"")
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
    lines.append(f"**打板短线（盘中14:32推送执行）：**")
    lines.append(f"- 情绪分档: ≥100家 25%仓 / 80-99家 12.5% / <80家 空仓 / 150+家 降半仓")
    lines.append(f"- 只打首板非一字 + 量比<1.5；≥100家时加 题材≥3家 × 流通市值<80亿")
    lines.append(f"- 次日开盘无条件卖，最多4只，不补仓")
    lines.append(f"")
    lines.append(f"**NQP V6 趋势:**")
    lines.append(f"- 严格遵守市场分级: {regime_label} → {'仅P4/P5' if regime == 'weak' else '全信号' if regime == 'bull' else '空仓等待'}")
    lines.append(f"- 硬止损12%，不补仓")
    lines.append(f"- 高位过热(S4)触发时减仓至半仓")
    lines.append(f"")

    # Footer
    lines.append("---")
    lines.append(f"*自动生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 打板短线 + NQP V6趋势追踪*")
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

    # Part 1: Daban review
    print("Running daban review...")
    daban = daban_review(today)
    if daban.get("error") == "holiday":
        print("非交易日 → 跳过复盘推送")
        sys.exit(0)

    # Part 2: NQP V6
    print("Running NQP V6 analysis...")
    nqp = nqp_v6_analysis()

    # Generate report
    report = generate_report(daban, nqp, today, tomorrow)

    # Save
    report_path = REPORTS_DIR / f"combined_{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report: {report_path}")

    # Push
    regime = nqp.get("regime", "?")
    regime_emoji = {"bull":"🟢","weak":"🟡","bear":"🔴"}.get(regime,"❓")
    dz = daban.get("day_zt", 0)
    cands = daban.get("cands", [])
    sell_names = "/".join([c["name"] for c in cands[:4]]) if cands else "空仓"
    title = f"Vibe复盘 {today} | {regime_emoji}{regime} | 涨停{dz}家 | 卖出:{sell_names}"

    # Push content
    push_lines = [f"## Vibe复盘 {today}", ""]
    push_lines.append(f"**市场状态** {regime_emoji} {regime} | 今日涨停 **{dz} 家**")
    push_lines.append("")

    push_lines.append("---")
    push_lines.append("## 📈 打板复盘")
    push_lines.append("")
    if daban.get("error"):
        push_lines.append(f"⚠️ {daban.get('note','数据异常')}")
        push_lines.append("")
    elif not cands:
        push_lines.append(f"**涨停 {dz} 家 → {daban.get('tier')} → 空仓观望**")
        push_lines.append(f"> {daban.get('note','')}")
        push_lines.append("")
    else:
        push_lines.append(f"**涨停 {dz} 家 → {daban.get('tier')} → 单票 {daban.get('pos',0)*100:.0f}% 仓**")
        push_lines.append(f"> {daban.get('note','')}")
        push_lines.append("")
        push_lines.append("**今日收盘口径候选（已按盘中推送买入的，明日开盘无条件卖出）：**")
        push_lines.append("")
        push_lines.append("| 代码 | 名称 | 涨停价 | 量比 | 题材(联动) | 市值 |")
        push_lines.append("|------|------|--------|------|-----------|------|")
        for c in cands:
            ltsz = f"{c['ltsz_yi']:.0f}亿" if c["ltsz_yi"] else "—"
            push_lines.append(f"| {c['code']} | {c['name']} | {c['price']:.2f} | "
                              f"{c['vol_ratio']:.2f} | {c['tag1']}({c['tag1_cnt']}家) | {ltsz} |")
        push_lines.append("")
        push_lines.append("⏰ **明日开盘无条件卖出**，不恋战不补仓。")
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
    push_lines.append(f"*{today} 自动生成 | 打板短线 + NQP V6趋势追踪*")
    push_wechat(title, "\n".join(push_lines))

    print("Done.")
