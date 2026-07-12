#!/usr/bin/env python3
"""
NQP V3.3 每日趋势追踪报告 — 云端全自动版 (腾讯财经)
GitHub Actions 14:30触发，生成完整10章报告 + 方糖推送
"""
import os, sys, time, re, math, json
from datetime import datetime, timedelta
from pathlib import Path
import requests

# ── 配置 ──────────────────────────────────────────
TODAY = datetime.now().strftime("%Y-%m-%d")
GITHUB_REPO = "Nikkosun-ai/nqp-signal-pipeline"
REPORT_MD = Path("reports") / f"nqp_v33_daily_{TODAY}.md"
REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
FTQQ_SEND_KEY = os.environ.get("FTQQ_SEND_KEY", "")

# ── 交易池 24只 ────────────────────────────────────
POOL = {
    "002050": "三花智控", "601689": "拓普集团", "688017": "绿的谐波",
    "002747": "埃斯顿",   "300124": "汇川技术", "002472": "双环传动",
    "000887": "中鼎股份", "603211": "晋拓股份",
    "300750": "宁德时代", "002594": "比亚迪",   "300274": "阳光电源",
    "002371": "北方华创", "688012": "中微公司", "688981": "中芯国际",
    "688256": "寒武纪",   "688041": "海光信息", "002230": "科大讯飞",
    "688111": "金山办公", "603501": "韦尔股份", "603986": "兆易创新",
    "600584": "长电科技", "688072": "拓荆科技", "301269": "华大九天",
    "603019": "中科曙光",
}

# ── 赛道映射 ──────────────────────────────────────
SECTORS = {
    "机器人":  ["002050","601689","688017","002747","300124","002472","000887","603211"],
    "新能源":  ["300750","002594","300274"],
    "半导体":  ["002371","688012","688981","603501","603986","600584","688072","301269"],
    "AI/算力": ["688256","688041","002230","688111","603019"],
}
SEC_EMOJI = {"机器人":"🤖", "新能源":"🔋", "半导体":"💾", "AI/算力":"🧠"}

# ═══════════════════════════════════════════════════
# 数据采集层
# ═══════════════════════════════════════════════════

def fetch_realtime(codes):
    """腾讯财经实时行情 (qt.gtimg.cn)，批量查询"""
    url = f"https://qt.gtimg.cn/q=" + ",".join(f"sh{c}" if c.startswith("6") else f"sz{c}" for c in codes)
    try:
        resp = requests.get(url, timeout=10)
        resp.encoding = "gbk"
    except Exception as e:
        print(f"[ERR] 行情请求失败: {e}")
        return {}

    quotes = {}
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        m = re.search(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        code_raw, data = m.group(1), m.group(2)
        code = code_raw[2:]
        if code not in POOL:
            continue
        fields = data.split("~")
        try:
            quotes[code] = {
                "price":    float(fields[3])  if fields[3]  else 0,
                "change_pct": float(fields[32]) if fields[32] else 0,
                "pe":       float(fields[39]) if fields[39]  else 0,
                "market_cap": float(fields[45]) if fields[45] and fields[45].replace(".","").isdigit() else 0,
                "volume":   float(fields[6])  if fields[6]  else 0,
                "high":     float(fields[33]) if fields[33] else 0,
                "low":      float(fields[34]) if fields[34] else 0,
                "open":     float(fields[5])  if fields[5]  else 0,
                "pre_close": float(fields[4]) if fields[4]  else 0,
            }
        except (ValueError, IndexError):
            quotes[code] = {"price": 0, "change_pct": 0, "pe": 0, "market_cap": 0,
                            "volume": 0, "high": 0, "low": 0, "open": 0, "pre_close": 0}
    return quotes


def fetch_kline(code, days=250):
    """腾讯财经日K线 (web.ifzq.gtimg.cn)"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days},qfq"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        klines = data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", []) or \
                 data.get("data", {}).get(f"{prefix}{code}", {}).get("day", [])
        if not klines:
            return []
        result = []
        for k in klines:
            result.append({
                "date": k[0],
                "open": float(k[1]), "close": float(k[2]),
                "high": float(k[3]), "low": float(k[4]),
                "volume": float(k[5]),
            })
        return result
    except Exception as e:
        print(f"[WARN] {code} K线失败: {e}")
        return []


# ═══════════════════════════════════════════════════
# 技术指标计算
# ═══════════════════════════════════════════════════

def calc_indicators(bars):
    if not bars or len(bars) < 20:
        return {"ma200": 0, "atr14": 0, "dev_ma200": 0, "slope20": 0, "closes": [], "vol_ratio": 1}
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    ma200 = sum(closes[-200:]) / min(200, len(closes)) if len(closes) >= 20 else closes[-1]
    trs = []
    for i in range(-14, 0):
        if i >= -len(closes):
            h, l, pc = highs[i], lows[i], closes[i-1] if i-1 >= -len(closes) else closes[i]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr14 = sum(trs) / len(trs) if trs else 0
    dev_ma200 = (closes[-1] - ma200) / ma200 * 100 if ma200 > 0 else 0
    slope20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
    vol5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else 1
    vol20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1
    vol_ratio = vol5 / vol20 if vol20 > 0 else 1
    return {"ma200": ma200, "atr14": atr14, "dev_ma200": dev_ma200, "slope20": slope20,
            "closes": closes, "vol_ratio": vol_ratio}


def calc_trend_score(dev, atr, price):
    score = 50
    if dev > 20: score += min(25, (dev-20)*1.25)
    elif dev > 5: score += dev*1.0
    elif dev < -15: score -= min(25, abs(dev+15)*1.25)
    elif dev < 0: score += dev*0.5
    if price > 0 and atr > 0:
        atr_pct = atr/price*100
        if atr_pct < 2: score += 10
        elif atr_pct < 4: score += 5
        else: score -= 5
    return max(0, min(100, score))


def detect_signals(price, bars, ind):
    if not bars or len(bars) < 20:
        return {"buy": [], "sell": [], "buy_details": [], "sell_details": []}
    closes = ind["closes"]
    ma200 = ind["ma200"]
    dev = ind["dev_ma200"]
    atr = ind["atr14"]
    slope = ind["slope20"]
    vol_ratio = ind["vol_ratio"]
    current = closes[-1]
    buy, sell, buy_d, sell_d = [], [], [], []

    # P1: MA200回调买点
    if ma200 > 0 and abs(dev) < 5 and slope > 0:
        buy.append(("P1", 0.75))
        buy_d.append(f"P1(MA200回调): 现价{price:.1f}距MA200{ma200:.1f}约{dev:+.1f}%，趋势向上")

    # P2: 超跌反弹
    if dev < -18 and closes[-1] > closes[-2] and slope > -30:
        buy.append(("P2", 0.65))
        buy_d.append(f"P2(超跌反弹): 乖离{dev:+.1f}%，今日反弹，关注企稳")

    # P3: 突破确认
    if dev > 2 and dev < 15 and vol_ratio > 1.3 and closes[-1] > closes[-5]:
        buy.append(("P3", 0.70))
        buy_d.append(f"P3(突破确认): 乖离{dev:+.1f}%，放量{vol_ratio:.1f}x，突破MA200")

    # P4: 趋势延续
    if 5 < dev < 35 and slope > 3 and ma200 > 0 and closes[-1] > closes[-10]:
        buy.append(("P4", 0.80))
        buy_d.append(f"P4(趋势延续): 乖离{dev:+.1f}%，20日斜率{slope:+.1f}%，健康上升")

    # P5: 加速突破
    if 10 < dev < 60 and slope > 5 and vol_ratio > 1.5:
        highs_10 = max(b["high"] for b in bars[-10:])
        lows_10 = min(b["low"] for b in bars[-10:])
        range_pct = (highs_10 - lows_10) / current * 100 if current > 0 else 100
        if range_pct < 8:
            buy.append(("P5", 0.60))
            buy_d.append(f"P5(加速突破): 10日振幅{range_pct:.1f}%，紧凑盘整+放量{vol_ratio:.1f}x")

    # P6: 深度价值
    if dev < -30:
        buy.append(("P6", 0.50))
        buy_d.append(f"P6(深度价值): 乖离{dev:+.1f}%，极端超跌，左侧试探")

    # S1: 硬止损
    if ma200 > 0 and current < ma200 * 0.98:
        sell.append(("S1", 0.90))
        sell_d.append(f"S1(硬止损): 现价{price:.1f}已跌破MA200{ma200:.1f}（-2%）")

    # S2: MA200破位
    if ma200 > 0 and current < ma200 and slope < 0:
        sell.append(("S2", 0.80))
        sell_d.append(f"S2(MA200破位): 跌破MA200且趋势转负")

    # S3: 深度回调
    if ma200 > 0 and current < ma200 * 0.95:
        sell.append(("S3", 0.70))
        sell_d.append(f"S3(深度回调): 距MA200回调超5%")

    # S4: 高位过热 四档
    if dev > 80:
        sell.append(("S4", 0.95))
        sell_d.append(f"S4(🔥极端过热): 乖离{dev:+.1f}% ≥80%，风险极高")
    elif dev > 60:
        sell.append(("S4", 0.80))
        sell_d.append(f"S4(⚠️高危过热): 乖离{dev:+.1f}% 60-80%")
    elif dev > 40:
        sell.append(("S4", 0.65))
        sell_d.append(f"S4(🌡️过热): 乖离{dev:+.1f}% 40-60%")
    elif dev > 25:
        sell.append(("S4", 0.50))
        sell_d.append(f"S4(温和过热): 乖离{dev:+.1f}% 25-40%")

    # S5: 趋势弱化
    if dev < -5 and len(closes) >= 10 and closes[-1] < closes[-10]:
        sell.append(("S5", 0.70))
        sell_d.append(f"S5(趋势弱化): 10日下跌且乖离为负{dev:+.1f}%")

    return {"buy": buy, "sell": sell, "buy_details": buy_d, "sell_details": sell_d}


# ═══════════════════════════════════════════════════
# 报告生成 — 完整11章
# ═══════════════════════════════════════════════════

def generate_report(quotes, all_bars):
    L = []
    def a(s=""): L.append(s)

    # ═══ Ch1: 策略总览 ═══
    a(f"# NQP V3.3 每日趋势追踪报告 {TODAY}")
    a()
    a(f"> 📡 数据源：腾讯财经 | ⏰ 触发时间：14:30 北京时间 | 🤖 GitHub Actions 云端引擎")
    a()
    a("## 1. 策略总览")
    a()
    a("| 参数 | 值 |")
    a("|---|---|")
    a(f"| 交易池 | {len(POOL)} 只标的 |")
    a(f"| 赛道 | 机器人 / 新能源 / 半导体 / AI算力 |")
    a(f"| 行情成功率 | {sum(1 for q in quotes.values() if q.get('price',0)>0)}/{len(POOL)} |")
    a(f"| K线成功率 | {len(all_bars)}/{len(POOL)} |")
    a("| 买入模式 | P1(MA200回调) P2(超跌反弹) P3(突破确认) P4(趋势延续) P5(加速突破) P6(深度价值) |")
    a("| 卖出模式 | S1(硬止损) S2(MA200破位) S3(深度回调) S4(高位过热) S5(趋势弱化) |")
    a()

    # 预计算
    rows = []
    for c, name in POOL.items():
        q = quotes.get(c, {})
        bars = all_bars.get(c)
        ind = calc_indicators(bars) if bars else {}
        price = q.get("price", 0) or 0
        dev = ind.get("dev_ma200", 0)
        atr = ind.get("atr14", 0)
        trend = calc_trend_score(dev, atr, price)
        ma200 = ind.get("ma200", 0)
        slope = ind.get("slope20", 0)
        vol_r = ind.get("vol_ratio", 1)
        pe = q.get("pe", 0) or 0
        mc = q.get("market_cap", 0) or 0
        chg = q.get("change_pct", 0) or 0
        sigs = detect_signals(price, bars, ind) if bars else {"buy":[],"sell":[],"buy_details":[],"sell_details":[]}
        sec = "其他"
        for sn, codes in SECTORS.items():
            if c in codes: sec = sn; break
        rows.append({"code":c,"name":name,"price":price,"dev":dev,"trend":trend,"atr":atr,
                     "ma200":ma200,"pe":pe,"mc":mc,"chg":chg,"slope":slope,"vol_r":vol_r,
                     "sector":sec,"signals":sigs,"bars":bars,"ind":ind})

    ranked = sorted(rows, key=lambda r: r["trend"], reverse=True)

    # ═══ Ch2: 赛道热度 ═══
    a("## 2. 赛道热度分析")
    a()
    a("| 赛道 | 标的数 | 均PE | 均趋势分 | 均乖离 | 过热数 | 热度 |")
    a("|---|---|---|---|---|---|")
    sector_stats = {}
    for sn, codes in SECTORS.items():
        sr = [r for r in rows if r["sector"]==sn]
        n = len(sr)
        ap = sum(r["pe"] for r in sr if r["pe"]>0)/max(1,sum(1 for r in sr if r["pe"]>0))
        at = sum(r["trend"] for r in sr)/n if n else 0
        ad = sum(r["dev"] for r in sr)/n if n else 0
        hn = sum(1 for r in sr if r["dev"]>25)
        heat_label = "🔥🔥🔥极热" if ad>30 else ("🔥🔥偏热" if ad>15 else ("🔥温和" if ad>0 else ("🌡️中性" if ad>-10 else "❄️偏冷")))
        sector_stats[sn] = {"n":n,"avg_pe":ap,"avg_trend":at,"avg_dev":ad,"hot_n":hn,"heat":heat_label}
        em = SEC_EMOJI.get(sn,"")
        a(f"| {em} {sn} | {n} | {ap:.1f} | {at:.0f} | {ad:+.1f}% | {hn}/{n} | {heat_label} |")
    a()

    a("### 赛道轮动解读")
    a()
    ss = sorted(sector_stats.items(), key=lambda x: x[1]["avg_dev"], reverse=True)
    top, bot = ss[0], ss[-1]
    a(f"- **最强**: {SEC_EMOJI.get(top[0],'')} {top[0]}（均乖离{top[1]['avg_dev']:+.1f}%，均趋势分{top[1]['avg_trend']:.0f}）")
    if top[1]["hot_n"]>0:
        a(f"  - ⚠️ {top[1]['hot_n']}/{top[1]['n']}只已过热，注意高位回落")
    a(f"- **最弱**: {SEC_EMOJI.get(bot[0],'')} {bot[0]}（均乖离{bot[1]['avg_dev']:+.1f}%，均趋势分{bot[1]['avg_trend']:.0f}）")
    if bot[1]["avg_dev"]<-10:
        a(f"  - 💡 深度回调，关注P2/P6机会")
    a()

    # ═══ Ch3: 交易池 ═══
    a("## 3. 交易池一览")
    a()
    a("| 代码 | 名称 | 赛道 | 现价 | 涨跌 | PE | MA200乖离 | 趋势分 | ATR14 | 放量比 |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for r in ranked:
        ps = f"{r['pe']:.1f}" if r['pe']>0 else "—"
        a_s = f"{r['atr']:.2f}" if r['atr']>0 else "—"
        vs = f"{r['vol_r']:.1f}x" if r['vol_r'] else "—"
        a(f"| {r['code']} | {r['name']} | {r['sector']} | {r['price']:.2f} | {r['chg']:+.1f}% | {ps} | {r['dev']:+.1f}% | {r['trend']:.0f} | {a_s} | {vs} |")
    a()

    # ═══ Ch4: 买入信号 ═══
    a("## 4. 买入信号 (P1-P6)")
    a()
    a("| 模式 | 含义 | 条件 |")
    a("|---|---|---|")
    a("| P1 | MA200回调 | 价格在MA200±5%，趋势向上 |")
    a("| P2 | 超跌反弹 | 乖离<-18%+今日反弹 |")
    a("| P3 | 突破确认 | 突破MA200+2%+放量>1.3x |")
    a("| P4 | 趋势延续 | 乖离5-35%+20日斜率>3% |")
    a("| P5 | 加速突破 | 乖离10-60%+10日振幅<8%+放量>1.5x |")
    a("| P6 | 深度价值 | 乖离<-30%，左侧试探 |")
    a()

    buy_found = False
    for r in rows:
        sd = r["signals"]
        if sd["buy"]:
            buy_found = True
            a(f"### ✅ {r['code']} {r['name']}（{r['sector']}）")
            a(f"> 现价:{r['price']:.2f} | 乖离:{r['dev']:+.1f}% | 趋势分:{r['trend']:.0f} | PE:{r['pe']:.1f}" if r['pe']>0 else f"> 现价:{r['price']:.2f} | 乖离:{r['dev']:+.1f}% | 趋势分:{r['trend']:.0f}")
            a()
            for d in sd["buy_details"]: a(f"- **{d}**")
            a()
            buy_p = round(r["ma200"]*1.02,2) if r["ma200"]>0 and r["dev"]>0 else round(r["price"]*0.92,2)
            tgt = round(r["price"]*1.15,2) if r["trend"]>=50 else round(r["price"]*1.08,2)
            atr_s = round(r["price"]-r["atr"]*2,2) if r["atr"]>0 else round(r["price"]*0.95,2)
            hs = round(r["price"]*0.88,2)
            a("| 参数 | 值 ||---|---|")
            a(f"| 建议买入价 | {buy_p:.2f} |")
            a(f"| 卖出目标价 | {tgt:.2f} |")
            a(f"| ATR止损价 | {atr_s:.2f} |")
            a(f"| 硬止损价(-12%) | {hs:.2f} |")
            a()

    if not buy_found:
        a("> 📊 今日暂无买入信号触发")
        a()
        a("### 未触发原因分析")
        a()
        near = []
        for r in rows:
            d,sl,vr = r["dev"],r["slope"],r["vol_r"]
            if 3<abs(d)<8 and sl>0: near.append(f"- {r['code']} {r['name']}: 乖离{d:+.1f}%，近P1（需<5%）")
            if -22<d<-15: near.append(f"- {r['code']} {r['name']}: 乖离{d:+.1f}%，近P2（需<-18%+反弹）")
            if 0<d<18 and vr>1.1: near.append(f"- {r['code']} {r['name']}: 乖离{d:+.1f}%+放量{vr:.1f}x，近P3")
        if near:
            a("以下接近触发：")
            for n in near[:8]: a(n)
        else:
            a("22只标的均不接近买入触发。整体趋势可能偏弱或已完成上涨。")
            a("- 高位：等回调P1/P3 | 低位：等P2/P6")
        a()

    # ═══ Ch5: 卖出预警 ═══
    a("## 5. 卖出预警 (S1-S5)")
    a()
    a("| 模式 | 含义 | S4分档 |")
    a("|---|---|---|")
    a("| S1 | 硬止损 | 跌破MA200 -2% |")
    a("| S2 | MA200破位 | 跌破MA200+趋势转负 |")
    a("| S3 | 深度回调 | 距MA200回调>5% |")
    a("| S4 | 高位过热 | 🔥≥80%极端 / ⚠️60-80%高危 / 🌡️40-60%过热 / 25-40%温和 |")
    a("| S5 | 趋势弱化 | 10日下跌+乖离为负 |")
    a()

    sell_found = False
    for r in rows:
        sd = r["signals"]
        if sd["sell"]:
            sell_found = True
            s4l = [d for d in sd["sell_details"] if d.startswith("S4")]
            otl = [d for d in sd["sell_details"] if not d.startswith("S4")]
            icon = "🔴" if any("S1" in d for d in sd["sell_details"]) else "🟡"
            a(f"### {icon} {r['code']} {r['name']}（{r['sector']}）")
            a(f"> 现价:{r['price']:.2f} | 乖离:{r['dev']:+.1f}% | 趋势分:{r['trend']:.0f}")
            a()
            for d in otl+s4l: a(f"- **{d}**")
            a()

    if not sell_found:
        a("> ✅ 今日暂无卖出预警 — 所有标的处于安全区间")
        a()
        a("### 潜在关注")
        a()
        nr = []
        for r in rows:
            if r["dev"]>20: nr.append(f"- {r['code']} {r['name']}: 乖离{r['dev']:+.1f}%，近S4(>25%)")
            if r["dev"]<-3 and r["slope"]<0: nr.append(f"- {r['code']} {r['name']}: 乖离{r['dev']:+.1f}%+趋势转负，近S5")
        for n in nr[:8]: a(n)
        if not nr: a("- 所有标的乖离正常，无迫近预警")
        a()

    # ═══ Ch6: 逐股建议 ═══
    a("## 6. 逐股操作建议")
    a()

    for r in ranked:
        dv, tr, at, pr = r["dev"], r["trend"], r["atr"], r["price"]
        ma, pe = r["ma200"], r["pe"]
        if pr <= 0: continue

        if tr>=65 and 5<dv<40: action, pos = "🟢 买入/加仓", "标准仓位"
        elif tr>=50 and dv>-5: action, pos = "🟡 持有", "现有仓位"
        elif tr>=50 and dv>40: action, pos = "🟠 高位持有/分批止盈", "减至半仓"
        elif tr<35 or dv<-15: action, pos = "🔴 观望/等待", "不入场"
        else: action, pos = "🟡 持有/观望", "轻仓观察"

        bp = round(ma*1.02,2) if dv>0 and ma>0 else round(pr*0.92,2)
        tgt = round(pr*1.15,2) if tr>=50 else (round(pr*1.10,2) if tr>=35 else round(pr*1.05,2))
        ats = round(pr-at*2,2) if at>0 else round(pr*0.95,2)
        hs = round(pr*0.88,2)

        if dv>30: ana = f"高位（乖离{dv:+.1f}%），不建议追高。持仓可分批止盈。"
        elif dv>15: ana = f"趋势强但乖离偏高({dv:+.1f}%)。持有享受趋势，空仓等P1回调。"
        elif dv>0: ana = f"温和上升({dv:+.1f}%)。MA200({ma:.1f})附近是优质P1买点。"
        elif dv>-10: ana = f"围绕MA200({ma:.1f})震荡({dv:+.1f}%)。等突破P3或超跌P2。"
        elif dv>-25: ana = f"弱势回调({dv:+.1f}%)。关注企稳，连续3日不创新低可P2试探。"
        else: ana = f"深度下跌({dv:+.1f}%)。P6左侧机会，等放量阳线确认。"

        a(f"### {r['code']} {r['name']}（{r['sector']}）")
        a(f"> {ana}")
        a()
        a("| 指标 | 值 ||---|---|")
        a(f"| 现价 | {pr:.2f} |")
        a(f"| MA200 | {ma:.2f}" if ma>0 else "| MA200 | — |")
        a(f"| MA200乖离 | {dv:+.1f}% |")
        a(f"| 趋势分 | {tr:.0f}/100 |")
        a(f"| PE(TTM) | {pe:.1f}" if pe>0 else "| PE(TTM) | — |")
        a(f"| 建议买入价 | {bp:.2f} |")
        a(f"| 卖出目标价 | {tgt:.2f} |")
        a(f"| ATR止损价 | {ats:.2f} |")
        a(f"| 硬止损价(-12%) | {hs:.2f} |")
        a(f"| 操作建议 | **{action}** |")
        a(f"| 建议仓位 | {pos} |")
        a()

    # ═══ Ch7: 信号矩阵 ═══
    a("## 7. 信号矩阵 (22x11)")
    a()
    a("| 代码 | 名称 | P1 | P2 | P3 | P4 | P5 | P6 | S1 | S2 | S3 | S4 | S5 |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in ranked:
        bs = set(s[0] for s in r["signals"]["buy"])
        ss = set(s[0] for s in r["signals"]["sell"])
        cells = [r["code"], r["name"]]
        for sig in ["P1","P2","P3","P4","P5","P6","S1","S2","S3","S4","S5"]:
            cells.append("🟢" if sig in bs else ("🔴" if sig in ss else "—"))
        a("| " + " | ".join(cells) + " |")
    a()

    # ═══ Ch8: 优先级操作 ═══
    a("## 8. 优先级操作清单")
    a()
    p0 = [r for r in rows if any(s[0] in ("S1","S2") for s in r["signals"]["sell"])]
    p1b = [r for r in rows if r["signals"]["buy"] and r not in p0]
    p1s = [r for r in rows if any("极端" in d or "高危" in d for d in r["signals"]["sell_details"]) and r not in p0]
    p2 = [r for r in rows if r["dev"]>20 and r not in p0 and r not in p1b and r not in p1s]

    if p0:
        a("### 🔴 P0 必须操作")
        a("| 标的 | 操作 | 数量 | 价格 | 条件 ||---|---|---|---|---|")
        for r in p0: a(f"| {r['code']} {r['name']} | 立即卖出 | 全部 | 市价 | S1/S2触发 |")
        a()
    if p1b:
        a("### 🟢 P1 买入")
        a("| 标的 | 操作 | 模式 | 买入价 | 止损价 ||---|---|---|---|---|")
        for r in p1b:
            bp = round(r["ma200"]*1.02,2) if r["ma200"]>0 else round(r["price"]*0.92,2)
            sp = round(r["price"]-r["atr"]*2,2) if r["atr"]>0 else round(r["price"]*0.95,2)
            modes = ",".join(s[0] for s in r["signals"]["buy"])
            a(f"| {r['code']} {r['name']} | 买入 | {modes} | {bp:.2f} | {sp:.2f} |")
        a()
    if p1s:
        a("### 🟠 P1 减仓（高位过热）")
        a("| 标的 | 操作 | 过热 | 现价 | 建议 ||---|---|---|---|---|")
        for r in p1s:
            lv = "🔥极端" if r["dev"]>80 else ("⚠️高危" if r["dev"]>60 else ("🌡️过热" if r["dev"]>40 else "温和"))
            a(f"| {r['code']} {r['name']} | 减仓/止盈 | {lv}({r['dev']:+.1f}%) | {r['price']:.2f} | 减至半仓 |")
        a()
    if p2:
        a("### 🟡 P2 关注")
        for r in p2[:5]: a(f"- **{r['code']} {r['name']}**: 乖离{r['dev']:+.1f}%，近S4过热阈值(25%)。继续上涨建议分批止盈。")
        a()
    if not p0 and not p1b and not p1s and not p2:
        a("> 今日无优先级操作，所有标的处于正常区间。")
        a()
        hot_secs = [sn for sn,st in sector_stats.items() if st["avg_dev"]>20]
        if hot_secs: a(f"💡 关注{'、'.join(hot_secs)}赛道 — 均乖离较高，提前规划止盈。")
        a()

    # ═══ Ch9: 风险评估 ═══
    a("## 9. 风险评估（6维矩阵）")
    a()
    max_sp = max(st["n"] for st in sector_stats.values())/len(POOL)*100
    hdn = sum(1 for r in rows if r["dev"]>25)
    dfn = sum(1 for r in rows if r["dev"]<-15)
    hpn = sum(1 for r in rows if r["pe"]>100)
    ac = sum(r["chg"] for r in rows)/len(rows)
    a("| 风险维度 | 评估 | 等级 | 说明 ||---|---|---|---|")
    a(f"| 赛道集中度 | {max_sp:.0f}%集中 | {'🔴高' if max_sp>50 else '🟡中' if max_sp>35 else '🟢低'} | 半导体占{max_sp:.0f}% |")
    a(f"| 乖离风险 | {hdn}过热/{dfn}深调 | {'🔴高' if hdn>5 else '🟡中' if hdn>2 else '🟢低'} | 过热{hdn}只，超跌{dfn}只 |")
    a(f"| 估值风险 | {hpn}只PE>100 | {'🔴高' if hpn>5 else '🟡中' if hpn>2 else '🟢低'} | 高PE数:{hpn} |")
    a(f"| 情绪指标 | 均涨跌{ac:+.1f}% | {'🔴过热' if ac>3 else '🟢正常' if ac>-2 else '🔴恐慌'} | 日内均涨跌 |")
    a("| 流动性 | 腾讯财经实时 | 🟢正常 | 数据源稳定 |")
    a("| 仓位适配 | 建议≤70% | 🟡中性 | 根据个人持仓调整 |")
    a()

    # ═══ Ch10: 明日关注 ═══
    a("## 10. 明日关注")
    a()
    a("### 📈 上行")
    a("- 关注P3突破：MA200附近放量突破")
    np3 = [r for r in rows if 0<r["dev"]<18 and r["vol_r"]>1.1]
    for r in np3[:5]: a(f"  - {r['code']} {r['name']}: 乖离{r['dev']:+.1f}%+放量{r['vol_r']:.1f}x")
    if not np3: a("  - 暂无P3候选")
    a("- 高位S4监控：乖离>25%的标的设止盈")
    a()
    a("### 📉 下行")
    a("- 关注P1 MA200回调买点")
    np1 = [r for r in rows if 3<abs(r["dev"])<10 and r["slope"]>-5]
    for r in np1[:5]:
        m=r["ma200"]
        a(f"  - {r['code']} {r['name']}: MA200={m:.1f}，回调至{m*1.02:.1f}触发P1")
    if not np1: a("  - 暂无P1候选")
    a("- P2超跌反弹：深度回调标的是否放量阳线")
    a()
    a("### ↔️ 震荡")
    a("- 持有趋势分≥50标的，耐心等待")
    a("- 避免追涨杀跌，严格止损纪律")
    a("- 关注赛道轮动：资金可能从过热→冷却")
    a()

    # ═══ Ch11: 数据质量 ═══
    a("## 11. 数据质量")
    a()
    a("| 项目 | 状态 ||---|---|")
    a("| 行情源 | ✅ 腾讯财经 qt.gtimg.cn |")
    a("| K线源 | ✅ 腾讯财经 web.ifzq.gtimg.cn |")
    a(f"| 行情成功率 | {sum(1 for q in quotes.values() if q.get('price',0)>0)}/{len(POOL)} |")
    a(f"| K线成功率 | {len(all_bars)}/{len(POOL)} |")
    a("| 延迟 | ~1-3分钟 |")
    a(f"| 生成时间 | {datetime.now().isoformat()} |")
    a("| 引擎 | NQP V3.3 腾讯财经版 |")
    a()
    a("---")
    a("*⚠️ 免责声明：本报告由 GitHub Actions 自动生成，仅供研究参考，不构成投资建议。投资有风险，入市需谨慎。*")
    a(f"*下次更新：下一交易日 14:30 | NQP V3.3 | 腾讯财经*")

    return "\n".join(L)


# ═══════════════════════════════════════════════════
# 方糖推送
# ═══════════════════════════════════════════════════

def extract_push_content(report):
    """从完整报告中提取摘要推送"""
    lines = report.split("\n")
    parts = []

    # 标题
    parts.append(f"## NQP V3.3 {TODAY}")
    parts.append("")

    # 赛道热度
    in_sec = False
    for line in lines:
        if "赛道热度分析" in line: in_sec = True; continue
        if in_sec and line.startswith("## "): break
        if in_sec and line.strip() and (line.startswith("|") or line.startswith("-")):
            parts.append(line)
            if len(parts) > 20: break
    parts.append("")

    # 买入信号摘要
    buy_section = False
    buy_hits = []
    for line in lines:
        if "买入信号" in line and line.startswith("## "):
            buy_section = True; continue
        if buy_section and line.startswith("## "): break
        if buy_section:
            if "✅" in line and "###" in line: buy_hits.append(line)
            elif line.startswith("> ") and buy_hits: buy_hits[-1] += " " + line.strip("> ")

    parts.append("### 📈 买入信号")
    if buy_hits:
        for h in buy_hits[:5]: parts.append(h)
    else:
        # 找接近触发
        near_any = False
        for line in lines:
            if "买入信号" in line and line.startswith("## "):
                near_any = True; continue
            if near_any and line.startswith("## "): break
            if near_any and line.startswith("- ") and "近" in line:
                parts.append(line)
                if len(parts) > 30: break
        if not any("近" in p for p in parts[-8:]):
            parts.append("- 今日无买入信号触发")
    parts.append("")

    # 卖出信号摘要
    sell_section = False
    sell_hits = []
    for line in lines:
        if "卖出预警" in line and line.startswith("## "):
            sell_section = True; continue
        if sell_section and line.startswith("## "): break
        if sell_section and ("### 🔴" in line or "### 🟡" in line): sell_hits.append(line)

    parts.append("### 📉 卖出信号")
    if sell_hits:
        for h in sell_hits[:5]: parts.append(h)
    else:
        parts.append("- ✅ 无卖出预警")
    parts.append("")

    # 优先级操作
    ops_sec = False
    for line in lines:
        if "优先级操作清单" in line: ops_sec = True; continue
        if ops_sec and line.startswith("## "): break
        if ops_sec and line.strip() and not line.startswith(">"):
            parts.append(line)
            if len(parts) > 45: break

    # 限制长度
    result = "\n".join(parts)
    if len(result) > 3800:
        result = result[:3700] + "\n\n> ⚠️ 截断，点击链接看完整报告"

    return result


def send_ftqq(content):
    if not FTQQ_SEND_KEY:
        print("[WARN] FTQQ_SEND_KEY 未设置")
        return
    title = f"NQP V3.3 {TODAY}"
    filename = f"nqp_v33_daily_{TODAY}.md"
    url = f"https://github.com/{GITHUB_REPO}/blob/main/reports/{filename}"
    desp = content + f"\n\n---\n📄 [完整11章报告 →]({url})"
    try:
        resp = requests.post(f"https://sctapi.ftqq.com/{FTQQ_SEND_KEY}.send",
                             data={"title": title, "desp": desp}, timeout=15)
        r = resp.json()
        print(f"✅ 方糖推送成功" if r.get("code")==0 else f"❌ 方糖失败: {r}")
    except Exception as e:
        print(f"❌ 方糖异常: {e}")


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════

def main():
    print(f"[{datetime.now()}] NQP V3.3 完整11章报告")
    codes = list(POOL.keys())

    quotes = fetch_realtime(codes)
    print(f"[INFO] 行情: {sum(1 for q in quotes.values() if q.get('price',0)>0)}/{len(codes)}")

    all_bars = {}
    for i, c in enumerate(codes):
        try:
            bars = fetch_kline(c, 250)
            if bars: all_bars[c] = bars
        except Exception as e:
            print(f"[WARN] {c}: {e}")
        if i % 5 == 4: time.sleep(0.5)
    print(f"[INFO] K线: {len(all_bars)}/{len(codes)}")

    report = generate_report(quotes, all_bars)
    REPORT_MD.write_text(report, encoding="utf-8")
    print(f"[INFO] 报告: {REPORT_MD} ({len(report)}字符)")

    push = extract_push_content(report)
    send_ftqq(push)
    print(f"[{datetime.now()}] 完成")


if __name__ == "__main__":
    main()
