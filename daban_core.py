#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打板策略共享核心: 涨停池拉取 + K线 + 筛选逻辑 (盘中推送/收盘复盘共用)

策略口径 (三时段回测验证, 胜率69-75%):
  情绪分档: 涨停>=100家 单票25% | 80-99家 12.5% | <80家 空仓 | 150+家 降半仓
  选股:     首板 非一字 非ST × 量比<1.5 × (>=100家时加: 题材>=3家 × 流通市值<80亿)
数据源: 同花顺涨停池(免费零鉴权, 当日盘中实时更新) + 新浪日K + 腾讯实时行情
盘中口径: 新浪日K盘中不含当日bar → 实时字段走腾讯, 新浪仅取历史(首板+20日均量)
"""
import json, time, urllib.request
from collections import Counter

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"}
KLINE_DATALEN = 60      # 覆盖20日均量 + 昨日首板判定
MAX_PICKS = 4           # 每日最多打4只


def limit_threshold(code: str, name: str) -> float:
    if "ST" in str(name).upper():
        return 4.8
    if code.startswith(("30", "68")):
        return 19.8
    if code.startswith(("8", "4")):
        return 29.8
    return 9.8


def _prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("8", "4")):
        return "bj"
    return "sz"


def fetch_zt_pool(date_str: str, retries: int = 3) -> list:
    """同花顺涨停池 (当日数据盘中实时更新; 非交易日/API缺日返回空)"""
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/")
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            d = r.json()
            if d.get("errocode", 0) != 0:
                return []
            return d.get("data") or []
        except Exception:
            time.sleep(1.5)
    return []


def is_trading_day(target_date: str) -> bool:
    """判断 target_date 是否为交易日(且盘中/收盘后数据可用)

    实测: 新浪指数/个股日K在盘中不含当日bar; 同花顺涨停池对非交易日返回
    最近交易日的陈旧数据; 腾讯行情非交易时段返回最后交易日数据。
    三级证据(任一命中即True, 全部否定即False):
      1. 涨停池记录自带 date 字段: 盘中/收盘后实时返回当日, 节假日返回陈旧日期
      2. 上证指数K线含当日bar (收盘后)
      3. 腾讯上证指数行情时间戳(字段30)日期==当日 (盘中兜底; 非交易日为陈旧日期)
    任一接口网络异常时跳过该级, 由其余证据裁决。
    """
    # 1) 涨停池 date 字段 (最快最直接)
    try:
        rows = fetch_zt_pool(target_date)
        if rows:
            if str(rows[0].get("date", ""))[:10] == target_date:
                return True
            # 陈旧数据 → 疑似非交易日, 继续让下面两级翻案(涨停池缓存异常时)
    except Exception:
        pass
    # 2) 上证指数K线含当日bar (收盘后; 盘中无)
    try:
        url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
               f"CN_MarketDataService.getKLineData?symbol=sh000001"
               f"&scale=240&ma=no&datalen=10")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://finance.sina.com.cn/")
        with urllib.request.urlopen(req, timeout=12) as resp:
            txt = resp.read().decode("utf-8")
        d = json.loads(txt[txt.index("(") + 1: txt.rindex(")")])
        if d and target_date in {r["day"] for r in d}:
            return True
    except Exception:
        pass
    # 3) 腾讯上证指数行情时间戳 (盘中实时; 非交易日为最后交易日时间戳)
    try:
        q = fetch_tencent_quotes(["sh000001"])
        ts = str(q.get("sh000001", {}).get("ts", ""))
        if ts[:8] == target_date.replace("-", ""):
            return True
    except Exception:
        pass
    return False


def fetch_kline(code: str, datalen: int = KLINE_DATALEN) -> dict:
    """新浪日K: {day: {open,close,high,low,volume}}, 当日bar盘中实时更新"""
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
           f"CN_MarketDataService.getKLineData?symbol={_prefix(code)}{code}"
           f"&scale=240&ma=no&datalen={datalen}")
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Referer", "https://finance.sina.com.cn/")
    with urllib.request.urlopen(req, timeout=12) as resp:
        txt = resp.read().decode("utf-8")
    d = json.loads(txt[txt.index("(") + 1: txt.rindex(")")])
    if not d:
        return {}
    out = {}
    for r in d:
        out[r["day"]] = {
            "open": float(r["open"]), "close": float(r["close"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "volume": float(r["volume"]),
        }
    return out


def fetch_tencent_quotes(symbols: list) -> dict:
    """腾讯实时行情批量接口 (盘中实时可用, 不封IP): {symbol: {...}}

    入参可为 6 位代码(自动加市场前缀)或已带前缀的符号(如 sh000001)。
    返回字段: name, price, prev_close, pct, high, low, volume_hands(手),
    turnover, ltsz_yi(流通市值亿), limit_up(涨停价)
    """
    out = {}
    for i in range(0, len(symbols), 50):
        batch_map = {}          # 带前缀符号 -> 原始入参符号
        for s in symbols[i:i + 50]:
            if s[:2] in ("sh", "sz", "bj"):
                batch_map[s] = s
            elif s.startswith(("6", "9")):
                batch_map["sh" + s] = s
            elif s.startswith(("8", "4")):
                batch_map["bj" + s] = s
            else:
                batch_map["sz" + s] = s
        url = "https://qt.gtimg.cn/q=" + ",".join(batch_map)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode("gbk")
        except Exception:
            continue
        for line in data.strip().split(";"):
            if "=" not in line or '"' not in line:
                continue
            prefixed = line.split("=")[0].strip().replace("v_", "", 1)
            if prefixed not in batch_map:
                continue
            vals = line.split('"')[1].split("~")
            if len(vals) < 53:
                continue
            out[batch_map[prefixed]] = {
                "name": vals[1],
                "price": float(vals[3]) if vals[3] else 0.0,
                "prev_close": float(vals[4]) if vals[4] else 0.0,
                "pct": float(vals[32]) if vals[32] else 0.0,
                "high": float(vals[33]) if vals[33] else 0.0,
                "low": float(vals[34]) if vals[34] else 0.0,
                "volume_hands": float(vals[36]) if vals[36] else 0.0,  # 手
                "turnover": float(vals[38]) if vals[38] else 0.0,
                "ltsz_yi": float(vals[45]) if vals[45] else None,      # 流通市值(亿)
                "limit_up": float(vals[47]) if vals[47] else 0.0,
                "ts": vals[30],                                        # yyyyMMddHHmmss 行情时间戳
            }
    return out


def screen_candidates_intraday(rows: list, target_date: str, sleep: float = 0.1) -> dict:
    """盘中口径筛选 (14:32 盘中推送用)

    盘中数据实测: 新浪日K不含当日bar; 同花顺涨停池盘中仅有
    {id,name,code,reason,date,market} 精简字段。故盘中实时字段全部走腾讯行情,
    新浪K线仅取历史部分(首板判定 + 20日均量)。返回结构与 screen_candidates 相同。
    """
    # ── 1. 腾讯批量实时行情 (全部涨停池股票) ──
    codes = [str(x.get("code", "")).zfill(6) for x in rows if str(x.get("code", "")).zfill(6)]
    quotes = fetch_tencent_quotes(codes)
    if not quotes:
        return {"day_zt": 0, "tier": "无数据", "pos": 0.0, "note": "腾讯行情不可用", "cands": []}

    # ── 2. 池内清洗: 涨跌幅确认封死涨停 ──
    pool = []
    for x in rows:
        code = str(x.get("code", "")).zfill(6)
        name = str(x.get("name", ""))
        q = quotes.get(code)
        if q is None or q["price"] <= 0:
            continue
        th = limit_threshold(code, name)
        if q["pct"] < th - 0.15:
            continue
        reason = str(x.get("reason", "") or "")
        pool.append({
            "code": code, "name": name, "zf": round(q["pct"], 2),
            "price": q["limit_up"], "prev_close": q["prev_close"],
            "high": q["high"], "low": q["low"],
            "volume_gu": q["volume_hands"] * 100,
            "ltsz_yi": q["ltsz_yi"], "huanshou": q["turnover"],
            "tag1": reason.split("+")[0].strip() if reason else "",
        })
    day_zt = len(pool)
    if not pool:
        return {"day_zt": 0, "tier": "无数据", "pos": 0.0, "note": "无封死涨停股票", "cands": []}

    # 题材联动计数 (池内同题材涨停家数)
    tag_cnt = Counter(p["tag1"] for p in pool if p["tag1"])
    for p in pool:
        p["tag1_cnt"] = tag_cnt.get(p["tag1"], 0)

    # ── 3. 逐股新浪历史K线: 首板/20日均量/一字板 ──
    cands = []
    for p in pool:
        try:
            bars = fetch_kline(p["code"])
        except Exception:
            bars = {}
        if not bars:
            continue
        dates = sorted(bars)
        if len(dates) < 21:
            continue                    # K线不足20日均量
        th = limit_threshold(p["code"], p["name"])
        y_pct = (bars[dates[-1]]["close"] / bars[dates[-2]]["close"] - 1) * 100
        if y_pct >= th - 0.5:
            continue                    # 昨日涨停 → 非首板
        if p["high"] == p["low"] and p["high"] == p["price"]:
            continue                    # 一字/秒板(盘中未开过板)排不进
        vol_mean = sum(bars[dates[i]]["volume"] for i in range(-20, 0)) / 20
        vol_ratio = p["volume_gu"] / vol_mean if vol_mean > 0 else 9.9
        if vol_ratio >= 1.5:
            continue                    # 缩量过滤
        open_depth = (p["price"] - p["low"]) / p["prev_close"] * 100
        cands.append({
            "code": p["code"], "name": p["name"],
            "price": p["price"],              # 涨停价(挂单价)
            "vol_ratio": round(vol_ratio, 2),
            "tag1": p["tag1"], "tag1_cnt": p["tag1_cnt"],
            "ltsz_yi": p["ltsz_yi"], "zf": p["zf"],
            "open_depth": round(open_depth, 2),
            "huanshou": p["huanshou"],
        })
        time.sleep(sleep)

    # ── 4. 情绪分档 + 选股 (同收盘口径) ──
    strict = [c for c in cands if c["tag1_cnt"] >= 3 and c["ltsz_yi"] is not None and c["ltsz_yi"] < 80]
    if day_zt >= 150:
        tier, pos, picks = "峰值150+", 0.125, strict
        note = "情绪绝对峰值: 降半仓观望(回测: 150+家胜率骤降至60.4%)"
    elif day_zt >= 100:
        tier, pos, picks = "高潮100-149", 0.25, strict
        note = "情绪高潮: 严格执行 题材>=3 × 市值<80亿"
    elif day_zt >= 80:
        tier, pos, picks = "温和80-99", 0.125, cands
        note = "情绪温和: 仅缩量首板, 不强制题材"
    else:
        tier, pos, picks = "冷清<80", 0.0, []
        note = "情绪不足: 空仓观望(回测: <80家日首板胜率60.4%但大面率2.5%偏高, 保守空仓)"

    # 排序: 题材联动降序 → 市值升序 → 量比升序 (同回测口径)
    picks = sorted(picks, key=lambda c: (-c["tag1_cnt"], c["ltsz_yi"] or 999, c["vol_ratio"]))
    return {"day_zt": day_zt, "tier": tier, "pos": pos, "note": note,
            "cands": picks[:MAX_PICKS]}


def screen_candidates(rows: list, target_date: str, sleep: float = 0.12) -> dict:
    """涨停池原始记录 -> 打板候选清单

    返回: {day_zt, tier, pos, note, cands:[{code,name,price,vol_ratio,tag1,
           tag1_cnt,ltsz_yi,zf,open_depth,huanshou}]}
    """
    # ── 1. 池内清洗: 剔除未封死涨停(接近阈值以下) ──
    pool = []
    for x in rows:
        code = str(x.get("code", "")).zfill(6)
        name = str(x.get("name", ""))
        th = limit_threshold(code, name)
        zf = float(x.get("zhangfu", 0) or 0)
        if zf < th - 0.15:
            continue
        huanshou = float(x.get("huanshou", 0) or 0)
        cje = float(x.get("chengjiaoe", 0) or 0)
        ltsz = round(cje / huanshou / 100, 1) if huanshou > 0 else None
        reason = str(x.get("reason", "") or "")
        pool.append({
            "code": code, "name": name, "zf": round(zf, 2),
            "huanshou": huanshou, "ltsz_yi": ltsz,
            "tag1": reason.split("+")[0].strip() if reason else "",
        })
    day_zt = len(pool)
    if not pool:
        return {"day_zt": 0, "tier": "无数据", "pos": 0.0, "note": "涨停池为空(非交易日或API缺日)", "cands": []}

    # 题材联动计数 (池内同题材涨停家数)
    tag_cnt = Counter(p["tag1"] for p in pool if p["tag1"])
    for p in pool:
        p["tag1_cnt"] = tag_cnt.get(p["tag1"], 0)

    # ── 2. 逐股K线: 首板/非一字/量比/开板深度 ──
    cands = []
    for p in pool:
        try:
            bars = fetch_kline(p["code"])
        except Exception:
            bars = {}
        if not bars:
            continue
        dates = sorted(bars)
        if target_date not in bars:
            continue                    # 今日bar未生成 → 跳过
        idx = dates.index(target_date)
        if idx < 21:
            continue                    # K线不足20日均量
        today = bars[dates[idx]]
        prev_close = bars[dates[idx - 1]]["close"]
        th = limit_threshold(p["code"], p["name"])
        y_pct = (prev_close / bars[dates[idx - 2]]["close"] - 1) * 100
        if y_pct >= th - 0.5:
            continue                    # 昨日涨停 → 非首板
        yizi = today["high"] == today["low"]
        if yizi:
            continue                    # 一字板排不进
        vol_mean = sum(bars[dates[i]]["volume"] for i in range(idx - 20, idx)) / 20
        vol_ratio = today["volume"] / vol_mean if vol_mean > 0 else 9.9
        if vol_ratio >= 1.5:
            continue                    # 缩量过滤
        open_depth = (today["close"] - today["low"]) / prev_close * 100
        cands.append({
            "code": p["code"], "name": p["name"],
            "price": today["close"],          # 涨停价
            "vol_ratio": round(vol_ratio, 2),
            "tag1": p["tag1"], "tag1_cnt": p["tag1_cnt"],
            "ltsz_yi": p["ltsz_yi"], "zf": p["zf"],
            "open_depth": round(open_depth, 2),
            "huanshou": p["huanshou"],
        })
        time.sleep(sleep)

    # ── 3. 情绪分档 + 选股 ──
    strict = [c for c in cands if c["tag1_cnt"] >= 3 and c["ltsz_yi"] is not None and c["ltsz_yi"] < 80]
    if day_zt >= 150:
        tier, pos, picks = "峰值150+", 0.125, strict
        note = "情绪绝对峰值: 降半仓观望(回测: 150+家胜率骤降至60.4%)"
    elif day_zt >= 100:
        tier, pos, picks = "高潮100-149", 0.25, strict
        note = "情绪高潮: 严格执行 题材>=3 × 市值<80亿"
    elif day_zt >= 80:
        tier, pos, picks = "温和80-99", 0.125, cands
        note = "情绪温和: 仅缩量首板, 不强制题材"
    else:
        tier, pos, picks = "冷清<80", 0.0, []
        note = "情绪不足: 空仓观望(回测: <80家日首板胜率60.4%但大面率2.5%偏高, 保守空仓)"

    # 排序: 题材联动降序 → 市值升序 → 量比升序 (同回测口径)
    picks = sorted(picks, key=lambda c: (-c["tag1_cnt"], c["ltsz_yi"] or 999, c["vol_ratio"]))
    return {"day_zt": day_zt, "tier": tier, "pos": pos, "note": note,
            "cands": picks[:MAX_PICKS]}
