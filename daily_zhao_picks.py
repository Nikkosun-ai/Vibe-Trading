#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Zhao Laoge Strategy Stock Screener
Runs every trading day after market close (17:37 HKT)
Generates tomorrow's watchlist and saves to daily_reports/
"""
from __future__ import annotations

import sys, io, os, json, time, random, requests, re
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

# ── Config ────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "daily_reports"
REPORTS_DIR.mkdir(exist_ok=True)

# ServerChan push config (https://sct.ftqq.com/)
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SERVERCHAN_URL = "https://sctapi.ftqq.com/{sendkey}.send"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DT_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Rate limiting ─────────────────────────────
_session = requests.Session()
_session.headers.update({"User-Agent": UA})
_last_call = [0.0]
_MIN_INTERVAL = 0.8

def _get(url, **kw):
    wait = _MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.3))
    try:
        return _session.get(url, timeout=kw.pop("timeout", 15), **kw)
    finally:
        _last_call[0] = time.time()

# ── Data fetchers ─────────────────────────────
def fetch_dragon_tiger(date_str: str | None = None, start: str = "", end: str = "") -> list[dict]:
    """Fetch dragon tiger board data for a single date or date range"""
    if date_str:
        start, end = date_str, date_str
    data, page = [], 1
    while True:
        params = {
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": "ALL",
            "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')",
            "pageNumber": str(page), "pageSize": "500",
            "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }
        try:
            r = _get(DT_URL, params=params,
                     headers={"Referer": "https://data.eastmoney.com/"}, timeout=20)
            d = r.json()
            batch = (d.get("result") or {}).get("data") or []
            data.extend(batch)
            pages = (d.get("result") or {}).get("totalPage", 1)
            if page >= pages or len(batch) < 500:
                break
            page += 1
            time.sleep(1.2)
        except Exception as e:
            print(f"[WARN] DT fetch error ({start}~{end}): {e}")
            break
    return data

def fetch_concept_blocks(code: str) -> list[dict]:
    """Fetch concept board membership for a stock"""
    market = 1 if code.startswith("6") else 0
    params = {
        "fltt": "2", "invt": "2", "secid": f"{market}.{code}",
        "spt": "3", "pi": "0", "pz": "200", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    try:
        r = _get("https://push2.eastmoney.com/api/qt/slist/get", params=params,
                 headers={"Referer": "https://quote.eastmoney.com/"})
        d = r.json()
        diff = (d.get("data") or {}).get("diff") or {}
        items = diff.values() if isinstance(diff, dict) else diff
        return [{"name": it.get("f14",""), "code": it.get("f12",""),
                 "change": it.get("f3",""), "leader": it.get("f128","")}
                for it in items]
    except Exception as e:
        return []

def fetch_thaihot(date_str: str | None = None) -> list[dict]:
    """Fetch THS daily hot stocks with reason tags"""
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return []
        return data.get("data") or []
    except Exception:
        return []

# ── Strategy filters ──────────────────────────
def is_first_board(code: str, recent_past: set[str]) -> bool:
    return code not in recent_past

def score_candidate(c: dict) -> float:
    """Score a candidate using Zhao's criteria (max ~40 pts)"""
    score = 0.0
    # Net buy amount (up to 20)
    nb = c["net_buy_wan"]
    if nb >= 10000:      score += 20
    elif nb >= 5000:     score += 15
    elif nb >= 2000:     score += 10
    elif nb >= 1000:     score += 5
    else:                score += 2

    # Turnover sweet spot (10-30%)
    to = c["turnover_pct"]
    if 10 <= to <= 30:   score += 10
    elif 5 <= to <= 40:  score += 5

    # Buy/sell ratio
    if c["sell_wan"] > 0:
        ratio = c["buy_wan"] / c["sell_wan"]
        if ratio >= 3:   score += 8
        elif ratio >= 2: score += 5
        elif ratio >= 1.5: score += 3

    # Sector heat (will be added later)
    c["_base_score"] = round(score, 1)
    return score

# ── Main ─────────────────────────────────────
def run_screening(target_date: str | None = None):
    """Run the daily screening and return the report text"""
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    tomorrow = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    tmrw_weekday = datetime.strptime(tomorrow, "%Y-%m-%d").strftime("%A")
    today_weekday = datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")

    print(f"Date: {target_date} ({today_weekday})")
    print(f"Target: {tomorrow} ({tmrw_weekday})")

    # Step 1: Today's dragon tiger
    print("Fetching dragon tiger data...")
    today_data = fetch_dragon_tiger(target_date)
    if not today_data:
        print("No dragon tiger data for today (may not be a trading day)")
        return None
    print(f"  Got {len(today_data)} records")

    # Step 2: Past 5 trading days (for first-board filter)
    past_start = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    past_end = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    past_data = fetch_dragon_tiger(start=past_start, end=past_end) if past_start < target_date else []
    recent_codes = {r.get("SECURITY_CODE","") for r in past_data}

    # Step 3: Filter
    candidates, seen = [], set()
    for row in today_data:
        code = row.get("SECURITY_CODE", "")
        name = row.get("SECURITY_NAME_ABBR", "")
        if code in seen: continue
        seen.add(code)

        if "ST" in name.upper(): continue
        chg = float(row.get("CHANGE_RATE") or 0)
        if chg < 9: continue  # must be limit-up direction

        turnover = float(row.get("TURNOVERRATE") or 0)
        if turnover < 3: continue

        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if net_buy <= 0: continue

        close = float(row.get("CLOSE_PRICE") or 0)
        if close <= 0: continue

        reason = row.get("EXPLANATION", "")
        first_bd = is_first_board(code, recent_codes)
        multi_day = any(kw in reason for kw in ["连续","三日","累计"])

        candidates.append({
            "code": code, "name": name, "close": close,
            "change_pct": chg, "turnover_pct": turnover,
            "net_buy_wan": net_buy,
            "buy_wan": (row.get("BILLBOARD_BUY_AMT") or 0) / 10000,
            "sell_wan": (row.get("BILLBOARD_SELL_AMT") or 0) / 10000,
            "reason": reason, "is_first": first_bd, "is_multi": multi_day,
        })

    # Only first boards (not multi-day)
    first_boards = [c for c in candidates if c["is_first"] and not c["is_multi"]]
    other_boards = [c for c in candidates if c not in first_boards]
    print(f"Filtered: {len(candidates)} total, {len(first_boards)} first boards")

    if not first_boards:
        print("No first-board candidates today.")
        return None

    # Step 4: Get concept blocks and sector heat
    print("Fetching concept blocks...")
    all_concepts = {}
    for c in first_boards:
        all_concepts[c["code"]] = fetch_concept_blocks(c["code"])
        time.sleep(0.4)

    concept_counts = defaultdict(int)
    for code, boards in all_concepts.items():
        for b in boards:
            concept_counts[b["name"]] += 1

    for c in first_boards:
        boards = all_concepts.get(c["code"], [])
        max_heat, top_concept = 0, ""
        for b in boards:
            h = concept_counts.get(b["name"], 0)
            if h > max_heat:
                max_heat, top_concept = h, b["name"]
        c["sector_heat"] = max_heat
        c["top_concept"] = top_concept
        c["all_concepts"] = [b["name"] for b in boards[:8]]

    # Step 5: Score
    for c in first_boards:
        score_candidate(c)
        # Add sector heat bonus
        if c["sector_heat"] >= 5:    c["_base_score"] += 10
        elif c["sector_heat"] >= 3:  c["_base_score"] += 5
        c["score"] = round(c["_base_score"], 1)

    first_boards.sort(key=lambda x: -x["score"])

    # Step 6: Generate report
    top_n = min(5, len(first_boards))
    top = first_boards[:top_n]

    hot_concepts = sorted(concept_counts.items(), key=lambda x: -x[1])[:10]
    hot_concepts = [(n, c) for n, c in hot_concepts if c >= 2]

    lines = []
    lines.append(f"# 赵老哥策略每日选股 — {target_date}")
    lines.append(f"")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  目标交易日: **{tomorrow}** ({tmrw_weekday})")
    lines.append(f"> 策略: 新题材龙头 + 近5日首板 + 高换手 + 净买>0 + 回封确认")
    lines.append(f"")
    lines.append(f"## 今日数据")
    lines.append(f"- 龙虎榜记录: **{len(today_data)}** 条")
    lines.append(f"- 首板候选: **{len(first_boards)}** 只")
    lines.append(f"- 今日主线: **{', '.join([n for n,_ in hot_concepts[:5]])}**")
    lines.append(f"")

    # Hot concepts
    if hot_concepts:
        lines.append("## 今日热点概念")
        lines.append("| 概念 | 出现次数 |")
        lines.append("|------|---------|")
        for name, cnt in hot_concepts[:8]:
            lines.append(f"| {name} | {cnt} |")
        lines.append("")

    # Top picks
    lines.append("## 明日（" + tomorrow + "）精选标的")
    lines.append("")
    for i, c in enumerate(top):
        lines.append(f"### {'🥇🥈🥉🏅🏅'[i] if i < 5 else '•'} #{i+1} {c['code']} {c['name']} — 评分 {c['score']:.1f}/40")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 今日收盘 | ¥{c['close']:.2f} |")
        lines.append(f"| 涨幅 | **{c['change_pct']:+.1f}%** |")
        lines.append(f"| 换手率 | {c['turnover_pct']:.1f}% |")
        lines.append(f"| 净买入 | **{c['net_buy_wan']:.0f}万** |")
        bs_ratio = c['buy_wan'] / c['sell_wan'] if c['sell_wan'] > 0 else float('inf')
        lines.append(f"| 买卖比 | {bs_ratio:.1f}:1 |")
        lines.append(f"| 题材热度 | {c['sector_heat']}只同概念 |")
        lines.append(f"| 所属概念 | {', '.join(c.get('all_concepts', [])[:6])} |")
        lines.append(f"| 上榜原因 | {c['reason'][:60]} |")
        lines.append("")
        lines.append("**明日操作计划：**")
        lines.append("")
        lines.append(f"| 情景 | 操作 |")
        lines.append(f"|------|------|")
        lines.append(f"| 高开 ≥ {c['close']*1.03:.2f} (+3%) | 买入20%仓位 |")
        lines.append(f"| 高开 ≥ {c['close']*1.01:.2f} (+1%) | 买入10%仓位 |")
        lines.append(f"| 平开/微高 | 5%试探仓 |")
        lines.append(f"| 低开 < {c['close']*0.99:.2f} (-1%) | 放弃 |")
        lines.append(f"| 止损线 | ¥{c['close']*0.95:.2f} (-5%) |")
        lines.append(f"| 第一目标 | ¥{c['close']*1.05:.2f} (+5%) |")
        lines.append("")

    # Full ranking table
    if len(first_boards) > top_n:
        lines.append("## 完整排名")
        lines.append("")
        lines.append("| 排名 | 代码 | 名称 | 收盘 | 涨幅 | 换手 | 净买(万) | 评分 | 热度 | 概念 |")
        lines.append("|------|------|------|------|------|------|----------|------|------|------|")
        for i, c in enumerate(first_boards[:15]):
            lines.append(
                f"| {i+1} | {c['code']} | {c['name']} | {c['close']:.2f} | "
                f"{c['change_pct']:+.1f}% | {c['turnover_pct']:.1f}% | "
                f"{c['net_buy_wan']:.0f} | {c['score']:.1f} | "
                f"{c['sector_heat']}只 | {c.get('top_concept','')} |"
            )
        lines.append("")

    # Execution rules
    lines.append("## 执行铁律")
    lines.append("")
    lines.append("```")
    lines.append("09:25 竞价结束后：")
    lines.append("  ✅ 精选标的高开 ≥1% → 按仓位计划执行")
    lines.append("  ⚠️ 任意标的低开/平开 → 直接跳过")
    lines.append("  ❌ 已成交3笔 → 停止当天所有新交易")
    lines.append("  ❌ 连续2笔止损 → 本周停止交易")
    lines.append("")
    lines.append("仓位管理：")
    lines.append("  最多同时持有：3只")
    lines.append("  单只最大仓位：20%")
    lines.append("  总仓位上限：50%")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 赵老哥首板回封策略*")
    lines.append(f"*⚠️ AI算法筛选，不构成投资建议*")

    report = "\n".join(lines)
    return {
        "text": report,
        "target_date": target_date,
        "tomorrow": tomorrow,
        "first_boards": first_boards,
        "top": top,
        "total_records": len(today_data),
        "hot_concepts": hot_concepts,
    }

# ── Push notification ────────────────────────
def push_serverchan(sendkey: str, title: str, content: str) -> bool:
    """Push via Server酱 (WeChat notification)"""
    if not sendkey:
        return False
    try:
        r = requests.post(
            SERVERCHAN_URL.format(sendkey=sendkey),
            data={"title": title, "desp": content},
            timeout=15,
        )
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

# ── Entry point ───────────────────────────────
if __name__ == "__main__":
    today = date.today().strftime("%Y-%m-%d")
    result = run_screening(today)

    if result is None:
        print("No trading data for today. Exiting.")
        sys.exit(0)

    # Save markdown report
    report_path = REPORTS_DIR / f"zhao_picks_{today}.md"
    report_path.write_text(result["text"], encoding="utf-8")
    print(f"Report saved: {report_path}")

    # Save JSON for potential push integration
    json_path = REPORTS_DIR / f"zhao_picks_{today}.json"
    json_data = {
        "date": today,
        "tomorrow": result["tomorrow"],
        "total_dragon_tiger": result["total_records"],
        "hot_concepts": [{"name": n, "count": c} for n, c in result["hot_concepts"][:5]],
        "picks": [
            {
                "rank": i+1, "code": c["code"], "name": c["name"],
                "close": c["close"], "score": c["score"],
                "net_buy_wan": c["net_buy_wan"], "sector_heat": c["sector_heat"],
                "concepts": c.get("all_concepts", [])[:5],
                "entry_plan": {
                    "strong_open": round(c["close"] * 1.03, 2),
                    "normal_open": round(c["close"] * 1.01, 2),
                    "skip_below": round(c["close"] * 0.99, 2),
                    "stop_loss": round(c["close"] * 0.95, 2),
                }
            }
            for i, c in enumerate(result["top"])
        ]
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON saved: {json_path}")

    # Push via Server酱 (if configured)
    if SERVERCHAN_SENDKEY:
        concepts = ", ".join([f"{n}({c})" for n, c in result["hot_concepts"][:3]])
        title = f"赵老哥选股 {today} | {'/'.join([t['name'] for t in result['top'][:3]])}"
        content_lines = [f"## 明日({result['tomorrow']})精选 | 主线: {concepts}", ""]
        for i, t in enumerate(result["top"][:3]):
            content_lines.append(f"### {['1','2','3'][i]}. {t['code']} {t['name']} 评分{t['score']:.0f}")
            content_lines.append(f"> 收盘{t['close']:.2f} 涨{t['change_pct']:+.1f}% 净买{t['net_buy_wan']:.0f}万 换手{t['turnover_pct']:.1f}%")
            content_lines.append(f"> 高开≥{t['close']*1.01:.2f}买入10% | 止损{t['close']*0.95:.2f}")
            content_lines.append("")
        push_serverchan(SERVERCHAN_SENDKEY, title, "\n".join(content_lines))
    else:
        print("[Push] Skipped: SERVERCHAN_SENDKEY not set. Visit https://sct.ftqq.com/ to get one.")

    print("Done.")
