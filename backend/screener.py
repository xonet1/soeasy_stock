# -*- coding: utf-8 -*-
"""量化选股推荐：多因子打分 + 约束过滤 + Top N 清单 + 历期跟踪。

方法（参考多因子选股通行做法）：
  1) 横截面因子：
     技术面（本地通达信日线）：20日动量、MA20 趋势斜率、20日波动率（低波优先）、量比
     基本面（东财 clist 全市场快照）：PE(TTM)/PB（低估值优先）、ROE、主力净额/流通市值
  2) 排名百分位标准化（抗异常值，方向统一为越大越好）
  3) 经验权重加权合成综合分（权重为经验设定，未经单因子回测优化）
  4) 实盘约束过滤：排除 ST/退市、北交所、次新股(<120交易日)、当日涨停、
     日均成交额 < 5000 万（流动性）
  5) 综合分排序取 Top N，附主要贡献因子、建议仓位（单票≤10%）、波动率止损位
  6) 每期清单落盘跟踪（picks_history.json），再次生成时回填历期至今实际涨跌

诚实边界：因子权重未做单因子回测；输出为统计参考非投资建议。
"""
import json
import math
import os
import re
import time
import threading

import numpy as np
import pandas as pd

import models_reader as mr

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
HISTORY_PATH = os.path.join(BASE_DIR, "data", "picks_history.json")

UNIVERSE_TTL = 2 * 3600          # 全市场快照缓存 2 小时
MIN_DAYS = 120                   # 次新股排除：本地日线不足 120 根
MIN_AMT20 = 5e7                  # 流动性约束：20 日均成交额 ≥ 5000 万
LOCK_UP_PCT = 9.7                # 当日涨停（主板/创业板近似）排除：买不进
LIMIT_ST_RE = re.compile(r"ST|退")

# 因子权重（方向已统一：分位越高 = 越有利于做多）
WEIGHTS = {
    "mom20": 0.25,               # 20日动量
    "trend": 0.20,               # MA20 斜率（10日）
    "lowvol": 0.15,              # 低波动优先
    "value": 0.15,               # 低估值优先（PE .075 + PB .075）
    "quality": 0.10,             # ROE
    "flow": 0.10,                # 主力净额 / 流通市值
}
FACTOR_LABELS = {
    "mom20": "20日动量", "trend": "MA20趋势", "lowvol": "低波动",
    "value": "低估值", "quality": "高ROE", "flow": "主力资金",
}

_universe_lock = threading.Lock()


# ---------------- 全市场估值快照（datacenter + 腾讯批量行情） ----------------
# push2 clist 接口在本机环境被 TLS 指纹拦截，改用两条已验证稳定通道：
#   1) 东财 datacenter RPT_DMSK_TS_STOCKNEW：全市场收盘/涨跌/换手/PE动态/主力净额
#   2) 东财 datacenter RPT_LICO_FN_CPD 按报告期过滤：全市场最新年报 ROE
#   3) 腾讯批量行情：PE(TTM)/PB/流通市值（每批 60 只）
def _fetch_universe_snapshot(force=False):
    with _universe_lock:
        cache_fp = os.path.join(CACHE_DIR, "picks_universe.json")
        if not force and os.path.exists(cache_fp) \
                and time.time() - os.path.getmtime(cache_fp) < UNIVERSE_TTL:
            try:
                with open(cache_fp, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("rows"):
                    return d["rows"], d.get("as_of")
            except Exception:
                pass

        rows = _build_universe()
        as_of = time.strftime("%Y-%m-%d %H:%M")
        with open(cache_fp, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "as_of": as_of}, f, ensure_ascii=False)
        return rows, as_of


def _dc_all_pages(report, columns, filt, sort_col="SECURITY_CODE", page_size=500, max_pages=30):
    """datacenter 报表全分页拉取。"""
    out = []
    for page in range(1, max_pages + 1):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
               f"?reportName={report}&columns={columns}"
               + (f"&filter={filt}" if filt else "")
               + f"&pageNumber={page}&pageSize={page_size}"
               f"&sortColumns={sort_col}&sortTypes=1&source=WEB&client=WEB")
        d = mr._curl_json(url) or {}
        res = d.get("result") or {}
        data = res.get("data") or []
        out.extend(data)
        pages = res.get("pages") or 0
        if not data or page >= pages:
            break
    return out


def _latest_roe_map():
    """最近年报全市场 ROE：优先通达信本地专业财务（离线），否则东财。"""
    try:
        import cw_reader
        m, rd = cw_reader.latest_annual_roe_map()
        if m:
            return m, rd
    except Exception:
        pass
    year = time.localtime().tm_year
    for rd in (f"{year}-12-31", f"{year - 1}-12-31"):
        rows = _dc_all_pages(
            "RPT_LICO_FN_CPD", "SECUCODE,SECURITY_NAME_ABBR,WEIGHTAVG_ROE",
            f"(REPORTDATE='{rd}')", sort_col="SECUCODE")
        m = {}
        for r in rows:  # 同一证券多条披露记录 → 后到覆盖（取最新）
            code = str(r.get("SECUCODE") or "")[:6]
            roe = r.get("WEIGHTAVG_ROE")
            if code:
                m[code] = roe
        if len(m) >= 2000:
            return m, rd[:10]
    return {}, None


def _tencent_quotes(codes):
    """腾讯批量行情：{code: {pe_ttm, pb, float_mv, total_mv}}。"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        q = ",".join(("sh" if c.startswith(("6", "5")) else "sz") + c for c in batch)
        try:
            txt = map_reader_curl("https://qt.gtimg.cn/q=" + q)
        except Exception:
            continue
        for seg in txt.split(";"):
            seg = seg.strip()
            if "=" not in seg:
                continue
            body = seg.split("=", 1)[1].strip('"')
            f = body.split("~")
            if len(f) < 50 or not f[2].isdigit():
                continue
            def _fl(idx):
                try:
                    return float(f[idx])
                except (ValueError, IndexError):
                    return None
            out[f[2]] = {"pe_ttm": _fl(39), "pb": _fl(46),
                         "total_mv": _fl(45), "float_mv": _fl(44)}
    return out


def map_reader_curl(url, timeout=15):
    import map_reader
    return map_reader._curl_text(url, timeout)


def _build_universe():
    base = _dc_all_pages(
        "RPT_DMSK_TS_STOCKNEW",
        "SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,TURNOVERRATE,"
        "PE_DYNAMIC,PRIME_INFLOW,ORG_PARTICIPATE", None, page_size=500)
    if not base:
        raise RuntimeError("全市场快照拉取失败")
    codes = [str(r.get("SECURITY_CODE")) for r in base]
    tmap = _tencent_quotes(codes)
    roe_map, roe_date = _latest_roe_map()

    rows = []
    for r in base:
        code = str(r.get("SECURITY_CODE") or "")
        t = tmap.get(code) or {}
        pe = t.get("pe_ttm")
        if pe is None or pe <= 0:
            pe = r.get("PE_DYNAMIC")
        rows.append({
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "price": r.get("CLOSE_PRICE"),
            "chg": r.get("CHANGE_RATE"),
            "turnover": r.get("TURNOVERRATE"),
            "pe": pe,
            "pb": t.get("pb"),
            "float_mv": (t.get("float_mv") or 0) * 1e8 if t.get("float_mv") else None,
            "total_mv": (t.get("total_mv") or 0) * 1e8 if t.get("total_mv") else None,
            "roe": roe_map.get(code),
            "main_inflow": r.get("PRIME_INFLOW"),
            "org_part": r.get("ORG_PARTICIPATE"),
        })
    return rows


# ---------------- 技术因子（本地日线） ----------------
def _tech_factors(df):
    """df: read_recent_cached(260) 结果。返回 dict，缺数据字段为 None。"""
    if df is None or df.empty or "close" not in getattr(df, "columns", []):
        return {}
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    if df.empty:
        return {}
    close = df["close"]
    n = len(df)
    out = {"days": n}
    if n < 60:
        return out
    c = float(close.iloc[-1])
    out["close"] = c
    prev = float(close.iloc[-2])
    out["chg"] = round((c / prev - 1) * 100, 2) if prev > 0 else None

    out["mom20"] = round((c / float(close.iloc[-21]) - 1) * 100, 2) if n > 21 else None
    out["mom60"] = round((c / float(close.iloc[-61]) - 1) * 100, 2) if n > 61 else None
    ret = close.pct_change()
    out["vol20"] = round(float(ret.tail(20).std()) * 100, 2) if n > 25 else None
    ma20 = close.rolling(20).mean()
    m20 = ma20.iloc[-1]
    m20_10 = ma20.iloc[-11] if n > 11 else np.nan
    out["trend"] = round((float(m20) / float(m20_10) - 1) * 100, 2) \
        if pd.notna(m20) and pd.notna(m20_10) and float(m20_10) > 0 else None
    out["bias20"] = round((c / float(m20) - 1) * 100, 2) if pd.notna(m20) and float(m20) > 0 else None

    if "amount" in df.columns:
        amt = df["amount"].astype(float)
        out["amt20"] = float(amt.tail(20).mean()) if n >= 20 else None
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(dtype=float)
    v5 = float(vol.tail(6).head(5).mean()) if len(vol) >= 6 else 0.0
    out["vr"] = round(float(vol.iloc[-1]) / v5, 2) if (v5 > 0 and len(vol) > 0) else None
    out["vol_sum20"] = float(vol.tail(20).mean()) if len(vol) >= 20 else None
    return out


def _grade(score):
    if score >= 0.65:
        return "A", "强"
    if score >= 0.55:
        return "B", "偏多"
    if score >= 0.45:
        return "C", "均衡"
    return "D", "观察"


# ---------------- 主流程 ----------------
def run_picks(reader, top_n=20, progress_cb=None):
    uni_rows, as_of = _fetch_universe_snapshot()
    uni = {r["code"]: r for r in uni_rows if r.get("code")}
    codes = [c for c in reader.all_codes() if c in uni]
    total = len(codes)

    factors, meta = [], []
    for i, code in enumerate(codes):
        u = uni[code]
        name = u.get("name") or reader.name_of(code)
        if LIMIT_ST_RE.search(name or ""):
            continue
        try:
            df = reader.read_recent_cached(code, n=260)
        except Exception:
            continue
        tf = _tech_factors(df)
        if tf.get("days", 0) < MIN_DAYS:
            continue
        # 实盘约束（文章 4.2）：流动性 / 涨停 / 停牌
        if not tf.get("amt20") or tf["amt20"] < MIN_AMT20:
            continue
        if (tf.get("chg") or 0) >= LOCK_UP_PCT:
            continue
        price = tf.get("close") or u.get("price")
        if not price or price <= 0:
            continue
        fm = float(u.get("float_mv") or 0)
        inflow = u.get("main_inflow")
        factors.append({
            "code": code, "name": name,
            "mom20": tf.get("mom20"), "trend": tf.get("trend"),
            "lowvol": -tf["vol20"] if tf.get("vol20") is not None else None,
            "pe": u.get("pe"), "pb": u.get("pb"), "quality": u.get("roe"),
            "flow": (inflow / fm * 100) if (inflow is not None and fm > 0) else None,
        })
        meta.append({"code": code, "close": price, "chg": tf.get("chg"),
                     "amt20": tf.get("amt20"), "vol20": tf.get("vol20"),
                     "mom60": tf.get("mom60"), "bias20": tf.get("bias20"),
                     "vr": tf.get("vr"), "turnover": u.get("turnover")})
        if progress_cb and (i + 1) % 200 == 0:
            progress_cb(i + 1, total)

    if len(factors) < top_n:
        return {"ok": False, "error": f"有效样本不足（{len(factors)} 只），请稍后重试"}
    fdf = pd.DataFrame(factors).set_index("code")
    mdf = {m["code"]: m for m in meta}

    # 价值因子合成：PE/PB 各自排名取均值（仅对正值有效，负值缺失按 NaN 排名）
    for col, better_low in (("pe", True), ("pb", True)):
        v = fdf[col].where(fdf[col] > 0)
        fdf["v_" + col] = (-v if better_low else v)
    fdf["value"] = fdf[["v_pe", "v_pb"]].rank(pct=True).mean(axis=1)

    # 横截面排名百分位 + 加权合成
    score = pd.Series(0.0, index=fdf.index)
    for key, w in WEIGHTS.items():
        if key == "value":
            pct = fdf["value"]
        else:
            pct = fdf[key].rank(pct=True)
        score = score + pct.fillna(0.5) * w
    score = score / sum(WEIGHTS.values())
    fdf["score"] = score.round(4)

    top = fdf.sort_values("score", ascending=False).head(top_n)

    # 各因子分位（含价值合成因子），供贡献度展示
    pcts = {}
    for key, w in WEIGHTS.items():
        pcts[key] = fdf["value"] if key == "value" else fdf[key].rank(pct=True)

    picks = []
    for code, row in top.iterrows():
        m = mdf.get(code, {})
        s = float(row["score"])
        g, gl = _grade(s)
        # 主要贡献因子：权重×分位最高者
        best_k, best_v = "mom20", -1
        for key, w in WEIGHTS.items():
            cv = pcts[key].get(code)
            if cv is not None and not math.isnan(cv) and w * cv > best_v:
                best_v, best_k = w * cv, key
        vol20 = m.get("vol20")
        stop = None
        if vol20 and m.get("close"):
            stop = round(m["close"] * (1 - min(0.25, max(0.05, 2.5 * vol20 / 100))), 2)
        pos = round(min(10.0, max(2.0, (s - 0.35) * 22)), 1)   # 单票 ≤10%
        picks.append({
            "code": code, "name": row["name"],
            "close": m.get("close"), "chg": m.get("chg"),
            "score": round(s * 100, 1), "grade": g, "grade_label": gl,
            "mom20": row.get("mom20"), "mom60": m.get("mom60"),
            "trend": row.get("trend"), "vol20": vol20,
            "pe": row.get("pe"), "pb": row.get("pb"), "roe": row.get("quality"),
            "turnover": m.get("turnover"), "vr": m.get("vr"),
            "amt20_yi": round(m["amt20"] / 1e8, 2) if m.get("amt20") else None,
            "top_factor": FACTOR_LABELS.get(best_k, best_k),
            "pos_suggest": f"{pos}%", "stop_loss": stop,
        })

    # 历史跟踪：落盘本期，回填历期至今实际涨跌
    trade_date = _latest_trade_date(reader)
    entry = {"date": trade_date, "as_of": as_of, "top_n": top_n,
             "picks": [{"code": p["code"], "name": p["name"],
                        "close": p["close"], "score": p["score"]} for p in picks]}
    history = _load_history()
    history = [h for h in history if h.get("date") != trade_date]
    history.append(entry)
    history = history[-24:]
    _update_history_perf(history, reader)
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

    return {
        "ok": True,
        "as_of": as_of,
        "trade_date": trade_date,
        "universe": len(uni),
        "valid": len(factors),
        "weights": WEIGHTS,
        "factor_labels": FACTOR_LABELS,
        "picks": picks,
    }


def _latest_trade_date(reader):
    try:
        idx = reader.build_index()
        for code in ("000001", "600000", "600519"):
            fp = idx.get(code)
            if fp:
                df = reader.read_day(code, recent=1)
                if not df.empty:
                    return df["date"].iloc[-1].strftime("%Y-%m-%d")
    except Exception:
        pass
    return time.strftime("%Y-%m-%d")


def _load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _update_history_perf(history, reader):
    """回填历期 picks 从生成日至最近交易日的实际涨跌幅（%）。"""
    idx = reader.build_index()
    for h in history:
        entry_date = h.get("date")
        if not entry_date:
            continue
        for p in h.get("picks", []):
            if p.get("ret") is not None and p.get("entry_close"):
                continue
            fp = idx.get(str(p["code"]).zfill(6))
            if not fp:
                continue
            try:
                df = reader.read_day(str(p["code"]).zfill(6))
            except Exception:
                continue
            if df.empty:
                continue
            base_df = df[df["date"] <= entry_date]
            if base_df.empty:
                continue
            entry_close = p.get("close") or float(base_df["close"].iloc[-1])
            p["entry_close"] = round(float(entry_close), 2)
            last = float(df["close"].iloc[-1])
            p["last_close"] = round(last, 2)
            p["ret"] = round((last / entry_close - 1) * 100, 2)
        rets = [p["ret"] for p in h.get("picks", []) if p.get("ret") is not None]
        h["avg_ret"] = round(sum(rets) / len(rets), 2) if rets else None


def picks_history(reader):
    history = _load_history()
    _update_history_perf(history, reader)
    return {"ok": True, "history": history[::-1][:12]}
