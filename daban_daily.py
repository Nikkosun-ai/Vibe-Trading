#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打板策略盘中推送 (GitHub Actions 14:30 北京时间启动, 尾盘14:45-15:00买入窗口)

用法: python daban_daily.py [日期YYYY-MM-DD, 默认今天]
推送: ServerChan 微信 (SERVERCHAN_SENDKEY 环境变量)
落盘: daily_reports/daban_signal_{date}.json (daily_reports/ 已 gitignore)
"""
import sys, io, os, json
from datetime import date, datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from daban_core import fetch_zt_pool, screen_candidates, is_trading_day

REPORTS_DIR = Path(__file__).parent / "daily_reports"
REPORTS_DIR.mkdir(exist_ok=True)
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_SENDKEY", "")
SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"


def push_wechat(title: str, content: str) -> bool:
    if not SERVERCHAN_KEY:
        print("[Push] Skipped: SERVERCHAN_SENDKEY not set")
        return False
    try:
        import requests
        r = requests.post(SERVERCHAN_URL.format(SERVERCHAN_KEY),
                           data={"title": title, "desp": content}, timeout=15)
        d = r.json()
        ok = d.get("code") == 0
        print(f"[Push] {'OK' if ok else 'FAIL: ' + str(d.get('message', '?'))}: {title}")
        return ok
    except Exception as e:
        print(f"[Push] Error: {e}")
        return False


def build_push(res: dict, today: str) -> tuple:
    """返回 (title, content)"""
    n = res["day_zt"]
    tier = res["tier"]
    pos = res["pos"]
    cands = res["cands"]

    if n == 0:
        return (f"打板 {today} | 无涨停数据",
                f"## 🎯 打板信号 {today}\n\n今日涨停池为空（非交易日或数据源异常），跳过。\n")
    if not cands:
        return (f"打板 {today} | 涨停{n}家 {tier} 空仓",
                f"## 🎯 打板信号 {today}\n\n**涨停 {n} 家 → {tier} → 空仓观望**\n\n"
                f"> {res['note']}\n\n明日条件不变：涨停 ≥80 家才出手。\n")

    lines = [f"## 🎯 打板信号 {today}",
             "",
             f"**涨停 {n} 家 → {tier} → 单票 {pos*100:.0f}% 仓**（最多打 4 只）",
             f"> {res['note']}",
             "",
             "| # | 代码 | 名称 | 涨停价 | 量比 | 题材(联动) | 流通市值 | 盘中开板 |",
             "|---|------|------|--------|------|-----------|----------|----------|"]
    for i, c in enumerate(cands, 1):
        ltsz = f"{c['ltsz_yi']:.0f}亿" if c["ltsz_yi"] else "—"
        lines.append(f"| {i} | {c['code']} | {c['name']} | {c['price']:.2f} | "
                     f"{c['vol_ratio']:.2f} | {c['tag1']}({c['tag1_cnt']}家) | {ltsz} | "
                     f"{'✅' if c['open_depth'] >= 3 else '⚠️浅'} |")
    lines += ["",
              "⏰ **执行铁律**：",
              f"1. 收到推送后立即以**涨停价 {cands[0]['price']:.2f} 元对应价**挂单排队（各票以表中涨停价）",
              "2. 只买盘中曾开板的（开板深度≥3%）；秒板排不进就放弃",
              "3. **14:50 二次确认**：仍封死涨停 + 量比仍<1.5 才留单；炸板/放量撤单",
              "4. 次日开盘**无条件卖出**（集合竞价或开盘30秒内）",
              "5. 情绪峰值150+家时执行半仓（今日提示为准）",
              "",
              f"*生成 {datetime.now().strftime('%H:%M')} | 数据: 同花顺+新浪 | 不构成投资建议*"]
    title = f"打板 {today} | 涨停{n}家{tier} | {len(cands)}只候选"
    return title, "\n".join(lines)


def main():
    today = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")
    print(f"打板盘中筛选: {today} {datetime.now().strftime('%H:%M:%S')}")

    if not is_trading_day(today):
        print("非交易日(或当日bar未生成) → 跳过推送")
        return

    rows = fetch_zt_pool(today)
    print(f"涨停池原始记录: {len(rows)} 条")
    if not rows:
        print("无数据 → 非交易日或API缺日, 跳过推送")
        return

    res = screen_candidates(rows, today)
    print(f"涨停 {res['day_zt']} 家 | {res['tier']} | 单票{res['pos']*100:.0f}% | 候选 {len(res['cands'])} 只")
    for c in res["cands"]:
        print(f"  {c['code']} {c['name']:<8} 价{c['price']:.2f} 量比{c['vol_ratio']} "
              f"题材{c['tag1']}({c['tag1_cnt']}) 市值{c['ltsz_yi']}亿 开板{c['open_depth']}%")

    # 落盘
    out = {"date": today, "generated": datetime.now().isoformat(timespec="seconds"),
           "day_zt": res["day_zt"], "tier": res["tier"], "pos": res["pos"],
           "note": res["note"], "cands": res["cands"]}
    p = REPORTS_DIR / f"daban_signal_{today}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"落盘: {p}")

    # 推送
    title, content = build_push(res, today)
    push_wechat(title, content)
    print("Done.")


if __name__ == "__main__":
    main()
