#!/usr/bin/env python3
"""
# DEPRECATED — 已被 pool_rotator.py (v6) 替代
# 原因: v1 使用腾讯财经+akshare，赛道只有6个；v6 改用东财push2+baostock四级降级，8赛道
# 保留仅供历史参考，所有 workflow 均使用 pool_rotator.py
#
NQP V3.3 月度股票池自动轮换引擎
===================================
每月第一个交易日运行，自动扫描A股市场，按新质生产力赛道
筛选符合条件的标的，生成新的交易池。

筛选流程：
  1. 从同花顺概念板块获取赛道候选股
  2. 基础过滤：市值>100亿 + 日均成交>2亿 + PE>0
  3. 技术过滤：收盘价 > MA200
  4. 综合评分：趋势强度 + 动量 + 流动性
  5. 每赛道取Top5 → 输出新池 + 变动报告

用法：
  python cloud/pool_rotator.py                    # 自动模式（输出json）
  python cloud/pool_rotator.py --dry-run          # 预览模式（仅打印，不写文件）
  python cloud/pool_rotator.py --date 2026-07-01  # 指定日期
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

# ============================================================
# 赛道-概念关键词映射
# ============================================================
SECTOR_CONCEPT_MAP: Dict[str, List[str]] = {
    "半导体": [
        "半导体", "芯片", "集成电路", "光刻机", "光刻胶",
        "存储芯片", "先进封装", "第三代半导体", "EDA",
        "半导体设备", "半导体材料", "晶圆", "IGBT"
    ],
    "AI算力": [
        "AI", "人工智能", "算力", "CPO", "光模块",
        "服务器", "数据中心", "液冷", "算力租赁",
        "AI芯片", "GPU", "HBM", "高速连接", "云计算"
    ],
    "机器人": [
        "机器人", "人形机器人", "工业母机", "减速器",
        "伺服电机", "传感器", "机器视觉", "自动化",
        "智能制造", "空心杯电机", "丝杠", "执行器"
    ],
    "新能源": [
        "新能源", "储能", "光伏", "锂电池", "固态电池",
        "钠电池", "充电桩", "逆变器", "风电", "氢能",
        "智能电网", "特高压", "光伏逆变器", "海上风电"
    ],
    "低空经济": [
        "低空经济", "无人机", "飞行汽车", "eVTOL",
        "通用航空", "航天", "卫星导航", "北斗",
        "商业航天", "航空发动机"
    ],
    "量子计算": [
        "量子", "量子计算", "量子通信", "量子加密",
        "量子传感"
    ],
}


def fetch_hot_concepts() -> pd.DataFrame:
    """
    获取同花顺概念板块热度数据。
    使用东财概念板块接口（免费、稳定）。
    """
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_em()
        print(f"  [概念] 获取到 {len(df)} 个概念板块")
        return df
    except Exception as e:
        print(f"  [概念] akshare 失败: {e}，尝试直连...")
        # Fallback: 东财直连
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "200",
            "po": "1", "np": "1",
            "fs": "m:90+t:3",
            "fields": "f2,f3,f4,f12,f14",
            "fid": "f3",
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("data") and data["data"].get("diff"):
            rows = []
            for item in data["data"]["diff"]:
                rows.append({
                    "代码": item.get("f12", ""),
                    "板块名称": item.get("f14", ""),
                    "涨跌幅": item.get("f3", 0),
                })
            df = pd.DataFrame(rows)
            print(f"  [概念] 东财直连接口获取到 {len(df)} 个概念")
            return df
        raise RuntimeError("概念数据获取失败")


def match_sectors(concepts_df: pd.DataFrame) -> Dict[str, List[str]]:
    """将概念板块匹配到NQP赛道。返回 {赛道名: [概念代码列表]}"""
    sector_concepts: Dict[str, List[str]] = {sector: [] for sector in SECTOR_CONCEPT_MAP}
    name_col = "板块名称" if "板块名称" in concepts_df.columns else "f14"
    code_col = "代码" if "代码" in concepts_df.columns else "f12"

    for _, row in concepts_df.iterrows():
        concept_name = str(row.get(name_col, ""))
        concept_code = str(row.get(code_col, ""))
        for sector, keywords in SECTOR_CONCEPT_MAP.items():
            for kw in keywords:
                if kw in concept_name:
                    sector_concepts[sector].append(concept_code)
                    break

    for sector in sector_concepts:
        sector_concepts[sector] = list(set(sector_concepts[sector]))

    return sector_concepts


def fetch_concept_stocks(concept_codes: List[str]) -> pd.DataFrame:
    """获取概念板块成分股。使用东财接口批量获取。"""
    all_stocks = set()
    for i, code in enumerate(concept_codes):
        try:
            import akshare as ak
            df = ak.stock_board_concept_cons_em(symbol=code)
            stocks = df["代码"].tolist()
            all_stocks.update(stocks)
            if (i + 1) % 5 == 0:
                print(f"    已处理 {i+1}/{len(concept_codes)} 个概念板块...")
            time.sleep(0.3)
        except Exception as e:
            print(f"    [WARN] 概念 {code} 获取失败: {e}")
            continue

    print(f"    共获取 {len(all_stocks)} 只候选股")
    return pd.DataFrame({"代码": sorted(all_stocks)})


def fetch_spot_quotes(codes: List[str], batch_size: int = 80) -> pd.DataFrame:
    """批量获取A股实时行情（腾讯财经接口）。"""
    all_rows = []
    total = len(codes)

    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        code_str = ",".join([f"sh{c}" if c.startswith("6") else f"sz{c}" for c in batch])
        url = f"https://qt.gtimg.cn/q={code_str}"

        try:
            resp = requests.get(url, timeout=15)
            resp.encoding = "gbk"
            text = resp.text
        except Exception as e:
            print(f"    [WARN] 行情请求失败: {e}")
            continue

        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or '="' not in line:
                continue
            try:
                data_str = line.split('="')[1].rstrip('";')
                fields = data_str.split("~")
                if len(fields) < 50:
                    continue

                code_raw = fields[2]
                code = code_raw[2:] if len(code_raw) > 2 else code_raw
                name = fields[1]
                price = float(fields[3]) if fields[3] else 0.0
                change_pct = float(fields[32]) if fields[32] else 0.0
                pe = float(fields[39]) if fields[39] else 0.0
                market_cap = float(fields[45]) if fields[45] else 0.0  # 总市值（亿）
                volume = float(fields[6]) if fields[6] else 0.0  # 成交量（手）
                turnover = float(fields[37]) if fields[37] else 0.0  # 成交额（万）

                all_rows.append({
                    "代码": code,
                    "名称": name,
                    "现价": price,
                    "涨跌幅": change_pct,
                    "PE": pe,
                    "总市值_亿": market_cap,
                    "成交量_手": volume,
                    "成交额_万": turnover,
                })
            except (IndexError, ValueError):
                continue

        if (i + batch_size) % 200 == 0:
            print(f"    已获取 {min(i + batch_size, total)}/{total} 行情...")
        time.sleep(0.15)

    df = pd.DataFrame(all_rows)
    print(f"    成功获取 {len(df)} 只股票行情")
    return df


def apply_basic_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    基础过滤：市值>100亿 + 成交额>2亿/日 + PE>0 + 现价>3
    """
    initial = len(df)
    df = df[
        (df["总市值_亿"] > 100) &
        (df["成交额_万"] > 20000) &
        (df["PE"] > 0) &
        (df["现价"] > 3)
    ].copy()
    dropped = initial - len(df)
    print(f"    基础过滤: {initial} → {len(df)} (剔除 {dropped})")
    return df


def fetch_kline_batch(codes: List[str], lookback: int = 250) -> Dict[str, pd.DataFrame]:
    """批量获取日K线数据（腾讯财经接口）。"""
    klines = {}
    total = len(codes)

    for i, code in enumerate(codes):
        try:
            prefix = "sh" if code.startswith("6") else "sz"
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {
                "param": f"{prefix}{code},day,,,{lookback}",
                "_var": "kline_day"
            }
            resp = requests.get(url, params=params, timeout=10)
            text = resp.text

            json_str = text[text.find("{"):text.rfind("}") + 1]
            data = json.loads(json_str)

            kline_data = data.get("data", {}).get(f"{prefix}{code}", {}).get("day", [])
            if not kline_data:
                kline_data = data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", [])

            if kline_data and len(kline_data) >= 200:
                rows = []
                for item in kline_data:
                    rows.append({
                        "date": item[0],
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]),
                    })
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
                klines[code] = df

        except Exception:
            continue

        if (i + 1) % 50 == 0:
            print(f"    K线获取 {i+1}/{total}，已成功 {len(klines)} 只...")
        time.sleep(0.1)

    print(f"    K线获取完成: {len(klines)}/{total} 只有效K线数据")
    return klines


def compute_ma200_score(
    klines: Dict[str, pd.DataFrame], quotes_df: pd.DataFrame
) -> pd.DataFrame:
    """计算MA200乖离 + 趋势评分，只保留 close > MA200 的标的。"""
    results = []
    for code, kdf in klines.items():
        try:
            if len(kdf) < 200:
                continue

            kdf["MA200"] = kdf["close"].rolling(200).mean()
            kdf["MA20"] = kdf["close"].rolling(20).mean()

            latest_close = kdf["close"].iloc[-1]
            ma200 = kdf["MA200"].iloc[-1]
            ma20 = kdf["MA20"].iloc[-1]

            if pd.isna(ma200) or ma200 <= 0:
                continue

            deviation = (latest_close / ma200 - 1) * 100

            # 过滤：必须在MA200上方（允许-3%以内）
            if deviation <= -3:
                continue

            # MA20斜率（最近10日）
            idx10 = min(11, len(kdf) - 1)
            ma20_10d_ago = kdf["MA20"].iloc[-idx10] if len(kdf) >= idx10 + 1 else kdf["MA20"].iloc[0]
            ma20_slope = (ma20 / ma20_10d_ago - 1) * 100 if pd.notna(ma20_10d_ago) and ma20_10d_ago > 0 else 0

            # 年化收益
            returns = kdf["close"].pct_change().dropna()
            ann_return = returns.mean() * 252 * 100 if len(returns) > 0 else 0

            # 波动率
            volatility = returns.std() * np.sqrt(252) * 100 if len(returns) > 0 else 100

            # 最大回撤
            cummax = kdf["close"].expanding().max()
            drawdown = (kdf["close"] / cummax - 1) * 100
            max_dd = drawdown.min()

            # 综合趋势评分 (0-100)
            dev_score = max(0, min(25, 25 - abs(deviation - 10) * 2))
            slope_score = max(0, min(25, ma20_slope * 5 if ma20_slope > 0 else 0))
            ret_score = max(0, min(25, ann_return / 2))
            dd_score = max(0, min(25, (20 + max_dd) * 1.25)) if max_dd > -20 else 25

            trend_score = dev_score + slope_score + ret_score + dd_score

            # 成交量评分
            quote_row = quotes_df[quotes_df["代码"] == code]
            turnover = quote_row["成交额_万"].values[0] if len(quote_row) > 0 else 0
            vol_score = min(25, turnover / 20000 * 5)

            total_score = trend_score * 0.70 + vol_score * 0.30

            results.append({
                "代码": code,
                "名称": quote_row["名称"].values[0] if len(quote_row) > 0 else code,
                "现价": latest_close,
                "PE": quote_row["PE"].values[0] if len(quote_row) > 0 else 0,
                "总市值_亿": quote_row["总市值_亿"].values[0] if len(quote_row) > 0 else 0,
                "MA200乖离": round(deviation, 2),
                "MA20斜率": round(ma20_slope, 2),
                "年化收益": round(ann_return, 2),
                "最大回撤": round(max_dd, 2),
                "趋势分": round(trend_score, 1),
                "成交量分": round(vol_score, 1),
                "综合评分": round(total_score, 1),
            })
        except Exception:
            continue

    df = pd.DataFrame(results)
    if len(df) > 0:
        df = df.sort_values("综合评分", ascending=False).reset_index(drop=True)
    print(f"    MA200过滤后: {len(df)} 只标的")
    return df


def assign_sectors(scored_df: pd.DataFrame) -> pd.DataFrame:
    """
    将标的分配到NQP赛道。
    基于个股所属概念板块自动识别，优先匹配最具体的赛道。
    """
    sector_col = []

    for idx, row in scored_df.iterrows():
        code = row["代码"]
        assigned = "其他"
        try:
            # 通过东财概念查询
            url = "https://push2.eastmoney.com/api/qt/slist/get"
            params = {
                "spt": "1", "fltt": "2",
                "invt": "2",
                "fields": "f3,f12,f14",
                "fs": f"m:90+t:3+f:!50,code:{code}",
                "pn": "1", "pz": "10",
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            concepts_list = []
            if data.get("data") and data["data"].get("diff"):
                for item in data["data"]["diff"]:
                    concepts_list.append(item.get("f14", ""))

            matched_sectors = set()
            for concept in concepts_list:
                for sector, keywords in SECTOR_CONCEPT_MAP.items():
                    for kw in keywords:
                        if kw in concept:
                            matched_sectors.add(sector)
                            break
                    if sector in matched_sectors:
                        break

            if matched_sectors:
                # 选匹配关键词最多的赛道
                best_sector = max(
                    matched_sectors,
                    key=lambda s: sum(
                        1 for kw in SECTOR_CONCEPT_MAP[s] if any(kw in c for c in concepts_list)
                    )
                )
                assigned = best_sector
        except Exception:
            pass
        sector_col.append(assigned)

    scored_df["赛道"] = sector_col
    return scored_df


def select_top_per_sector(scored_df: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """每赛道取Top N，去重后返回最终池。"""
    selected = []
    for sector in SECTOR_CONCEPT_MAP:
        sector_df = scored_df[scored_df["赛道"] == sector]
        if len(sector_df) == 0:
            continue
        top = sector_df.nlargest(top_n, "综合评分")
        selected.append(top)

    if not selected:
        return pd.DataFrame()

    result = pd.concat(selected, ignore_index=True)
    result = result.drop_duplicates(subset="代码")
    sector_order = {s: i for i, s in enumerate(SECTOR_CONCEPT_MAP)}
    result["赛道序"] = result["赛道"].map(sector_order)
    result = result.sort_values(["赛道序", "综合评分"], ascending=[True, False])
    result = result.drop(columns=["赛道序"])
    return result


def load_existing_pool() -> Dict[str, Dict]:
    """加载当前池（从 pool_result.json）。"""
    pool_path = Path(__file__).parent / "pool_result.json"
    if pool_path.exists():
        with open(pool_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {s["代码"]: s for s in data.get("pool", [])}
    return {}


def compute_diff(new_pool: pd.DataFrame, old_pool: Dict[str, Dict]) -> Dict:
    """计算新旧池差异。"""
    new_codes = set(new_pool["代码"].tolist())
    old_codes = set(old_pool.keys())

    added = new_codes - old_codes
    removed = old_codes - new_codes
    kept = new_codes & old_codes

    added_stocks = []
    for code in added:
        row = new_pool[new_pool["代码"] == code].iloc[0]
        added_stocks.append({
            "代码": code,
            "名称": row["名称"],
            "赛道": row["赛道"],
            "综合评分": row["综合评分"],
        })

    removed_stocks = []
    for code in removed:
        old = old_pool[code]
        removed_stocks.append({
            "代码": code,
            "名称": old.get("名称", code),
            "赛道": old.get("赛道", "未知"),
        })

    return {
        "new_total": len(new_pool),
        "old_total": len(old_pool),
        "added": added_stocks,
        "removed": removed_stocks,
        "kept": len(kept),
    }


def format_pool_for_pipeline(pool_df: pd.DataFrame) -> str:
    """将池格式化为 daily_pipeline.py 可用的 Python 代码片段。"""
    lines = ["# NQP_POOL: 自动生成于 " + datetime.now().strftime("%Y-%m-%d %H:%M")]

    for sector in SECTOR_CONCEPT_MAP:
        sector_stocks = pool_df[pool_df["赛道"] == sector]
        if len(sector_stocks) == 0:
            continue
        lines.append(f"    # {sector} ({len(sector_stocks)}只)")
        for _, row in sector_stocks.iterrows():
            lines.append(f'    ("{row["代码"]}", "{row["名称"]}", "{sector}"),')

    return "\n".join(lines)


def print_summary(final_pool: pd.DataFrame, diff: Dict, dry_run: bool):
    """打印轮换摘要。"""
    print(f"\n{'='*60}")
    print(f"🏆 最终交易池 ({len(final_pool)} 只)")
    print(f"{'='*60}")

    for sector in SECTOR_CONCEPT_MAP:
        sector_stocks = final_pool[final_pool["赛道"] == sector]
        if len(sector_stocks) == 0:
            continue
        print(f"\n## {sector} ({len(sector_stocks)}只)")
        for _, row in sector_stocks.iterrows():
            print(f"  {row['代码']} {row['名称']:8s}  "
                  f"评分{row['综合评分']:5.1f}  "
                  f"乖离{row['MA200乖离']:+6.1f}%  "
                  f"PE{row['PE']:5.0f}")

    if diff.get("old_total", 0) > 0:
        print(f"\n{'='*60}")
        print(f"📊 池变动 ({diff['old_total']} → {diff['new_total']})")
        print(f"{'='*60}")
        if diff["added"]:
            print(f"\n🟢 纳入 ({len(diff['added'])}只):")
            for s in diff["added"]:
                print(f"  + {s['代码']} {s['名称']:8s} [{s['赛道']}] 评分{s['综合评分']:.1f}")
        if diff["removed"]:
            print(f"\n🔴 移出 ({len(diff['removed'])}只):")
            for s in diff["removed"]:
                print(f"  - {s['代码']} {s['名称']:8s} [{s['赛道']}]")
        if not diff["added"] and not diff["removed"]:
            print("\n  ✅ 池无变化")
    else:
        print(f"\n  (首次运行，无旧池对比)")


def main():
    parser = argparse.ArgumentParser(description="NQP V3.3 月度池轮换")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写文件")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD")
    args = parser.parse_args()

    today = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"NQP V3.3 月度池轮换 — {today}")
    print(f"{'='*60}\n")

    # Step 1: 获取概念板块
    print("[1/7] 获取概念板块...")
    try:
        concepts_df = fetch_hot_concepts()
    except Exception as e:
        print(f"  [FATAL] 概念获取失败: {e}")
        print("  池轮换中止，保持现有池不变。")
        return 1

    # Step 2: 匹配NQP赛道
    print("\n[2/7] 匹配NQP赛道...")
    sector_concepts = match_sectors(concepts_df)
    for sector, codes in sector_concepts.items():
        print(f"  {sector}: {len(codes)} 个概念板块")

    all_concept_codes = []
    for codes in sector_concepts.values():
        all_concept_codes.extend(codes)
    all_concept_codes = list(set(all_concept_codes))
    if not all_concept_codes:
        print("  [FATAL] 未匹配到任何概念板块！")
        return 1

    # Step 3: 获取成分股（限制前30个概念，避免API超时）
    print(f"\n[3/7] 获取成分股（前30个概念板块）...")
    stocks_df = fetch_concept_stocks(all_concept_codes[:30])
    candidate_codes = stocks_df["代码"].tolist()
    print(f"  候选标的: {len(candidate_codes)} 只")

    if len(candidate_codes) == 0:
        print("  [FATAL] 未获取到任何候选股！")
        return 1

    # Step 4: 获取行情 + 基础过滤
    print(f"\n[4/7] 获取行情 + 基础过滤...")
    quotes_df = fetch_spot_quotes(candidate_codes)
    quotes_df = apply_basic_filter(quotes_df)
    filtered_codes = quotes_df["代码"].tolist()

    if len(filtered_codes) == 0:
        print("  [FATAL] 基础过滤后无标的！")
        return 1

    # Step 5: K线 + MA200过滤
    print(f"\n[5/7] K线 + MA200过滤 ({len(filtered_codes)} 只)...")
    klines = fetch_kline_batch(filtered_codes)
    scored_df = compute_ma200_score(klines, quotes_df)

    if len(scored_df) == 0:
        print("\n[WARN] 无标的通过MA200过滤！使用现有池。")
        return 1

    # Step 6: 赛道分配
    print("\n[6/7] 分配赛道...")
    scored_df = assign_sectors(scored_df)

    # Step 7: 选择最终池
    print("\n[7/7] 选择最终池...")
    final_pool = select_top_per_sector(scored_df, top_n=5)

    if len(final_pool) == 0:
        print("  [FATAL] 无标的可纳入最终池！")
        return 1

    # 差异对比
    old_pool = load_existing_pool()
    diff = compute_diff(final_pool, old_pool)

    # 输出摘要
    print_summary(final_pool, diff, args.dry_run)

    # 保存
    if not args.dry_run:
        output_path = Path(__file__).parent / "pool_result.json"
        pool_data = []
        for _, row in final_pool.iterrows():
            pool_data.append({
                "代码": row["代码"],
                "名称": row["名称"],
                "赛道": row["赛道"],
                "现价": row["现价"],
                "PE": row["PE"],
                "总市值_亿": row["总市值_亿"],
                "MA200乖离": row["MA200乖离"],
                "综合评分": row["综合评分"],
                "趋势分": row["趋势分"],
            })

        result = {
            "generated_at": today,
            "total": len(final_pool),
            "pool": pool_data,
            "diff": diff,
            "pool_code": format_pool_for_pipeline(final_pool),
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 池已保存到: {output_path}")
    else:
        print(f"\n🔍 [预览模式] 未写入文件")

    return 0


if __name__ == "__main__":
    sys.exit(main())
