# -*- coding: utf-8 -*-
"""
fin_report.py — 近 8 年财报分析（长期基本面，描述性参考非投资建议）

数据来源：东财 datacenter 公开报表（业绩 RPT_LICO_FN_CPD / 资产负债 RPT_DMSK_FN_BALANCE /
现金流 RPT_DMSK_FN_CASHFLOW），取最近 8 个年报（12-31），缓存 12 小时。

输出：
  - 8 年年报逐行：营收/同比、净利/同比、毛利率、净利率、ROE(加权)、负债率、
    EPS、每股经营现金流、每股现金分红（从分配描述解析"10派X元"）
  - 长期指标：营收/净利 CAGR、ROE 均值、现金流含金量(ΣCFO/Σ净利)、盈利年数、
    增长年数、分红年数、平均负债率
  - 长期财务健康分 0~100（增长 50 + ROE 20 + 现金含金 15 + 杠杆 10 + 分红 5）
    → 五档：优秀/良好/中等/偏弱/差（描述性）
上市不足 8 年时用现有年报数并注明；ETF/基金不适用。
"""
import os
import re
import json
import time

import models_reader as mr

try:
    import cw_reader  # 通达信专业财务数据（本地离线，优先）
except Exception:
    cw_reader = None

TTL = 12 * 3600
YEARS = 8

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _annual_rows(rows, date_key, n):
    ann = [r for r in rows if "-12-31" in str(r.get(date_key, ""))]
    return ann[:n]


def _dividend_per_share(desc):
    """从分配描述解析每股现金分红（"10派X元"→X/10；"每股派X元"→X），无则 0。"""
    if not desc:
        return 0.0
    s = str(desc)
    if "不分配" in s:
        return 0.0
    m = re.search(r"每股派(?:现|息)?\s*([\d.]+)\s*元", s)
    if m:
        return float(m.group(1))
    m = re.search(r"派\s*([\d.]+)\s*元", s)
    return float(m.group(1)) / 10 if m else 0.0


def _dividend_events(code, mains=None):
    """采集全部报告期（年报/中报/三季报）的现金分红事件，含 2026 年中期分红。

    返回按报告期倒序的 [{year, period, dps, plan, kind}]，同一报告期去重。
    ASSIGNDSCRPT 中"不分配不转增"及空值会被过滤。mains 为复用已拉取的
    RPT_LICO_FN_CPD 行（东财路径已有），避免重复请求。
    """
    if mains is None:
        secu = mr._secucode(code)
        if not secu:
            return []
        try:
            mains = mr._dc_rows("RPT_LICO_FN_CPD", "REPORTDATE,ASSIGNDSCRPT", secu, 60, "REPORTDATE")
        except Exception:
            return []
    events = {}
    for r in (mains or []):
        rd = str(r.get("REPORTDATE", ""))[:10]
        desc = str(r.get("ASSIGNDSCRPT") or "").strip()
        if len(rd) < 10 or not desc or desc == "None":
            continue
        dps = round(_dividend_per_share(desc), 4)
        if dps <= 0:
            continue
        mm = rd[5:7]
        kind = {"12": "年度", "06": "中期", "09": "三季", "03": "一季"}.get(mm, "其他")
        events[rd] = {"year": int(rd[:4]), "period": rd, "dps": dps, "plan": desc, "kind": kind}
    return sorted(events.values(), key=lambda e: e["period"], reverse=True)


def _dividend_year_rows(events):
    """按年度聚合分红事件：一年内年报+中期多次派息合并，并保留每次方案。"""
    by = {}
    for e in events:
        y = int(e["year"])
        b = by.setdefault(y, {"dps": 0.0, "count": 0, "plans": [], "periods": [], "kinds": []})
        b["dps"] += e["dps"]
        b["count"] += 1
        b["plans"].append(e["plan"])
        b["periods"].append(e["period"])
        b["kinds"].append(e["kind"])
    out = []
    for y in sorted(by.keys(), reverse=True):  # 新 → 旧
        b = by[y]
        out.append({
            "year": y,
            "dps": round(b["dps"], 4),
            "count": b["count"],
            "plans": b["plans"],
            "periods": b["periods"],
            "kinds": b["kinds"],
            "plan": "；".join(b["plans"]),
        })
    return out


_TDX_ROOTS = [r"C:\zd_gjzq", r"C:\new_tdx", r"D:\new_tdx", r"C:\zd_zxzq_gm",
              r"C:\Program Files\new_tdx", r"C:\Program Files (x86)\new_tdx"]
_FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")


def _share_prices(code):
    """各年最后一个交易日收盘价 {年份: close}（不复权，用于股息率）。

    直接用 struct 解析通达信 .day（32B/条），不依赖 numpy/pandas——
    部分环境（Python 3.13）缺 numpy 会导致股息率整列为空。
    """
    import struct
    code = str(code).zfill(6)
    # 000xxx 在 sh 是指数、在 sz 才是个股，必须按代码规则定市场，不能顺序试探
    if code.startswith(("6", "5")):
        markets = ("sh",)
    elif code.startswith(("0", "3")):
        markets = ("sz",)
    elif code.startswith(("4", "8", "9")):
        markets = ("bj",)
    else:
        markets = ("sh", "sz", "bj")
    roots = ([os.environ.get("TDX_ROOT", "")] if os.environ.get("TDX_ROOT") else []) + _TDX_ROOTS
    fp = None
    for root in roots:
        if not root:
            continue
        for mkt in markets:
            cand = os.path.join(root, "vipdoc", mkt, "lday", mkt + code + ".day")
            if os.path.isfile(cand):
                fp = cand
                break
        if fp:
            break
    if not fp:
        return {}
    try:
        with open(fp, "rb") as f:
            buf = f.read()
    except Exception:
        return {}
    div = 1000.0 if code.startswith(_FUND_PREFIXES) else 100.0
    out = {}
    rec = struct.Struct("<IIIIIfII")
    for off in range(0, len(buf) - rec.size + 1, rec.size):
        date, _o, _h, _l, close, _amt, _vol, _res = rec.unpack_from(buf, off)
        y = date // 10000
        if 1990 <= y <= 2100 and close > 0:
            out[y] = close / div  # 按时间正序，同年后者覆盖 → 年末最后交易日
    return out


def _shares_by_year(code, years):
    """各年报总股本 {年份: 股数}（通达信本地财务，失败返回空）。"""
    out = {}
    if cw_reader is None or not cw_reader.is_available():
        return out
    for y in years:
        try:
            d = cw_reader.load_period(f"{y}1231")
            row = d.get(code) or {}
            if row.get("shares"):
                out[y] = row["shares"]
        except Exception:
            pass
    return out


def _enrich_dividends(code, rows, events=None):
    """补充分红画像：财报年报行 DPS 用「年度+中期」合计，并单独产出分红年表。

    返回 (available, div_rows)。div_rows 含尚无年报的年份（如 2026 年中报分红），
    每行带 count（当年派息次数）与 plans/periods/kinds（每次方案明细）。
    """
    if events is None:
        events = _dividend_events(code)
    if not events:
        return False, []
    by_year = _dividend_year_rows(events)
    dmap = {int(r["year"]): r for r in by_year}
    prices = _share_prices(code)
    share_years = [int(r["year"]) for r in rows] + list(dmap.keys())
    shares = _shares_by_year(code, share_years)
    eps_map = {int(r["year"]): r.get("eps") for r in rows}

    def derive(y, dps, count=None, plans=None, periods=None, kinds=None):
        px = prices.get(y)
        sh = shares.get(y)
        eps = eps_map.get(y)
        return {
            "dps": round(dps, 2),
            "count": count or 0,
            "plans": plans or [],
            "periods": periods or [],
            "kinds": kinds or [],
            "payout": round(dps / eps * 100, 1) if (dps > 0 and eps) else None,
            "year_end_price": round(px, 2) if px else None,
            "div_yield": round(dps / px * 100, 2) if (dps > 0 and px and px > 0) else None,
            "div_amount": round(dps * sh / 1e8, 2) if (dps > 0 and sh) else None,
        }

    # 财报年报行也用年度合计（含中期），保持两个表口径一致
    for row in rows:
        y = int(row["year"])
        b = dmap.get(y)
        row.update(derive(y, b["dps"] if b else 0.0,
                          b["count"] if b else 0, b["plans"] if b else [],
                          b["periods"] if b else [], b["kinds"] if b else []))
        row["div_plan"] = b["plan"] if b else None

    # 独立分红年表：不限于年报年份，2026 等中期分红年份也会出现
    div_rows = []
    for b in by_year:
        y = int(b["year"])
        item = {"year": y}
        item.update(derive(y, b["dps"], b["count"], b["plans"], b["periods"], b["kinds"]))
        item["plan"] = b["plan"]
        div_rows.append(item)

    # 每股分红同比（按旧→新顺序，跳过未分红年份；只有中期/三季、年报未出的年份不可比）
    prev = None
    for item in reversed(div_rows):
        complete = "年度" in item.get("kinds", [])
        item["partial"] = not complete  # 年报方案未出，当前仅为中期/三季派息
        if item["dps"] > 0 and prev and complete:
            item["dps_g"] = round((item["dps"] / prev - 1) * 100, 1)
        else:
            item["dps_g"] = None
        if item["dps"] > 0 and complete:
            prev = item["dps"]
    gmap = {r["year"]: r["dps_g"] for r in div_rows}
    for row in rows:
        row["dps_g"] = gmap.get(int(row["year"]))
    return any(r["dps"] > 0 for r in div_rows), div_rows


def _dividend_summary(rows):
    """连续分红年数 / 稳定性 / 分红率与股息率统计（rows 为新→旧）。"""
    ds = [r.get("dps") or 0 for r in rows]
    n = len(ds)
    streak = 0
    for v in ds:  # 新 → 旧，从最新年份往前数
        if v > 0:
            streak += 1
        else:
            break
    spans, cur = [], 0
    for v in reversed(ds):  # 旧 → 新统计历史连续段
        if v > 0:
            cur += 1
        elif cur:
            spans.append(cur)
            cur = 0
    if cur:
        spans.append(cur)
    payouts = [r["payout"] for r in rows if r.get("payout") is not None]
    yields_ = [r["div_yield"] for r in rows if r.get("div_yield") is not None]
    amounts = [r["div_amount"] for r in rows if r.get("div_amount") is not None]
    growing = sum(1 for r in rows if (r.get("dps_g") or 0) > 0)
    if not ds or all(v <= 0 for v in ds):
        stability = f"{n} 年均无现金分红"
    elif streak == n:
        stability = f"连续 {n} 年现金分红，未中断"
    elif streak >= 3:
        stability = f"近 {streak} 年连续分红，早期有中断"
    else:
        stability = f"分红不连续（最长连续 {max(spans) if spans else 0} 年）"
    return {
        "div_years": sum(1 for v in ds if v > 0),
        "streak": streak,
        "stability": stability,
        "payout_avg": round(sum(payouts) / len(payouts), 1) if payouts else None,
        "yield_avg": round(sum(yields_) / len(yields_), 2) if yields_ else None,
        "yield_latest": next((r["div_yield"] for r in rows if r.get("div_yield") is not None), None),
        "div_amount_total": round(sum(amounts), 1) if amounts else None,
        "div_amount_latest": next((r["div_amount"] for r in rows if r.get("div_amount") is not None), None),
        "dps_growing_years": growing,
    }


def _cagr(series):
    """复合年增长率；序列含非正值时改用均值同比，标记近似。"""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return None, False
    if vals[0] > 0 and vals[-1] > 0:
        years = len(vals) - 1
        return (vals[-1] / vals[0]) ** (1 / years) - 1, False
    grows = [g for g in vals if g is not None]
    grows = [g for g in grows if g == g]
    if grows:
        return sum(grows) / len(grows) / 100, True
    return None, False


def _local_rows(code):
    """通达信本地财务数据 → 与东财路径同构的 rows（dps 留空待网络补充）。"""
    if cw_reader is None or not cw_reader.is_available():
        return None
    h = cw_reader.history_for(code, YEARS)
    if not h:
        return None
    rows = []
    for r in h:
        rev, np_ = r.get("rev"), r.get("np")
        roe = r.get("roe_w") if r.get("roe_w") is not None else r.get("roe")
        rows.append({
            "year": r["date"][:4],
            "rev": round(rev / 1e8, 2) if rev else None,
            "rev_g": round(r.get("rev_g"), 1) if r.get("rev_g") is not None else None,
            "np": round(np_ / 1e8, 2) if np_ is not None else None,
            "np_g": round(r.get("np_g"), 1) if r.get("np_g") is not None else None,
            "gm": round(r.get("gross_margin"), 1) if r.get("gross_margin") is not None else None,
            "net_rate": round(r.get("net_margin"), 1) if r.get("net_margin") is not None else None,
            "roe": round(roe, 1) if roe is not None else None,
            "debt": round(r.get("debt_ratio"), 1) if r.get("debt_ratio") is not None else None,
            "eps": round(r.get("eps"), 2) if r.get("eps") is not None else None,
            "ocf_ps": round(r.get("cfo_ps"), 2) if r.get("cfo_ps") is not None else None,
            "ocf": round(r["cfo"] / 1e8, 2) if r.get("cfo") else None,
            "dps": None,  # 通达信包无分红描述，尝试东财补充
        })
    return rows


def _enrich_dps(code, rows):
    """东财补充每股分红（仅 ASSIGNDSCRPT 一个字段，失败不影响本地主数据）。"""
    try:
        secu = mr._secucode(code)
        if not secu:
            return False
        mains = mr._dc_rows("RPT_LICO_FN_CPD", "REPORTDATE,ASSIGNDSCRPT", secu, 40, "REPORTDATE")
        if not mains:
            return False
        dmap = {str(r["REPORTDATE"])[:4]: round(_dividend_per_share(r.get("ASSIGNDSCRPT")), 2)
                for r in mains if "-12-31" in str(r.get("REPORTDATE", ""))}
        for row in rows:
            row["dps"] = dmap.get(row["year"])
        return True
    except Exception:
        return False


def fetch_fin8(code, refresh=False):
    # v2：分红改为事件化采集（含 2026 中期分红 / 一年多次派息），旧缓存结构不兼容
    path = os.path.join(CACHE_DIR, f"fin8_v2_{code}.json")
    if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < TTL:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    source = "eastmoney"
    mains = None
    rows = _local_rows(code)
    if rows:
        source = "tdx_cw"
    else:
        secu = mr._secucode(code)
        if not secu:
            if code.startswith(("1", "5")):
                return {"ok": False, "is_fund": True,
                        "message": "ETF/基金无个股财报；基金规模与跟踪误差不在本模块范围。"}
            return {"ok": False, "error": "非沪深京证券代码"}
        mains = mr._dc_rows("RPT_LICO_FN_CPD",
                            "REPORTDATE,BASIC_EPS,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,"
                            "WEIGHTAVG_ROE,YSTZ,SJLTZ,BPS,MGJYXJJE,XSMLL,ASSIGNDSCRPT",
                            secu, 40, "REPORTDATE")
        bals = mr._dc_rows("RPT_DMSK_FN_BALANCE", "REPORT_DATE,DEBT_ASSET_RATIO,TOTAL_ASSETS", secu, 40, "REPORT_DATE")
        cfs = mr._dc_rows("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE,NETCASH_OPERATE", secu, 40, "REPORT_DATE")
        if not mains:
            return {"ok": False, "error": "财务数据不可得（可能为新上市/无报表）"}

        ann_m = _annual_rows(mains, "REPORTDATE", YEARS)
        if not ann_m:
            return {"ok": False, "error": "无年报数据"}
        ann_b = {str(r["REPORT_DATE"])[:10]: r for r in _annual_rows(bals, "REPORT_DATE", YEARS + 2)}
        ann_c = {str(r["REPORT_DATE"])[:10]: r for r in _annual_rows(cfs, "REPORT_DATE", YEARS + 2)}

        rows = []
        for r in ann_m:
            date = str(r["REPORTDATE"])[:10]
            rev = mr._f(r.get("TOTAL_OPERATE_INCOME"))
            np_ = mr._f(r.get("PARENT_NETPROFIT"))
            b = ann_b.get(date) or {}
            c = ann_c.get(date) or {}
            rows.append({
                "year": date[:4],
                "rev": round(rev / 1e8, 2) if rev else None,
                "rev_g": round(mr._f(r.get("YSTZ")), 1) if r.get("YSTZ") is not None else None,
                "np": round(np_ / 1e8, 2) if np_ is not None else None,
                "np_g": round(mr._f(r.get("SJLTZ")), 1) if r.get("SJLTZ") is not None else None,
                "gm": round(mr._f(r.get("XSMLL")), 1) if r.get("XSMLL") is not None else None,
                "net_rate": round(np_ / rev * 100, 1) if (np_ is not None and rev) else None,
                "roe": round(mr._f(r.get("WEIGHTAVG_ROE")), 1) if r.get("WEIGHTAVG_ROE") is not None else None,
                "debt": round(mr._f(b.get("DEBT_ASSET_RATIO")), 1) if b.get("DEBT_ASSET_RATIO") is not None else None,
                "eps": mr._f(r.get("BASIC_EPS")),
                "ocf_ps": mr._f(r.get("MGJYXJJE")),
                "ocf": round(mr._f(c.get("NETCASH_OPERATE")) / 1e8, 2) if c.get("NETCASH_OPERATE") else None,
                "dps": _dividend_per_share(r.get("ASSIGNDSCRPT")),
            })
        rows.reverse()  # 旧 → 新

    # 分红事件化：含年报/中报/三季报方案，2026 中期分红与一年多次派息均可展示
    events = _dividend_events(code, mains)  # 东财路径复用已拉取的 mains，本地路径重新请求
    dps_ok, div_rows = _enrich_dividends(code, rows, events)
    div = _dividend_summary(div_rows)

    # 长期指标
    n = len(rows)
    rev_series = [r["rev"] for r in rows if r["rev"] is not None]
    np_series = [r["np"] for r in rows if r["np"] is not None]
    rev_cagr, rev_approx = _cagr(rev_series)
    np_cagr, np_approx = _cagr(np_series)
    roes = [r["roe"] for r in rows if r["roe"] is not None]
    debts = [r["debt"] for r in rows if r["debt"] is not None]
    sum_ocf = sum(r["ocf"] for r in rows if r["ocf"] is not None)
    sum_np = sum(r["np"] for r in rows if r["np"] is not None and r["np"] > 0)
    cash_ratio = round(sum_ocf / sum_np, 2) if (sum_ocf and sum_np and sum_np > 0) else None
    metrics = {
        "years": n,
        "rev_cagr": round(rev_cagr * 100, 1) if rev_cagr is not None else None,
        "rev_cagr_approx": rev_approx,
        "np_cagr": round(np_cagr * 100, 1) if np_cagr is not None else None,
        "np_cagr_approx": np_approx,
        "roe_avg": round(sum(roes) / len(roes), 1) if roes else None,
        "roe_min": min(roes) if roes else None,
        "roe_max": max(roes) if roes else None,
        "debt_avg": round(sum(debts) / len(debts), 1) if debts else None,
        "cash_ratio": cash_ratio,
        "profit_years": sum(1 for r in rows if (r["np"] or 0) > 0),
        "np_up_years": sum(1 for r in rows if (r["np_g"] or -1e9) > 0),
        "div_years": div["div_years"],
        "div_total": round(sum(r["dps"] or 0 for r in div_rows), 2),
    }
    metrics.update(div)  # 追加连续年数/派息率/股息率/分红总额等分红画像

    # 长期财务健康分
    def _sc(v, bands):
        for th, s in bands:
            if v is None:
                continue
            if v >= th:
                return s
        return bands[-1][1]

    cagr_score_rev = _sc(metrics["rev_cagr"], [(20, 25), (10, 20), (5, 15), (0, 10), (-1e9, 4)])
    cagr_score_np = _sc(metrics["np_cagr"], [(20, 25), (10, 20), (5, 15), (0, 10), (-1e9, 4)])
    roe_s = _sc(metrics["roe_avg"], [(15, 20), (10, 16), (7, 12), (3, 8), (0.01, 4), (-1e9, 0)])
    cash_s = _sc(metrics["cash_ratio"], [(1.0, 15), (0.7, 11), (0.4, 7), (0.01, 3), (-1e9, 0)])
    debt_s = 10 if metrics["debt_avg"] is not None and metrics["debt_avg"] < 35 else (
        8 if metrics["debt_avg"] is not None and metrics["debt_avg"] < 55 else (
            4 if metrics["debt_avg"] is not None and metrics["debt_avg"] < 70 else 1))
    div_s = 5 if metrics["div_years"] >= 6 else (3 if metrics["div_years"] >= 4 else (1 if metrics["div_years"] >= 2 else 0))
    score = cagr_score_rev + cagr_score_np + roe_s + cash_s + debt_s + div_s
    if score >= 75:
        verdict = "优秀"
    elif score >= 60:
        verdict = "良好"
    elif score >= 45:
        verdict = "中等"
    elif score >= 30:
        verdict = "偏弱"
    else:
        verdict = "差"

    src_note = {
        "tdx_cw": "数据源：通达信专业财务数据（本地离线）；营收为利润表营业收入口径"
                  + ("；分红明细取自东财全部报告期方案" if dps_ok else "；分红方案网络不可得，明细暂缺"),
        "eastmoney": "数据源：东方财富 datacenter（网络）",
    }[source]
    data = {
        "ok": True,
        "code": code,
        "source": source,
        "rows": rows,
        "metrics": metrics,
        "dividend": {
            "summary": div,
            "rows": [{"year": r["year"], "dps": r.get("dps"), "count": r.get("count", 1),
                      "partial": r.get("partial", False),
                      "periods": r.get("periods", []), "kinds": r.get("kinds", []),
                      "plans": r.get("plans", []), "payout": r.get("payout"),
                      "div_yield": r.get("div_yield"), "div_amount": r.get("div_amount"),
                      "dps_g": r.get("dps_g"), "year_end_price": r.get("year_end_price"),
                      "plan": r.get("plan")}
                     for r in div_rows],
            "events": events,  # 每次派息明细（含 2026 中报、一年多次）
            "available": dps_ok,
            "note": "每股分红按年度汇总年报/中报/三季报全部现金分红方案（'10派X元'÷10）；"
                    "一年多次派息的每股分红为合计值，次数见「次数」列；"
                    "派息率=每股分红/EPS（无年报 EPS 的年份为空）；"
                    "股息率=每股分红/年末收盘价（2026 年为最新价，不复权）；"
                    "分红总额=每股分红×当年总股本；标记「仅中期」的年份年报方案未出、"
                    "同比不可比。方案为预案时以最终实施公告为准。",
        },
        "score": score,
        "parts": [
            {"name": "营收增长(CAGR)", "score": cagr_score_rev, "max": 25},
            {"name": "净利增长(CAGR)", "score": cagr_score_np, "max": 25},
            {"name": "ROE 均值", "score": roe_s, "max": 20},
            {"name": "现金流含金量", "score": cash_s, "max": 15},
            {"name": "杠杆水平", "score": debt_s, "max": 10},
            {"name": "分红连续性", "score": div_s, "max": 5},
        ],
        "verdict": verdict,
        "note": (f"共 {n} 个年报" + ("（上市不足 8 年）" if n < YEARS else "")) +
                ("；净利/营收含负值年份，CAGR 用均值同比近似" if (rev_approx or np_approx) else "")
                + "；" + src_note,
        "disclaimer": "基于公开年报的机械化统计，描述长期基本面趋势，不构成投资建议；"
                      "财报有滞后性，需结合最新季报与行业景气度综合判断。",
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
    return data
