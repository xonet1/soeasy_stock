# -*- coding: utf-8 -*-
"""
volume_analysis.py — 量能状态分析 + 逐日综合研判

全部基于本地通达信日线真实数据计算（量比=当日成交量/5日均量）：
  1. volume_records：最近 N 根 K 线的放量/缩量状态、量价关系与逐条解读
  2. daily_verdicts：最近 N 日的六维综合研判（趋势/位置/MACD/KDJ/RSI/布林/量能）
     规则化评分（-100~+100），输出偏多/中性/偏空结论与分项明细

程序化参考，非投资建议。
"""
import pandas as pd


def _f(x, n=2):
    try:
        v = float(x)
        if v != v:
            return None
        return round(v, n)
    except Exception:
        return None


def _chg(cur, prev):
    try:
        if prev["close"] and float(prev["close"]) > 0:
            return (float(cur["close"]) / float(prev["close"]) - 1) * 100
    except Exception:
        pass
    return 0.0


def volume_state(vr):
    """量比 → (状态名, 状态码)。码：2 明显放量 / 1 放量 / 0 平量 / -1 缩量 / -2 明显缩量。"""
    if vr is None:
        return ("—", 0)
    if vr >= 1.8:
        return ("明显放量", 2)
    if vr >= 1.3:
        return ("放量", 1)
    if vr >= 0.9:
        return ("平量", 0)
    if vr >= 0.6:
        return ("缩量", -1)
    return ("明显缩量", -2)


def _pv_pattern(vr, chg):
    """量价关系命名。"""
    if vr is None:
        return "量能未知"
    if vr >= 1.3:
        if chg > 2:
            return "放量上涨"
        if chg < -2:
            return "放量下跌"
        return "放量滞涨"
    if vr < 0.9:
        if chg > 2:
            return "缩量上涨"
        if chg < -2:
            return "缩量下跌"
        return "缩量整理"
    if chg > 2:
        return "平量上涨"
    if chg < -2:
        return "平量下跌"
    return "平量整理"


PV_NOTES = {
    "放量上涨": "量价齐升，买盘集中介入：低位多为资金启动信号，高位需防对倒出货，宜关注次日能否续量。",
    "放量下跌": "放量下跌，抛压沉重：若跌破关键支撑应警惕趋势转弱，可考虑减仓避险；低位恐慌性放量后常见阶段底。",
    "放量滞涨": "放量但价格停滞，多空分歧巨大：高位出现大概率是出货信号，宜减仓或观望；低位可能是换手吸筹。",
    "缩量上涨": "缩量上涨，追涨意愿不足：无量上涨持续性存疑，多为反抽或抛压轻的惯性上行，不宜重仓追高。",
    "缩量下跌": "缩量下跌，抛压趋于衰竭：接近阶段底部的特征之一，可观察缩量企稳后的放量转折点。",
    "缩量整理": "缩量横盘，多空观望：等待方向选择，突破方向需量能确认（放量突破才有效）。",
    "平量上涨": "量能平稳的温和上行，趋势延续概率一般，关注量能是否逐步放大。",
    "平量下跌": "量能平稳的温和回调，若无放量破位可视为正常整理。",
    "平量整理": "量价均平稳，处于均衡状态，等待增量信息。",
    "量能未知": "量能数据不足，无法判断。",
}


def volume_records(d, days=20):
    """最近 days 根 K 线的量能记录。d 为 enrich 后的 DataFrame。"""
    out = []
    n = len(d)
    if n < 2:
        return out
    for i in range(max(1, n - days), n):
        cur, prev = d.iloc[i], d.iloc[i - 1]
        date = cur["date"].strftime("%Y-%m-%d") if hasattr(cur["date"], "strftime") else str(cur["date"])
        chg = _chg(cur, prev)
        vr = None
        if pd.notna(cur.get("vma5")) and float(cur["vma5"]) > 0:
            vr = float(cur["volume"]) / float(cur["vma5"])
        state, code = volume_state(vr)
        pattern = _pv_pattern(vr, chg)
        out.append({
            "date": date,
            "close": _f(cur["close"], 3 if str(cur.get("code", "")).startswith(("15", "16", "5")) else 2) if pd.notna(cur.get("close")) else None,
            "chg": _f(chg, 2),
            "vr": _f(vr, 2),
            "state": state,
            "state_code": code,
            "pattern": pattern,
            "note": PV_NOTES.get(pattern, ""),
        })
    return out


def _verdict_label(score):
    if score >= 40:
        return "强偏多", 2
    if score >= 15:
        return "偏多", 1
    if score > -15:
        return "中性", 0
    if score > -40:
        return "偏空", -1
    return "强偏空", -2


def daily_verdicts(d, days=10):
    """最近 days 日的六维综合研判。返回逐日 {date, score, verdict, parts, summary, volume}。"""
    out = []
    n = len(d)
    if n < 2:
        return out
    for i in range(max(1, n - days), n):
        cur, prev = d.iloc[i], d.iloc[i - 1]
        date = cur["date"].strftime("%Y-%m-%d") if hasattr(cur["date"], "strftime") else str(cur["date"])
        score = 0
        parts = []

        def add(dim, text, delta):
            nonlocal score
            score += delta
            parts.append({"dim": dim, "text": text, "delta": delta})

        # 1/2 趋势与位置（均线）
        ma5, ma10, ma20 = cur.get("ma5"), cur.get("ma10"), cur.get("ma20")
        if all(pd.notna(x) for x in (ma5, ma10, ma20)):
            if ma5 > ma10 > ma20:
                add("趋势", "均线多头排列 MA5>MA10>MA20", 25)
            elif ma5 < ma10 < ma20:
                add("趋势", "均线空头排列 MA5<MA10<MA20", -25)
            else:
                add("趋势", "均线纠缠，方向未明", 0)
            if float(cur["close"]) > float(ma20):
                add("位置", f"收盘站上 MA20（{_f(ma20)}）", 10)
            else:
                add("位置", f"收盘位于 MA20（{_f(ma20)}）下方", -10)
        # 3 MACD 动能
        dif, dea = cur.get("dif"), cur.get("dea")
        if pd.notna(dif) and pd.notna(dea):
            bar_cur, bar_prev = cur.get("macd"), prev.get("macd")
            expanding = pd.notna(bar_cur) and pd.notna(bar_prev) and abs(float(bar_cur)) > abs(float(bar_prev))
            if float(dif) > float(dea):
                add("动能", f"DIF>DEA，红柱{'扩大' if expanding else '收敛'}", 15 if expanding else 10)
            else:
                add("动能", f"DIF<DEA，绿柱{'扩大' if expanding else '收敛'}", -15 if expanding else -10)
        # 4 KDJ
        k, dd = cur.get("k"), cur.get("d")
        if pd.notna(k) and pd.notna(dd):
            if float(k) > float(dd):
                t = f"K>D 多头（K={_f(k, 1)}）"
                dl = 10
            else:
                t = f"K<D 空头（K={_f(k, 1)}）"
                dl = -10
            if float(k) > 85:
                t += "，超买区防回调"
            elif float(k) < 15:
                t += "，超卖区察反弹"
            add("KDJ", t, dl)
        # 5 RSI
        rsi = cur.get("rsi6")
        if pd.notna(rsi):
            r = float(rsi)
            if r > 80:
                add("RSI", f"RSI6={_f(rsi, 1)} 超买，短线过热", -10)
            elif r < 20:
                add("RSI", f"RSI6={_f(rsi, 1)} 超卖，反弹窗口", 5)
            elif r >= 50:
                add("RSI", f"RSI6={_f(rsi, 1)} 强势区", 5)
            else:
                add("RSI", f"RSI6={_f(rsi, 1)} 弱势区", -5)
        # 6 布林位置
        boll_up, boll_mid, boll_low = cur.get("boll_up"), cur.get("boll_mid"), cur.get("boll_low")
        if pd.notna(boll_mid):
            close = float(cur["close"])
            if close >= float(boll_mid):
                t, dl = "价格位于布林中轨上方", 10
            else:
                t, dl = "价格位于布林中轨下方", -10
            if pd.notna(boll_up) and float(cur["high"]) >= float(boll_up):
                t += "，触及上轨"
            elif pd.notna(boll_low) and float(cur["low"]) <= float(boll_low):
                t += "，触及下轨"
            add("布林", t, dl)
        # 7 量能
        vr = None
        if pd.notna(cur.get("vma5")) and float(cur["vma5"]) > 0:
            vr = float(cur["volume"]) / float(cur["vma5"])
        state, _ = volume_state(vr)
        chg = _chg(cur, prev)
        pattern = _pv_pattern(vr, chg)
        vol_delta = {"放量上涨": 10, "放量下跌": -10, "缩量上涨": -5,
                     "缩量下跌": 5, "放量滞涨": -5}.get(pattern, 0)
        add("量能", f"{state}（量比{_f(vr)}）· {pattern}", vol_delta)

        label, code = _verdict_label(score)
        goods = [p["dim"] for p in parts if p["delta"] > 0]
        bads = [p["dim"] for p in parts if p["delta"] < 0]
        if score >= 15:
            summary = f"{len(goods)} 项偏多因素占优（{'、'.join(goods)}）"
            if bads:
                summary += f"；需留意：{'、'.join(bads)}"
            summary += "。短线偏多，但仍以分批与止损纪律为前提。"
        elif score <= -15:
            summary = f"{len(bads)} 项偏空因素占优（{'、'.join(bads)}）"
            if goods:
                summary += f"；潜在支撑：{'、'.join(goods)}"
            summary += "。短线偏空，若持仓宜考虑减仓防守。"
        else:
            summary = f"多空因素接近平衡（偏多：{'、'.join(goods) or '无'}；偏空：{'、'.join(bads) or '无'}），建议观望或小仓位应对，等待方向明朗。"
        out.append({
            "date": date,
            "score": score,
            "verdict": label,
            "verdict_code": code,
            "parts": parts,
            "summary": summary,
            "volume": {"state": state, "vr": _f(vr, 2), "pattern": pattern,
                       "chg": _f(chg, 2)},
        })
    return out
