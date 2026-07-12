#!/usr/bin/env python3
"""
NQP V3.3 月度池轮换 v6 — Actions 全链路多源降级

v6 修复:
  1. baostock query_stock_industry() 实际返回 [date, code, name, industry, class]
     代码错误地将 row[0](日期) 当 code 解析 → 全部被过滤
     修复: row[1] → code, row[3] → industry
  2. 缓存回退时 boards 可能是 list → .keys() 崩溃
     修复: 加载缓存后做类型兼容

四级降级（概念板块）：
  1. akshare (最快)
  2. eastmoney 直连 (HTTP, 国内源)
  3. baostock 行业分类 (TCP, 最稳)
  4. 本地缓存 (.pool_cache.json)
"""

import json
import os
import sys
import time
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

# ── 项目根 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "cloud" / ".pool_cache.json"
RESULT_FILE = PROJECT_ROOT / "cloud" / "pool_result.json"


# ══════════════════════════════════════════════════════════
# 赛道定义
# ══════════════════════════════════════════════════════════

TRACK_CONCEPTS: dict[str, list[str]] = {
    "机器人":   ["机器人", "人形机器人", "工业机器人", "伺服电机", "减速器", "机器视觉"],
    "低空经济": ["低空经济", "飞行汽车", "无人机", "通用航空", "eVTOL"],
    "AI应用":   ["人工智能", "AI", "大模型", "ChatGPT", "算力", "光模块", "CPO", "AI芯片", "数据中心"],
    "半导体":   ["半导体", "芯片", "集成电路", "光刻机", "先进封装", "EDA", "存储芯片"],
    "新能源":   ["新能源", "光伏", "风电", "储能", "固态电池", "钠离子电池", "氢能源"],
    "智能驾驶": ["智能驾驶", "自动驾驶", "车联网", "激光雷达", "毫米波雷达", "智能座舱"],
    "军工":     ["军工", "国防", "航天", "航空发动机", "导弹", "卫星互联网", "北斗"],
    "医药":     ["医药", "创新药", "CXO", "生物医药", "医疗器械", "中药", "疫苗"],
}

BAOSTOCK_INDUSTRY_MAP: dict[str, str] = {
    # 机器人
    "机械设备": "机器人", "自动化设备": "机器人", "通用设备": "机器人",
    "专用设备": "机器人", "仪器仪表": "机器人",
    # 半导体
    "电子": "半导体", "半导体": "半导体", "元件": "半导体",
    # AI应用
    "计算机": "AI应用", "通信": "AI应用", "传媒": "AI应用",
    "软件": "AI应用", "信息技术": "AI应用", "计算机应用": "AI应用",
    "通信设备": "AI应用",
    # 新能源
    "电力设备": "新能源", "新能源": "新能源", "电气设备": "新能源",
    # 智能驾驶
    "汽车": "智能驾驶", "汽车零部件": "智能驾驶",
    # 军工
    "国防军工": "军工", "航天航空": "军工", "军工电子": "军工",
    "航空装备": "军工", "航天装备": "军工", "地面兵装": "军工",
    "船舶制造": "军工",
    # 医药
    "医药生物": "医药", "医药": "医药", "化学制药": "医药",
    "生物制品": "医药", "医疗器械": "医药", "中药": "医药",
    "医疗服务": "医药",
}


MIN_MARKET_CAP = 100e8
MIN_TURNOVER = 2e8
MAX_PE_FOR_TRACK: dict[str, float] = {
    "半导体": 150, "AI应用": 150, "机器人": 120,
    "低空经济": 100, "新能源": 60, "智能驾驶": 100,
    "军工": 100, "医药": 80,
}
DEFAULT_MAX_PE = 100


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

def _retry(func, max_retries=3, wait=2.0):
    for i in range(max_retries):
        try:
            return func()
        except Exception as e:
            if i == max_retries - 1:
                raise
            time.sleep(wait * (2 ** i))


def _to_baostock_code(code: str) -> str:
    """将任意格式转为 baostock 9位代码: sh.600000 / sz.000001"""
    code = code.strip()
    if code.startswith(("sh.", "sz.", "bj.")):
        return code
    if len(code) == 6 and code.isdigit():
        if code.startswith("6"):
            return f"sh.{code}"
        elif code.startswith(("0", "2", "3")):
            return f"sz.{code}"
        elif code.startswith(("4", "8")):
            return f"bj.{code}"
    return f"sh.{code}"


def _from_baostock_code(bs_code: str) -> str:
    """sh.600000 → 600000"""
    return bs_code.replace("sh.", "").replace("sz.", "").replace("bj.", "").strip()


# ══════════════════════════════════════════════════════════
# Step 1 — 概念板块获取
# ══════════════════════════════════════════════════════════

def fetch_board_akshare() -> dict[str, list[str]]:
    import akshare as ak
    df = ak.stock_board_concept_name_em()
    boards: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        name = row.get("板块名称", "")
        if not name:
            continue
        try:
            cons = ak.stock_board_concept_cons_em(symbol=name)
            codes = [c[:6] for c in cons["代码"].tolist()]
            boards[name] = codes
        except Exception:
            continue
    return boards


def fetch_board_eastmoney() -> dict[str, list[str]]:
    import requests
    boards: dict[str, list[str]] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }
    sess = requests.Session()
    sess.headers.update(headers)
    url_list = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=5000&po=1&np=1&fltt=2&invt=2"
        "&fs=m:90+t:3&fields=f12,f14"
    )
    resp = sess.get(url_list, timeout=30)
    data = resp.json()
    if not data.get("data") or not data["data"].get("diff"):
        return boards
    items = data["data"]["diff"]
    for item in items:
        name = item.get("f14", "")
        code = item.get("f12", "")
        if not code or not name:
            continue
        try:
            c_url = (
                f"https://push2.eastmoney.com/api/qt/clist/get"
                f"?pn=1&pz=2000&po=1&np=1&fltt=2&invt=2"
                f"&fs=b:{code}+f:!50&fields=f12"
            )
            c_resp = sess.get(c_url, timeout=30)
            c_data = c_resp.json()
            cons = []
            if c_data.get("data") and c_data["data"].get("diff"):
                cons = [d.get("f12", "") for d in c_data["data"]["diff"]]
            boards[name] = cons
        except Exception:
            continue
    return boards


def fetch_board_baostock() -> dict[str, list[str]]:
    """
    baostock query_stock_industry() 实际返回格式:
      [update_date, code, code_name, industry, industry_classification]
    例: ['2026-07-06', 'sh.600000', '浦发银行', 'J66货币金融服务', '证监会行业分类']
    """
    import baostock as bs
    bs.login()
    rows = []
    try:
        rs = bs.query_stock_industry()
        while (rs.error_code == '0') & rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()

    if rows:
        print(f"  [baostock] 原始行数: {len(rows)}, 样本前3行:")
        for r in rows[:3]:
            print(f"    {r}")

    boards: dict[str, list[str]] = {}
    for row in rows:
        if len(row) < 4:
            continue
        # v6 修复: row[1]=code, row[3]=industry (不是 row[0]!)
        code = _from_baostock_code(row[1])
        if not code or len(code) != 6 or not code.isdigit():
            continue
        ind = (row[3] or row[2] or "").strip()
        if not ind:
            continue
        boards.setdefault(ind, []).append(code)

    print(f"  [baostock] 行业数: {len(boards)}, 总股票数: {sum(len(v) for v in boards.values())}")
    for ind_name in sorted(boards.keys())[:8]:
        print(f"    {ind_name}: {boards[ind_name][:3]}... ({len(boards[ind_name])}只)")

    return boards


def load_board_cache() -> dict[str, list[str]]:
    """加载本地缓存，兼容 dict 和 list 格式"""
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 兼容: 缓存可能是 {"boards": {...}} 或直接的 list → dict
    if isinstance(data, dict):
        # 如果有 boards 键，取它
        if "boards" in data:
            boards = data["boards"]
            if isinstance(boards, dict):
                return boards
            if isinstance(boards, list):
                return _convert_board_list(boards)
        # 否则当作 {行业: [代码]}
        return data
    if isinstance(data, list):
        return _convert_board_list(data)
    return {}


def _convert_board_list(data: list) -> dict[str, list[str]]:
    """list 格式 → dict 格式"""
    boards: dict[str, list[str]] = {}
    for item in data:
        if isinstance(item, dict):
            name = item.get("name") or item.get("industry") or ""
            codes = item.get("codes") or item.get("stocks") or []
            if name and codes:
                boards[name] = codes
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            boards[str(item[0])] = item[1] if isinstance(item[1], list) else [item[1]]
    return boards


def save_board_cache(boards: dict[str, list[str]]):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"boards": boards, "updated_at": datetime.now().isoformat()}, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════
# Step 2 — 赛道匹配
# ══════════════════════════════════════════════════════════

def match_tracks_concept(boards: dict[str, list[str]]) -> dict[str, set[str]]:
    tracks: dict[str, set[str]] = {t: set() for t in TRACK_CONCEPTS}
    for bname, codes in boards.items():
        for track, keywords in TRACK_CONCEPTS.items():
            if any(kw in bname for kw in keywords):
                tracks[track].update(codes)
    return tracks


def match_tracks_baostock(boards: dict[str, list[str]]) -> dict[str, set[str]]:
    tracks: dict[str, set[str]] = {t: set() for t in TRACK_CONCEPTS}
    matched_industries: dict[str, list[str]] = {t: [] for t in TRACK_CONCEPTS}
    unmatched: list[str] = []

    for ind, codes in boards.items():
        matched = False
        # 第一层: 精确匹配
        for industry_key, track in BAOSTOCK_INDUSTRY_MAP.items():
            if industry_key == ind or industry_key in ind:
                tracks[track].update(codes)
                matched_industries[track].append(ind)
                matched = True
                break
        # 第二层: 模糊匹配
        if not matched:
            for industry_key, track in BAOSTOCK_INDUSTRY_MAP.items():
                if ind in industry_key or any(kw in ind for kw in industry_key.split()):
                    tracks[track].update(codes)
                    matched_industries[track].append(ind)
                    matched = True
                    break
        if not matched:
            unmatched.append(ind)

    for t in TRACK_CONCEPTS:
        inds = matched_industries.get(t, [])
        if inds:
            print(f"  [赛道] {t}: {len(inds)} 个行业 → {len(tracks[t])} 只成分股")
        else:
            print(f"  [赛道] {t}: 0 个行业 → 0 只成分股")

    if unmatched:
        print(f"  [未匹配] {len(unmatched)} 个行业: {unmatched[:15]}...")

    return tracks


# ══════════════════════════════════════════════════════════
# Step 3 — 成分股扫描 + 基础过滤
# ══════════════════════════════════════════════════════════

def _fetch_fundamentals_eastmoney(codes: list[str]) -> dict[str, dict]:
    """东财 push2 个股 API"""
    import requests
    results: dict[str, dict] = {}
    for i in range(0, len(codes), 200):
        batch = codes[i:i+200]
        secids = ",".join(f"1.{c}" if c.startswith("6") else f"0.{c}" for c in batch)
        url = (
            f"https://push2.eastmoney.com/api/qt/ulist.np/get"
            f"?fltt=2&invt=2&fields=f2,f12,f14,f20,f6,f15"
            f"&secids={secids}"
        )
        try:
            resp = requests.get(url, timeout=30)
            data = resp.json()
            if data.get("data") and data["data"].get("diff"):
                for item in data["data"]["diff"]:
                    code = item.get("f12", "")
                    results[code] = {
                        "price": float(item.get("f2") or 0),
                        "market_cap": float(item.get("f20") or 0),
                        "turnover_rate": float(item.get("f6") or 0),
                        "amount": float(item.get("f15") or 0),
                    }
        except Exception as e:
            print(f"    [东财] 批次 {i} 失败: {e}")
    return results


def _fetch_fundamentals_baostock(codes: list[str], target_date: str) -> dict[str, dict]:
    """baostock K线取 PE/换手/成交额"""
    import baostock as bs
    bs.login()
    results: dict[str, dict] = {}
    start = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        for code in codes:
            bs_code = _to_baostock_code(code)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,close,peTTM,turn,volume",
                    start_date=start, end_date=target_date,
                    frequency="d", adjustflag="2"
                )
                rows = []
                while (rs.error_code == '0') & rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    # 取最近一条有 PE 的记录
                    for r in reversed(rows):
                        pe = float(r[2]) if r[2] and r[2] != '0' and r[2] != '' else 0
                        close = float(r[1]) if r[1] else 0
                        turn = float(r[3]) if r[3] else 0
                        vol = float(r[4]) if r[4] else 0
                        amount = vol * close * 100 if close and vol else 0  # 估计成交额
                        if close:
                            results[code] = {
                                "price": close,
                                "market_cap": 0,  # baostock 无市值
                                "turnover_rate": turn,
                                "amount": amount,
                                "pe": pe,
                            }
                            break
            except Exception:
                continue
    finally:
        bs.logout()
    return results


def basic_filter(
    track_stocks: dict[str, set[str]],
    target_date: str
) -> dict[str, set[str]]:
    """基础过滤: 市值 > 100亿, 成交额 > 2亿, PE > 0 且 < 赛道上限"""
    all_codes = sorted(set().union(*track_stocks.values()))
    if not all_codes:
        return {t: set() for t in track_stocks}

    print(f"  [基本面] 尝试东财 API ({len(all_codes)} 只)...")
    fund = _fetch_fundamentals_eastmoney(all_codes)

    if not fund:
        print(f"  [基本面] 尝试 baostock ({len(all_codes)} 只)...")
        fund = _fetch_fundamentals_baostock(all_codes, target_date)

    print(f"  [基本面] 总共获取 {len(fund)} 只")

    filtered: dict[str, set[str]] = {t: set() for t in track_stocks}
    for track in track_stocks:
        max_pe = MAX_PE_FOR_TRACK.get(track, DEFAULT_MAX_PE)
        for code in track_stocks[track]:
            info = fund.get(code)
            if not info:
                continue
            mc = info.get("market_cap", 0)
            amt = info.get("amount", 0)
            pe = info.get("pe", 0)
            # 市值: baostock 无市值时跳过该条件
            if mc and mc < MIN_MARKET_CAP:
                continue
            if amt and amt < MIN_TURNOVER:
                continue
            if pe and (pe <= 0 or pe > max_pe):
                continue
            filtered[track].add(code)

        print(f"  [扫描] {track}: {len(filtered[track])} 只通过基础过滤")

    return filtered


# ══════════════════════════════════════════════════════════
# Step 4 — MA200 技术过滤
# ══════════════════════════════════════════════════════════

def ma200_filter(
    track_stocks: dict[str, set[str]],
    target_date: str
) -> dict[str, set[str]]:
    """MA200 上方过滤: 收盘价 > MA200"""
    all_codes = sorted(set().union(*track_stocks.values()))
    if not all_codes:
        return {t: set() for t in track_stocks}

    print(f"  [MA200] 处理 {len(all_codes)} 只标的...")
    import baostock as bs
    bs.login()
    # 需要足够长的历史算MA200
    start = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    passed: set[str] = set()
    failed: list[str] = []
    try:
        for code in all_codes:
            bs_code = _to_baostock_code(code)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code, "date,close", start_date=start, end_date=target_date,
                    frequency="d", adjustflag="2"
                )
                closes = []
                while (rs.error_code == '0') & rs.next():
                    row = rs.get_row_data()
                    if row[1]:
                        closes.append(float(row[1]))
                if len(closes) >= 200:
                    ma200 = sum(closes[-200:]) / 200
                    if closes[-1] > ma200:
                        passed.add(code)
                failed.append(code)
            except Exception:
                failed.append(code)
    finally:
        bs.logout()

    print(f"  [MA200] K线获取: {len(passed)} 只成功, {len(failed)} 只失败")

    result: dict[str, set[str]] = {}
    for track, codes in track_stocks.items():
        result[track] = codes & passed
        print(f"  [MA200] {track}: {len(result[track])} 只通过（共{len(codes)}只）")

    return result


# ══════════════════════════════════════════════════════════
# Step 5-7 — 评分 + Top5 + 输出
# ══════════════════════════════════════════════════════════

def score_stocks(
    track_stocks: dict[str, set[str]],
    target_date: str
) -> dict[str, list[tuple[str, float]]]:
    """综合评分: 趋势分 + 量分"""
    all_codes = sorted(set().union(*track_stocks.values()))
    if not all_codes:
        return {}

    import baostock as bs
    bs.login()
    start = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    scores: dict[str, float] = {}
    try:
        for code in all_codes:
            bs_code = _to_baostock_code(code)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code, "date,close,volume,amount",
                    start_date=start, end_date=target_date,
                    frequency="d", adjustflag="2"
                )
                closes = []
                amounts = []
                while (rs.error_code == '0') & rs.next():
                    row = rs.get_row_data()
                    if row[1]:
                        closes.append(float(row[1]))
                    if row[3]:
                        amounts.append(float(row[3]))
                if len(closes) < 20:
                    continue
                # MA200 乖离率
                ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else sum(closes[-20:]) / 20
                closest = closes[-1]
                deviation_pct = (closest - ma200) / ma200 * 100
                # 趋势强度: 近20日收益 / 波动率
                ret_20 = (closes[-1] / closes[-20] - 1) if len(closes) >= 20 else 0
                volatility = (max(closes[-20:]) - min(closes[-20:])) / closes[-20] if len(closes) >= 20 else 1
                trend_score = ret_20 / volatility * 100 if volatility > 0 else 0
                # 量分: 近5日相对20日均量
                avg_vol_20 = sum(amounts[-20:]) / 20 if len(amounts) >= 20 else amounts[-1]
                avg_vol_5 = sum(amounts[-5:]) / 5 if len(amounts) >= 5 else amounts[-1]
                vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1
                # 综合分
                s = deviation_pct * 0.3 + trend_score * 0.4 + min(vol_ratio, 2) * 10 * 0.3
                scores[code] = round(s, 2)
            except Exception:
                continue
    finally:
        bs.logout()

    result: dict[str, list[tuple[str, float]]] = {}
    for track, codes in track_stocks.items():
        ranked = sorted(
            [(c, scores.get(c, 0)) for c in codes if c in scores],
            key=lambda x: x[1], reverse=True
        )
        result[track] = ranked
        top5_str = ", ".join(f"{c}({s:.1f})" for c, s in ranked[:5])
        print(f"  [Top5] {track}: {top5_str}" if top5_str else f"  [Top5] {track}: (无)")
    return result


def run(dry_run: bool = False, target_date: Optional[str] = None):
    if target_date is None:
        target_date = date.today().isoformat()

    print("=" * 60)
    print(f"NQP V3.3 月度池轮换 v6 — {target_date}")
    print("=" * 60)

    # ──── Step 1: 获取概念板块 ────
    print("\n[1/7] 获取概念板块（多源降级）...")
    boards: dict[str, list[str]] = {}
    source = None

    # 1. akshare
    print("  [概念] 尝试 akshare...")
    try:
        boards = _retry(fetch_board_akshare)
        source = "akshare"
        print(f"  [概念] akshare 成功, {len(boards)} 个板块")
    except Exception as e:
        print(f"  [概念] akshare 失败: {e}")

    # 2. eastmoney
    if not boards:
        print("  [概念] 尝试 eastmoney...")
        try:
            boards = _retry(fetch_board_eastmoney)
            source = "eastmoney"
            print(f"  [概念] eastmoney 成功, {len(boards)} 个板块")
        except Exception as e:
            print(f"  [概念] eastmoney 失败: {e}")

    # 3. baostock
    if not boards:
        print("  [概念] 尝试 baostock...")
        try:
            boards = _retry(fetch_board_baostock)
            source = "baostock"
        except Exception as e:
            print(f"  [概念] baostock 失败: {e}")

    # 4. 缓存
    if not boards:
        print("  [概念] 使用本地缓存...")
        boards = load_board_cache()
        source = "cache"
        if boards:
            print(f"  [概念] 缓存加载成功, 板块/行业数: {len(boards)}")

    if not boards:
        print("  [概念] ❌ 所有数据源均失败，退出")
        sys.exit(1)

    print(f"  [概念] 成功, 数据源: {source}, 板块/行业数: {len(boards)}")

    # ──── Step 2: 赛道匹配 ────
    print("\n[2/7] 赛道匹配（数据源: {})...".format(source))
    if source == "baostock" or source == "cache":
        # 检查是否是 baostock 行业格式 (dict of industry→codes)
        # v6 修复: 兼容 list 格式的 boards（缓存可能为 list）
        if isinstance(boards, dict) and boards:
            tracks = match_tracks_baostock(boards)
        else:
            print("  [赛道] boards 非 dict, 回退概念匹配")
            tracks = match_tracks_concept(boards if isinstance(boards, dict) else {})
    else:
        tracks = match_tracks_concept(boards)

    total = len(set().union(*tracks.values()))
    print(f"  [赛道] 总计: {total} 只成分股（去重）")

    # ──── Step 3: 成分股扫描 + 基础过滤 ────
    print("\n[3/7] 成分股扫描...")
    filtered = basic_filter(tracks, target_date)

    # ──── Step 4: MA200 技术过滤 ────
    print("\n[4/7] MA200 技术过滤...")
    ma200_passed = ma200_filter(filtered, target_date)

    # ──── Step 5-6: 评分 + Top5 ────
    print("\n[5/7] 赛道分配 & 评分...")
    print("\n[6/7] Top5 选择...")
    ranked = score_stocks(ma200_passed, target_date)

    # ──── Step 7: 输出 ────
    print("\n[7/7] 输出结果...")
    top5_flat: list[str] = []
    for t in TRACK_CONCEPTS:
        top5_flat.extend(c for c, _ in ranked.get(t, [])[:5])

    # 对比上次池
    old_pool: list[str] = []
    if RESULT_FILE.exists():
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
                old_pool = old.get("stocks", [])
        except Exception:
            pass

    added = [c for c in top5_flat if c not in old_pool]
    removed = [c for c in old_pool if c not in top5_flat]

    result = {
        "generated_at": datetime.now().isoformat(),
        "version": "v3.3",
        "total_stocks": len(top5_flat),
        "stocks": top5_flat,
        "changes": {
            "added": added,
            "removed": removed,
            "added_count": len(added),
            "removed_count": len(removed),
        }
    }

    print(f"  总池: {len(top5_flat)} 只")
    print(f"  新增: {len(added)} 只 {added if added else ''}")
    print(f"  移除: {len(removed)} 只 {removed if removed else ''}")
    print()

    if dry_run:
        print("🔍 [DRY RUN] 预览模式，不写入文件")
    else:
        RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("✅ 已写入 pool_result.json")
        # 同时更新缓存
        save_board_cache(boards)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NQP V3.3 月度池轮换")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD")
    args = parser.parse_args()
    run(dry_run=args.dry_run, target_date=args.date)
