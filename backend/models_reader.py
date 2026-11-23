# -*- coding: utf-8 -*-
"""
models_reader.py — 多模型购买参考（经典模型库，仅供个人研究，非投资建议）

模型与数据来源：
  【基本面】东财 datacenter 财务报表（业绩 RPT_LICO_FN_CPD / 资产负债 RPT_DMSK_FN_BALANCE /
            利润 RPT_DMSK_FN_INCOME / 现金流 RPT_DMSK_FN_CASHFLOW）+ 腾讯行情（市值/PE）：
    1. Piotroski F-Score（9 项财务健康检查；流动比率/股本变动 2 项公开接口不可得，按可得 7 项归一）
    2. 财务风险评分（Altman Z-Score 思想简化版：EBIT/总资产、市值/负债、销售/总资产、负债率）
    3. 格雷厄姆数字（Graham Number：√(22.5×EPS×每股净资产) 与现价比较得安全边际）
    4. PEG（彼得·林奇：PE(TTM) ÷ 净利润同比增速）
    5. 基本面质量五维（ROE/毛利率/负债率/现金流覆盖/净利增速）
  【技术】本地通达信日线（离线秒算）：
    6. 海龟法则（唐奇安 20 日突破入场 / 10 日反向离场，ATR 衡量波动）
    7. 格兰维尔八大法则（价格与 MA20 的八条经典买卖判定，逐条触发记录）
    8. 动量因子（学术经典 12-1 月动量：近一年涨幅剔除最近一个月）
    9. 波动率评分（近半年年化波动率在自身历史中的分位，低波更优）
  【综合】加权合成 0-100 分 + 五档购买参考结论（仅描述性参考）。

缓存：财务数据 12 小时；技术模型每次现算（<0.3s）。ETF/基金返回不适用提示。
"""
import os
import re
import json
import math
import time
import threading

import map_reader

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
TTL_FIN = 12 * 3600
_LOCK = threading.RLock()


def _curl_json(url, timeout=15):
    txt = map_reader._curl_text(url, timeout)
    return json.loads(txt)


def _dc_url(report, columns, secucode, pagesize=4, sort_col="REPORT_DATE"):
    return (f"https://datacenter-web.eastmoney.com/api/data/v1/get"
            f"?reportName={report}&columns={columns}"
            f"&filter=(SECUCODE%3D%22{secucode}%22)&pageSize={pagesize}&pageNumber=1"
            f"&sortColumns={sort_col}&sortTypes=-1&source=HSF10&client=PC")


def _dc_rows(report, columns, secucode, pagesize=4, sort_col="REPORT_DATE"):
    try:
        j = _curl_json(_dc_url(report, columns, secucode, pagesize, sort_col))
        return (j.get("result") or {}).get("data") or []
    except Exception:
        return []


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _secucode(code):
    code = str(code).zfill(6)
    if code.startswith(("6",)):
        return code + ".SH"
    if code.startswith(("0", "3")):
        return code + ".SZ"
    if code.startswith(("4", "8", "9")):
        return code + ".BJ"
    return None


def fetch_quote_one(code, retries=3):
    """单股腾讯行情（独立短缓存，避免覆盖地图模块的全量行情缓存）。"""
    with _LOCK:
        path = os.path.join(CACHE_DIR, f"quote_{code}.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 120:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        prefix = map_reader._code_prefix(code)
        for _ in range(retries):
            try:
                txt = map_reader._curl_text(f"https://qt.gtimg.cn/q={prefix}", 12, "gbk")
                for line in txt.split(";"):
                    if line.strip().startswith("v_"):
                        q = map_reader._parse_quote_line(line.strip())
                        if q:
                            tmp = path + ".tmp"
                            with open(tmp, "w", encoding="utf-8") as f:
                                json.dump(q, f)
                            os.replace(tmp, path)
                            return q
            except Exception:
                continue
        return None


# ---------- 基本面数据抓取（单股，缓存 12h） ----------
def fetch_fundamentals(code, refresh=False):
    with _LOCK:
        path = os.path.join(CACHE_DIR, f"fin_{code}.json")
        if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < TTL_FIN:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        secu = _secucode(code)
        if not secu:
            return {"ok": False, "error": "非沪深京证券代码"}
        mains = _dc_rows("RPT_LICO_FN_CPD",
                         "REPORTDATE,BASIC_EPS,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,"
                         "WEIGHTAVG_ROE,YSTZ,SJLTZ,BPS,MGJYXJJE,XSMLL", secu, 10, "REPORTDATE")
        balances = _dc_rows("RPT_DMSK_FN_BALANCE",
                            "REPORT_DATE,TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_EQUITY,DEBT_ASSET_RATIO", secu, 6)
        incomes = _dc_rows("RPT_DMSK_FN_INCOME",
                           "REPORT_DATE,TOTAL_OPERATE_INCOME,TOTAL_PROFIT,FINANCE_EXPENSE,PARENT_NETPROFIT", secu, 6)
        cashflows = _dc_rows("RPT_DMSK_FN_CASHFLOW",
                             "REPORT_DATE,NETCASH_OPERATE", secu, 6)
        if not mains:
            return {"ok": False, "error": "财务数据不可得（可能为新上市/无报表）"}
        data = {
            "ok": True,
            "main": [{k: _f(v) if k != "REPORTDATE" else str(v)[:10] for k, v in r.items()} for r in mains],
            "balance": [{k: _f(v) if k != "REPORT_DATE" else str(v)[:10] for k, v in r.items()} for r in balances],
            "income": [{k: _f(v) if k != "REPORT_DATE" else str(v)[:10] for k, v in r.items()} for r in incomes],
            "cashflow": [{k: _f(v) if k != "REPORT_DATE" else str(v)[:10] for k, v in r.items()} for r in cashflows],
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass
        return data


def _annual(rows, date_key):
    """取最近两期年报；不足则退回最近两期并标注口径。"""
    ann = [r for r in rows if str(r.get(date_key, "")).endswith("12-31")]
    if len(ann) >= 2:
        return ann[0], ann[1], True
    if len(rows) >= 2:
        return rows[0], rows[1], False
    return (rows[0] if rows else None), None, False


# ---------- 模型 1：Piotroski F-Score ----------
def calc_fscore(fin):
    """9 项检查，2 项不可得；返回 {score, of, items, note}。"""
    items, checks = [], []
    main = fin["main"]
    bal = fin["balance"]
    inc = fin["income"]
    cf = fin["cashflow"]
    m_now, m_prev, annual = _annual(main, "REPORTDATE")
    b_now, b_prev, _ = _annual(bal, "REPORT_DATE")
    i_now, i_prev, _ = _annual(inc, "REPORT_DATE")
    c_now, c_prev, _ = _annual(cf, "REPORT_DATE")
    if not m_now:
        return None
    ta_n = b_now.get("TOTAL_ASSETS") if b_now else None
    ta_p = b_prev.get("TOTAL_ASSETS") if b_prev else None
    np_n = m_now.get("PARENT_NETPROFIT")
    np_p = m_prev.get("PARENT_NETPROFIT") if m_prev else None
    cfo_n = c_now.get("NETCASH_OPERATE") if c_now else None
    lev_n = b_now.get("DEBT_ASSET_RATIO") if b_now else None
    lev_p = b_prev.get("DEBT_ASSET_RATIO") if b_prev else None
    gm_n = m_now.get("XSMLL")
    gm_p = m_prev.get("XSMLL") if m_prev else None
    rev_n = m_now.get("TOTAL_OPERATE_INCOME")
    rev_p = m_prev.get("TOTAL_OPERATE_INCOME") if m_prev else None

    def add(name, val, ok, note=""):
        items.append({"name": name, "val": val, "pass": ok, "note": note})
        if ok is not None:
            checks.append(1 if ok else 0)

    add("盈利性 ROA", _r(np_n / ta_n * 100, 1) if (np_n and ta_n) else None,
        bool(np_n > 0 and ta_n) if (np_n is not None and ta_n) else None, "净利润>0 且总资产为正（单位：%）")
    add("经营现金流为正", _r(cfo_n / 1e8, 1) if cfo_n is not None else None,
        (cfo_n > 0) if cfo_n is not None else None, "单位：亿元")
    add("盈利改善（净利同比）", m_now.get("SJLTZ"), (m_now.get("SJLTZ") or 0) > 0 if m_now.get("SJLTZ") is not None else None,
        "以净利同比增速近似 ΔROA 方向")
    add("应计质量（现金流≥净利）", None,
        bool(cfo_n is not None and np_n and cfo_n > np_n), "经营现金流覆盖净利润")
    add("杠杆下降", (_r(lev_n, 1), _r(lev_p, 1)) if lev_n is not None else None,
        (lev_p is not None and lev_n is not None and lev_n < lev_p), "资产负债率较上期下降")
    add("流动比率提升", None, None, "公开接口无流动资产明细，不可得")
    add("无股本稀释", None, None, "公开接口无股本变动明细，不可得")
    add("毛利率提升", (_r(gm_n, 1), _r(gm_p, 1)) if gm_n is not None else None,
        (gm_p is not None and gm_n is not None and gm_n > gm_p), "单位：%")
    turn_n = (rev_n / ta_n) if (rev_n and ta_n) else None
    turn_p = (rev_p / ta_p) if (rev_p and ta_p) else None
    add("资产周转提升", (_r(turn_n, 3), _r(turn_p, 3)) if turn_n else None,
        (turn_p is not None and turn_n is not None and turn_n > turn_p), "营收/总资产")
    score = sum(checks)
    return {"score": score, "of": len(checks), "total_def": 9, "items": items,
            "annual": annual,
            "note": f"9 项中 {len(checks)} 项可判定（2 项数据不可得），得 {score}/{len(checks)}；"
                    f"{'年报口径' if annual else '最近报告期口径（无年报）'}"}


# ---------- 模型 2：财务风险评分（Z-Score 思想简化版） ----------
def calc_zrisk(fin):
    bal = fin["balance"]
    inc = fin["income"]
    m = fin["main"]
    b = bal[0] if bal else None
    i = inc[0] if inc else None
    if not (b and i):
        return None
    ta = b.get("TOTAL_ASSETS")
    tl = b.get("TOTAL_LIABILITIES")
    ebit = (i.get("TOTAL_PROFIT") or 0) + (i.get("FINANCE_EXPENSE") or 0)
    rev = i.get("TOTAL_OPERATE_INCOME")
    lev = b.get("DEBT_ASSET_RATIO")
    mv = None  # 由调用方行情补充
    if not ta:
        return None
    x3 = ebit / ta if ta else None          # EBIT/总资产
    x5 = rev / ta if ta else None           # 销售/总资产
    parts = []
    x3_s = 2 if (x3 or 0) > 0.06 else (1 if (x3 or 0) > 0.02 else 0)
    parts.append({"name": "EBIT/总资产", "val": _r(x3 * 100, 2), "unit": "%", "score": x3_s,
                  "note": "盈利能力对资产风险的覆盖（经典 Z=X3）"})
    parts.append({"name": "资产负债率", "val": _r(lev, 1), "unit": "%",
                  "score": 2 if (lev or 100) < 40 else (1 if (lev or 100) < 60 else 0),
                  "note": "杠杆水平"})
    if x5 is not None:
        parts.append({"name": "销售/总资产", "val": _r(x5, 2), "unit": "倍",
                      "score": 2 if x5 > 1 else (1 if x5 > 0.4 else 0),
                      "note": "资产创造收入效率（经典 Z=X5）"})
    raw = {"x3": _r(x3, 4), "x5": _r(x5, 4), "lev": _r(lev, 1)}
    return {"parts": parts, "max": len(parts) * 2, "raw": raw, "mv": None, "tl": _r(tl / 1e8, 1) if tl else None}


# ---------- 模型 3：格雷厄姆数字 ----------
def calc_graham(fin, quote):
    m = fin["main"]
    m_now, _, _ = _annual(m, "REPORTDATE")
    if not m_now:
        return None
    eps = m_now.get("BASIC_EPS")
    bps = m_now.get("BPS")
    price = (quote or {}).get("close")
    if not (eps and bps and price):
        return None
    gn = math.sqrt(22.5 * eps * bps)
    margin = gn / price - 1
    return {"eps": eps, "bps": _r(bps, 2), "price": price, "graham": _r(gn, 2),
            "margin": _r(margin * 100, 1),
            "note": "格雷厄姆防御型估值：√(22.5×EPS×每股净资产)，现价低于该值视为估值偏安全"}


# ---------- 模型 4：PEG ----------
def calc_peg(fin, quote):
    m = fin["main"]
    m_now, _, _ = _annual(m, "REPORTDATE")
    if not m_now:
        return None
    pe = (quote or {}).get("pe")
    g = m_now.get("SJLTZ")  # 净利同比 %
    if pe is None or g is None:
        return None
    if g <= 0:
        return {"pe": pe, "growth": g, "peg": None, "reason": "净利增速为负，PEG 不适用（模型失效边界）",
                "note": "PEG=PE(TTM)÷净利增速，<1 通常被认为增速未被高估；增速≤0 时 PEG 无意义，单独标注"}
    return {"pe": pe, "growth": g, "peg": _r(pe / g, 2), "reason": None,
            "note": "PEG=PE(TTM)÷净利增速，<1 通常被认为增速未被高估；增速≤0 时 PEG 无意义，单独标注"}


# ---------- 模型 5：基本面质量五维 ----------
def calc_quality(fin, quote):
    m = fin["main"]
    bal = fin["balance"]
    cf = fin["cashflow"]
    m_now, _, _ = _annual(m, "REPORTDATE")
    b = bal[0] if bal else None
    c = cf[0] if cf else None
    if not m_now:
        return None
    roe = m_now.get("WEIGHTAVG_ROE")
    gm = m_now.get("XSMLL")
    lev = b.get("DEBT_ASSET_RATIO") if b else None
    np_ = m_now.get("PARENT_NETPROFIT")
    cfo = c.get("NETCASH_OPERATE") if c else None
    g = m_now.get("SJLTZ")
    parts = []
    roe_s = 30 if (roe or 0) >= 15 else (24 if (roe or 0) >= 10 else (18 if (roe or 0) >= 7 else (12 if (roe or 0) >= 3 else (6 if (roe or 0) > 0 else 0))))
    parts.append({"name": "ROE(加权)", "val": _r(roe, 1), "unit": "%", "score": roe_s, "max": 30})
    gm_s = 25 if (gm or 0) >= 40 else (20 if (gm or 0) >= 25 else (15 if (gm or 0) >= 15 else (8 if (gm or 0) >= 5 else 3)))
    parts.append({"name": "毛利率", "val": _r(gm, 1), "unit": "%", "score": gm_s, "max": 25})
    lev_s = 20 if (lev or 100) < 30 else (16 if (lev or 100) < 50 else (10 if (lev or 100) < 65 else 4))
    parts.append({"name": "负债率", "val": _r(lev, 1), "unit": "%", "score": lev_s, "max": 20})
    cover = (cfo / np_) if (cfo is not None and np_ and np_ > 0) else None
    cov_s = 15 if (cover or 0) >= 1 else (10 if (cover or 0) >= 0.5 else (5 if (cover or 0) > 0 else 0))
    parts.append({"name": "现金流/净利", "val": _r(cover, 2), "unit": "倍", "score": cov_s, "max": 15})
    g_s = 10 if (g or -1) >= 30 else (8 if (g or -1) >= 10 else (5 if (g or -1) >= 0 else 0))
    parts.append({"name": "净利增速", "val": _r(g, 1), "unit": "%", "score": g_s, "max": 10})
    score = sum(p["score"] for p in parts)
    return {"score": score, "max": 100, "parts": parts}


# ---------- 模型 6：海龟法则（唐奇安通道） ----------
def calc_turtle(d):
    if d is None or len(d) < 30:
        return None
    cur = d.iloc[-1]
    close = float(cur["close"])
    hi20 = float(d["high"].iloc[-21:-1].max())
    lo10 = float(d["low"].iloc[-11:-1].min())
    atr = cur.get("atr14")
    atr_v = float(atr) if atr is not None and atr == atr else None
    dist_top = (close / hi20 - 1) * 100
    dist_bot = (close / lo10 - 1) * 100
    if dist_top >= 0:
        state, score = "处于20日突破区（通道上沿）", 85
        act = "海龟体系入场条件满足：收盘创 20 日新高；按规则以 2N(ATR) 距离设置止损，仓位按单位风险控制"
    elif dist_bot <= 0:
        state, score = "跌破10日低点（离场通道）", 15
        act = "海龟体系离场条件满足：收盘跌破 10 日低点，按规则应退出头寸，不接飞刀"
    else:
        # 通道内：越接近上沿越高
        pos = dist_bot / max(dist_top - dist_bot, 0.01)
        pos = min(max(pos, 0), 1)
        state, score = ("通道中上部" if pos > 0.5 else "通道中下部"), int(30 + pos * 40)
        act = "处于唐奇安通道内部，等待向上突破 20 日高（%.2f）或跌破 10 日低（%.2f）的方向确认" % (hi20, lo10)
    return {"state": state, "score": score, "hi20": _r(hi20, 2), "lo10": _r(lo10, 2),
            "atr": _r(atr_v, 2), "action": act}


# ---------- 模型 7：格兰维尔八大法则 ----------
def calc_granville(d):
    if d is None or len(d) < 30:
        return None
    close = float(d["close"].iloc[-1])
    ma = d["ma20"]
    ma_n = float(ma.iloc[-1])
    ma_5ago = float(ma.iloc[-6])
    slope = (ma_n / ma_5ago - 1) * 100 if ma_5ago else 0
    dist = (close / ma_n - 1) * 100
    prev_c = float(d["close"].iloc[-2])
    lows5 = d["low"].iloc[-5:]
    touched = (lows5 < ma.iloc[-5:] * 0.995).any() or (d["close"].iloc[-5:] < ma.iloc[-5:]).any()
    fired = []
    if slope > 0 and prev_c <= ma.iloc[-2] and close > ma_n:
        fired.append(("买入①", "均线上行+价格上穿 MA20（黄金交叉式买入）"))
    if slope > 0 and touched and close > ma_n:
        fired.append(("买入②", "均线上行+回踩不破 MA20 后收复（回调买入）"))
    if slope > 0 and close > ma_n and dist > 0 and touched is False and dist < 3:
        fired.append(("买入③", "均线上行+价格沿线上方小幅回落后再涨（持币待涨区）"))
    if slope < 0 and dist < -12:
        fired.append(("买入④", "均线下行+价格深度超跌远离 MA20（抢反弹，仅激进者）"))
    if slope > 0 and dist > 15:
        fired.append(("卖出⑤", "均线上行+价格大幅乖离 MA20（短期回调风险，防追高）"))
    if slope < 0 and prev_c > ma.iloc[-2] and close < ma_n:
        fired.append(("卖出⑥", "均线下行+价格反抽 MA20 失败（下降趋势确认）"))
    if slope < -0.2 and close < ma_n:
        fired.append(("卖出⑦", "均线走平转下行+价格位于 MA20 下方（趋势转弱）"))
    if slope < 0 and -3 < dist < 2:
        fired.append(("卖出⑧", "均线下行+价格反弹至 MA20 附近受阻（逢高减仓区）"))
    net = sum(1 if f[0].startswith("买") else -1 for f in fired)
    score = min(max(50 + net * 25, 5), 95)
    return {"slope": _r(slope, 2), "dist": _r(dist, 1), "fired": fired, "score": score,
            "note": "以 MA20 为参考均线；无触发时为中性"}


# ---------- 模型 8：动量因子（12-1月） ----------
def calc_momentum(d):
    if d is None or len(d) < 60:
        return None
    close = d["close"]
    cur = float(close.iloc[-1])
    if len(d) >= 250:
        base = float(close.iloc[-250])
        recent = float(close.iloc[-21])
        mom = cur / recent - 1 + recent / base - 1  # 近一年剔除最近一月
        n = 250
    else:
        base = float(close.iloc[0])
        mom = cur / base - 1
        n = len(d)
    pct = mom * 100
    score = 90 if pct > 30 else (75 if pct > 15 else (60 if pct > 0 else (40 if pct > -15 else 25)))
    return {"mom": _r(pct, 1), "window": n, "score": score,
            "note": "12-1 月动量：近一年涨幅剔除最近一个月（避免短期反转效应）；样本不足一年时为区间涨幅"}


# ---------- 模型 9：波动率评分 ----------
def calc_volatility(d):
    if d is None or len(d) < 60:
        return None
    import numpy as np
    ret = d["close"].pct_change().dropna()
    vol_now = float(ret.iloc[-120:].std() * math.sqrt(243) * 100) if len(ret) >= 120 else float(ret.std() * math.sqrt(243) * 100)
    hist = ret.iloc[:-120] if len(ret) > 240 else ret
    vol_hist = float(hist.std() * math.sqrt(243) * 100) if len(hist) >= 60 else None
    # 相对自身历史的位置
    score = 80 if vol_now < 20 else (65 if vol_now < 30 else (50 if vol_now < 45 else (35 if vol_now < 60 else 20)))
    return {"vol": _r(vol_now, 1), "vol_hist": _r(vol_hist, 1) if vol_hist else None, "score": score,
            "note": "近 120 交易日年化波动率（%）；低波动通常回撤风险更小"}


# ---------- 技术模型汇总（本地日线） ----------
def calc_technical(code):
    from tdx_reader import TdxData
    from indicators import enrich
    r = TdxData()
    df = r.read_recent_cached(code, n=320)
    if df is None or df.empty:
        return None
    d = enrich(df)
    return {
        "turtle": calc_turtle(d),
        "granville": calc_granville(d),
        "momentum": calc_momentum(d),
        "volatility": calc_volatility(d),
    }


# ---------- 综合合成 ----------
def _blend(parts):
    """parts: [(score0_100, weight), ...]"""
    tot_w = sum(w for _, w in parts if _ is not None)
    if not tot_w:
        return None
    return sum(s * w for s, w in parts if s is not None) / tot_w


def build_verdict(total):
    if total is None:
        return ""
    if total >= 70:
        return "多方共振·重点关注"
    if total >= 55:
        return "参考买入区·建议分批"
    if total >= 40:
        return "中性·继续观察"
    if total >= 25:
        return "偏谨慎·等待信号"
    return "回避·防守为主"


def analyze(code, refresh=False):
    code = str(code).zfill(6)
    is_fund = code.startswith(("1", "5")) and not code.startswith("588")
    if is_fund:
        return {"ok": False, "is_fund": True,
                "message": "ETF/基金无个股财务模型；基本面模型仅适用于 A 股个股，技术模型已包含在个股页其他区块。"}
    fin = fetch_fundamentals(code, refresh)
    if not fin.get("ok"):
        return {"ok": False, "error": fin.get("error", "财务数据不可得")}
    quote = fetch_quote_one(code)
    fscore = calc_fscore(fin)
    zrisk = calc_zrisk(fin)
    if zrisk and quote:
        tl = zrisk.get("tl")
        mv = quote.get("mv")
        if tl and mv:
            x4 = mv / tl
            zrisk["raw"]["x4"] = _r(x4, 2)
            zrisk["parts"].append({"name": "市值/总负债", "val": _r(x4, 2), "unit": "倍",
                                   "score": 2 if x4 > 1 else (1 if x4 > 0.5 else 0),
                                   "note": "市场对债务的覆盖能力（经典 Z=X4）"})
            zrisk["max"] = len(zrisk["parts"]) * 2
    graham = calc_graham(fin, quote)
    peg = calc_peg(fin, quote)
    quality = calc_quality(fin, quote)
    tech = calc_technical(code) or {}

    # 估值合成：格雷厄姆安全边际 60% + PEG 40%
    g_s = None
    if graham:
        mg = graham["margin"] / 100
        g_s = 100 if mg > 0.3 else (70 if mg > 0 else (45 if mg > -0.2 else 20))
    p_s = None
    if peg and peg.get("peg") is not None:
        pv = peg["peg"]
        p_s = 95 if pv < 0.5 else (80 if pv < 1 else (65 if pv < 1.5 else (50 if pv < 2 else (35 if pv < 3 else 15))))
    elif peg and peg.get("growth") is not None and peg["growth"] <= 0:
        p_s = 30  # 负增长
    val_score = _blend([(g_s, 0.6), (p_s, 0.4)])

    # 技术合成：海龟 40% + 格兰维尔 30% + 动量 30%
    t_parts = []
    if tech.get("turtle"): t_parts.append((tech["turtle"]["score"], 0.4))
    if tech.get("granville"): t_parts.append((tech["granville"]["score"], 0.3))
    if tech.get("momentum"): t_parts.append((tech["momentum"]["score"], 0.3))
    tech_score = _blend(t_parts)

    fs_s = fscore["score"] / fscore["of"] * 100 if fscore and fscore["of"] else None
    z_s = sum(p["score"] for p in zrisk["parts"]) / zrisk["max"] * 100 if zrisk and zrisk["max"] else None

    total = _blend([
        (quality["score"] if quality else None, 0.25),
        (fs_s, 0.20),
        (val_score, 0.20),
        (tech_score, 0.25),
        (z_s, 0.10),
    ])
    total = _r(total, 0) if total is not None else None
    return {
        "ok": True,
        "code": code,
        "name": None,
        "quote": {"close": quote.get("close"), "pe": quote.get("pe"), "pb": quote.get("pb"), "mv": quote.get("mv")} if quote else None,
        "fscore": fscore,
        "zrisk": zrisk,
        "graham": graham,
        "peg": peg,
        "quality": quality,
        "technical": tech,
        "scores": {
            "quality": quality["score"] if quality else None,
            "fscore": _r(fs_s, 0) if fs_s else None,
            "valuation": _r(val_score, 0) if val_score is not None else None,
            "technical": _r(tech_score, 0) if tech_score is not None else None,
            "risk": _r(z_s, 0) if z_s else None,
        },
        "total": total,
        "verdict": build_verdict(total),
        "disclaimer": "以上为经典公开模型在本地数据上的机械化演算，输出为描述性参考，不构成投资建议；"
                      "财务数据存在滞后（报告期），模型各有失效边界（如 PEG 不适用于周期股/亏损股）。",
    }
