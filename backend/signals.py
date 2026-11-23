# -*- coding: utf-8 -*-
"""
signals.py — 买卖信号扫描引擎 + 全市场选股

信号类型：
  macd_golden  MACD 金叉（DIF 上穿 DEA）
  macd_death   MACD 死叉
  ma_golden    MA5 上穿 MA20
  ma_death     MA5 下穿 MA20
  breakout20   收盘突破 20 日新高
  volume_burst 放量（成交量 > 5 日均量 1.8 倍且涨幅 > 2%）
  kdj_oversold KDJ 超卖区金叉（K 上穿 D 且 K<30）
  kdj_overbought KDJ 超买区死叉（K 下穿 D 且 K>70）
  boll_lower   触及布林下轨
  boll_upper   触及布林上轨

选股条件（组合式）：
  trend_up     均线多头排列 MA5>MA10>MA20
  macd_up      DIF>DEA 且 MACD 柱>0
  volume_up    放量
  low_rsi      RSI6<30 超卖
  breakout     突破 20 日新高
"""
import numpy as np
import pandas as pd
from indicators import enrich

SIGNAL_LABELS = {
    "macd_golden": "MACD金叉", "macd_death": "MACD死叉",
    "ma_golden": "均线金叉", "ma_death": "均线死叉",
    "breakout20": "突破20日新高", "volume_burst": "放量异动",
    "kdj_oversold": "KDJ超卖金叉", "kdj_overbought": "KDJ超买死叉",
    "boll_lower": "触及布林下轨", "boll_upper": "触及布林上轨",
}

FILTER_LABELS = {
    "trend_up": "均线多头排列", "macd_up": "MACD多头",
    "volume_up": "放量", "low_rsi": "RSI超卖", "breakout": "突破新高",
}

# 每种信号的教科书式解释
SIGNAL_MEANING = {
    "macd_golden": "MACD 快线（DIF）上穿慢线（DEA）形成金叉，代表短期上涨动能由弱转强，常被视作趋势转多的确认信号之一。",
    "macd_death": "MACD 快线（DIF）下穿慢线（DEA）形成死叉，代表下跌动能占优、上涨动能减弱，常被视作趋势转弱的预警信号之一。",
    "ma_golden": "5 日均线上穿 20 日均线，说明短期持仓成本快速抬升、短线资金转强，属于短线趋势转多的常见信号。",
    "ma_death": "5 日均线下穿 20 日均线，说明短线跌幅快于中期均线、短线资金转弱，属于短线趋势转弱的常见信号。",
    "breakout20": "收盘价突破近 20 个交易日最高价且当日收阳，代表多头重新掌握主动、向上突破前期套牢区。",
    "volume_burst": "成交量超过 5 日均量的 1.8 倍且当日涨幅大于 2%，说明有增量资金明显介入，量价齐升。",
    "kdj_oversold": "KDJ 在超卖区（K<30）形成金叉，反映短线恐慌抛售接近尾声，存在超跌反弹的可能。",
    "kdj_overbought": "KDJ 在超买区（K>70）形成死叉，反映短线追高资金衰竭，存在回调或震荡整固的可能。",
    "boll_lower": "当日最低价触及布林带下轨，说明价格短期偏离均值较远、被压到波动区间下沿，可能出现均值回归式反弹。",
    "boll_upper": "当日最高价触及布林带上轨，说明价格短期涨幅偏快、触及波动区间上沿，可能出现滞涨或回落。",
}


def _f(x, n=2):
    try:
        v = float(x)
        return ("—" if v != v else f"{v:.{n}f}")
    except Exception:
        return "—"


def explain_signal(t, cur, prev, extra=None):
    """结合信号当日指标值，返回 {detail, caution}：
    detail 为信号含义解释；caution 为买卖两面的审慎分析（含可操作建议）。"""
    extra = extra or {}
    detail = SIGNAL_MEANING.get(t, "")
    # 通用位置背景
    ctx = []
    close, ma20 = cur.get("close"), cur.get("ma20")
    if pd.notna(close) and pd.notna(ma20) and ma20:
        pos = (close / ma20 - 1) * 100
        ctx.append(f"现价较 MA20 {'高' if pos >= 0 else '低'}{_f(abs(pos), 1)}%")
    rsi = cur.get("rsi6")
    if pd.notna(rsi):
        ctx.append(f"RSI6={_f(rsi, 1)}")
    k = cur.get("k")
    if pd.notna(k):
        ctx.append(f"KDJ K值={_f(k, 1)}")
    bg = "；".join(ctx)

    caution = ""
    if t == "macd_golden":
        dif = cur.get("dif")
        zone = "零轴上方" if (pd.notna(dif) and dif > 0) else "零轴下方"
        caution = (f"金叉出现在{zone}（DIF={_f(dif)}）。零轴上方金叉多为上涨中继，但位置越高动能透支越明显，追买风险越大；"
                   f"零轴下方金叉多为超跌反弹，需等量能与均线确认后才可靠。审慎建议：不宜单凭金叉立即买入，"
                   f"可等收盘站稳 MA5/MA10 或放量回踩不破金叉当日低点后再分批介入；若价格仍在 MA20 下方（下降趋势），"
                   f"反弹金叉失败率较高，以观察为主。")
    elif t == "macd_death":
        dif = cur.get("dif")
        zone = "零轴上方" if (pd.notna(dif) and dif > 0) else "零轴下方"
        caution = (f"死叉出现在{zone}（DIF={_f(dif)}）。高位死叉常预示阶段性回调；零轴下方死叉则可能延续弱势。"
                   f"审慎建议：死叉滞后，不宜恐慌性一键清仓。若持仓盈利且收盘跌破 MA20，可分批减仓锁定利润；"
                   f"若 MA20 仍向上、价格在其上方，可只减部分仓位并观察 2-3 日是否为震荡假信号。")
    elif t == "ma_golden":
        caution = ("横盘期均线金叉会反复出现，噪音较大。审慎建议：确认 MA20 方向向上、收盘站稳 MA20 且量能配合后"
                   "再分批买入；若当日价格仍在 MA20 下方，单日金叉信号可靠性不足，以观望为主。")
    elif t == "ma_death":
        caution = ("短期均线死叉未必是趋势反转，需结合 MA20 走向判断。审慎建议：若收盘跌破 MA20 且 MA20 走平或下弯，"
                   "可减仓避险；若仅是短线回踩、中期趋势完好，减仓观察即可，不必清仓。")
    elif t == "breakout20":
        hp = extra.get("high_prev")
        vr = extra.get("vr")
        caution = (f"突破前 20 日高点 {_f(hp)}，当日量比（对 5 日均量）={_f(vr)}。放量（>1.5）突破更有效，"
                   f"缩量突破假信号概率高。审慎建议：追高买入风险大，可等回踩突破位（约 {_f(hp)}）不破后再介入；"
                   f"若 1-2 日内收盘跌回 20 日高点下方，视为假突破应止损退出。")
    elif t == "volume_burst":
        vr = extra.get("vr")
        high_pos = pd.notna(ma20) and pd.notna(close) and close > ma20 * 1.15
        caution = (f"当日量比={_f(vr)}。低位放量上涨通常是资金介入信号；高位放巨量则需警惕对倒出货。"
                   f"审慎建议：单日放量不构成买点，可等缩量回踩企稳后再介入；"
                   + ("当前价格已高于 MA20 约 15% 以上，属高位放量，追买需控制仓位。" if high_pos else
                      "价格位置不高时可小幅试探，破前日低点止损。"))
    elif t == "kdj_oversold":
        caution = ("超卖可以更超卖，下跌趋势中 KDJ 金叉常反复失败。审慎建议：等待企稳确认（收复 MA5 或放量阳线）"
                   "后再以小仓位左侧试探，或等 K 值上穿 50 的右侧信号再介入；单凭超卖金叉买入胜率不高。")
    elif t == "kdj_overbought":
        caution = ("强势股在超买区容易钝化，仅凭 KDJ 死叉卖出常会卖飞主升段。审慎建议：若当日或次日伴随放巨量滞涨、"
                   "长上影或跌破前一日低点，可分批减仓应对回调；趋势完好时以减仓为主而非清仓。")
    elif t == "boll_lower":
        caution = ("触下轨后价格可能沿下轨持续阴跌，「触轨即反弹」并不可靠。审慎建议：等待止跌确认"
                   "（收复 MA5 或放量收复布林中轨）再考虑低吸；左侧抄底仅适合小仓位并预设止损。")
    elif t == "boll_upper":
        caution = ("触及上轨本身不构成卖出依据——强趋势中价格可沿上轨连续运行。审慎建议：观察次日若出现放量滞涨"
                   "或收长上影，可分批止盈；无量贴上轨运行则持股观望，不必急于卖出。")
    if bg:
        caution = f"[{bg}] " + caution
    return {"detail": detail, "caution": caution}


def _cross_up(a, b):
    return (a.iloc[-2] <= b.iloc[-2]) and (a.iloc[-1] > b.iloc[-1])


def _cross_down(a, b):
    return (a.iloc[-2] >= b.iloc[-2]) and (a.iloc[-1] < b.iloc[-1])


def detect_signals(df, lookback=3, enriched=None):
    """检测最近 lookback 根内出现的信号，返回 [{date, type, label}]。
    enriched: 可选，预先计算好的指标 DataFrame，避免重复计算。"""
    if df is None or len(df) < 30:
        return []
    d = enriched if enriched is not None else enrich(df)
    signals = []
    for i in range(max(1, len(d) - lookback), len(d)):
        cur = d.iloc[i]
        prev = d.iloc[i - 1]
        date = d.index[i]
        dstr = cur["date"].strftime("%Y-%m-%d") if hasattr(cur["date"], "strftime") else str(date)

        def add(t, extra=None):
            e = explain_signal(t, cur, prev, extra)
            signals.append({"date": dstr, "type": t, "label": SIGNAL_LABELS[t],
                            "detail": e["detail"], "caution": e["caution"]})

        if "dif" in d:
            dif = d["dif"].iloc[:i+1]; dea = d["dea"].iloc[:i+1]
            if len(dif) >= 2:
                if _cross_up(dif, dea): add("macd_golden")
                if _cross_down(dif, dea): add("macd_death")
        if "ma5" in d and "ma20" in d and pd.notna(cur.get("ma20")) and pd.notna(prev.get("ma20")):
            ma5 = d["ma5"].iloc[:i+1]; ma20 = d["ma20"].iloc[:i+1]
            if _cross_up(ma5, ma20): add("ma_golden")
            if _cross_down(ma5, ma20): add("ma_death")
        # 20 日新高（不含当日）
        if i >= 20:
            high_prev = d["high"].iloc[max(0, i-20):i].max()
            if cur["close"] > high_prev and cur["close"] > cur["open"]:
                vr = float(cur["volume"] / cur["vma5"]) if pd.notna(cur.get("vma5")) and cur["vma5"] > 0 else None
                add("breakout20", {"high_prev": float(high_prev), "vr": vr})
        # 放量异动
        if pd.notna(cur.get("vma5")) and cur["vma5"] > 0:
            chg = (cur["close"] / prev["close"] - 1) * 100 if prev["close"] > 0 else 0
            if cur["volume"] > cur["vma5"] * 1.8 and chg > 2:
                add("volume_burst", {"vr": float(cur["volume"] / cur["vma5"])})
        # KDJ
        if pd.notna(cur.get("k")) and pd.notna(prev.get("k")):
            k = d["k"].iloc[:i+1]; dd = d["d"].iloc[:i+1]
            if _cross_up(k, dd) and cur["k"] < 30: add("kdj_oversold")
            if _cross_down(k, dd) and cur["k"] > 70: add("kdj_overbought")
        # 布林带
        if pd.notna(cur.get("boll_low")) and cur["low"] <= cur["boll_low"]:
            add("boll_lower")
        if pd.notna(cur.get("boll_up")) and cur["high"] >= cur["boll_up"]:
            add("boll_upper")
    return signals


def match_filters(df, filters, d=None):
    """判断单只股票是否满足全部选股条件。
    d: 可选，预先计算好的指标 DataFrame（尾部 60 根）。"""
    if df is None or len(df) < 60:
        return False
    if d is None:
        d = enrich(df.tail(60).reset_index(drop=True))
    cur = d.iloc[-1]
    prev = d.iloc[-2]
    for f in filters:
        if f == "trend_up":
            if not (pd.notna(cur.get("ma5")) and pd.notna(cur.get("ma10")) and pd.notna(cur.get("ma20"))):
                return False
            if not (cur["ma5"] > cur["ma10"] > cur["ma20"]):
                return False
        elif f == "macd_up":
            if not (cur.get("dif", 0) > cur.get("dea", 0) and cur.get("macd", 0) > 0):
                return False
        elif f == "volume_up":
            vma5 = cur.get("vma5", 0)
            if not (pd.notna(vma5) and vma5 > 0 and cur["volume"] > vma5 * 1.5):
                return False
        elif f == "low_rsi":
            if not (cur.get("rsi6", 50) < 30):
                return False
        elif f == "breakout":
            if len(d) < 21 or cur["close"] <= d["high"].iloc[-21:-1].max():
                return False
        elif f == "ma_golden":
            ma5 = d["ma5"].iloc[-2:]; ma20 = d["ma20"].iloc[-2:]
            if not (pd.notna(ma20.iloc[0]) and _cross_up(ma5, ma20)):
                return False
        else:
            return False  # 未知过滤条件：一律不通过（避免"未知条件=全通过"误选全市场）
    return True


def scan_market(reader, mode="signals", filters=None, signal_types=None,
                limit=80, progress_cb=None):
    """
    全市场扫描。
    mode='signals' 扫描信号；mode='filter' 按条件选股。
    返回按最新日期排序的结果列表。
    """
    codes = reader.all_codes()
    results = []
    # 只扫描个股代码段，排除指数/基金/债券
    stock_codes = [c for c in codes if c.startswith(("60", "68", "00", "30", "43", "83", "87", "92"))]
    total = len(stock_codes)
    wanted = set(signal_types) if signal_types else None
    for i, code in enumerate(stock_codes):
        try:
            df = reader.read_recent_cached(code, n=90)
            if df.empty or len(df) < 60:
                continue
            d = enrich(df, light=True)  # 全市场扫描用轻量指标，每只只算一次
            cur = d.iloc[-1]
            prev_close = d.iloc[-2]["close"]
            change = round((cur["close"] / prev_close - 1) * 100, 2) if prev_close > 0 else 0
            if mode == "signals":
                sigs = detect_signals(df, lookback=2, enriched=d)
                if sigs:
                    for s in sigs:
                        if wanted and s["type"] not in wanted:
                            continue
                        results.append({
                            "code": code, "name": reader.name_of(code),
                            "date": s["date"], "signal": s["type"], "label": s["label"],
                            "detail": s.get("detail"), "caution": s.get("caution"),
                            "close": round(float(cur["close"]), 2),
                            "change": change,
                        })
            else:
                tail = d.tail(60)
                if match_filters(df, filters or [], d=tail):
                    results.append({
                        "code": code, "name": reader.name_of(code),
                        "date": cur["date"].strftime("%Y-%m-%d"),
                        "close": round(float(cur["close"]), 2),
                        "change": change,
                        "ma5": round(float(cur["ma5"]), 2) if pd.notna(cur.get("ma5")) else None,
                        "rsi6": round(float(cur["rsi6"]), 1) if pd.notna(cur.get("rsi6")) else None,
                    })
        except Exception:
            continue
        if progress_cb and (i + 1) % 200 == 0:
            progress_cb(i + 1, total)
    results.sort(key=lambda x: (x["date"], x.get("change", 0)), reverse=True)
    return results[:limit], {"scanned": total}
