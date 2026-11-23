# -*- coding: utf-8 -*-
"""
predict_reader.py — 下一交易日买卖概率评级（统计参考，非投资建议）

方法（双源融合，全部基于公开历史行情，可复现）：
  1. 历史条件统计（kNN 式相似日样本）：
     以最近收盘日的 6 个可观测条件为特征——趋势(MA20斜率三档)、位置(close vs MA20)、
     量能状态(量比五档)、MACD 动能(DIF/DEA 多空 × 柱体增减)、RSI 区间四档、当日涨跌五档，
     在该股自身全部日线历史中寻找相似交易日（≥5/6 维匹配，样本不足自动放宽到 ≥4/6），
     按近期加权（半衰期 500 交易日）统计这些"相似日"的次日表现：
     上涨占比 → 经验上涨概率 P1；同时给出平均/中位涨跌、最好最差、样本数。
  2. 研判评分映射：将现有六维每日研判分（-100~+100）经 Logistic 映射为概率 P2。
  3. 融合：P = 0.65×P1 + 0.35×P2；评级 A~E：
     A 强偏多(≥62%) / B 偏多(55~62) / C 中性(45~55) / D 偏空(38~45) / E 强偏空(<38)。
诚实边界：市场非平稳，历史条件概率不代表未来；样本 <30 自动放宽维度并在结果中标注低置信；
  突发消息、大盘系统性风险不在模型输入内。输出为描述性统计参考。
"""
import math

from tdx_reader import TdxData
from indicators import enrich
from volume_analysis import daily_verdicts

HALF_LIFE = 500       # 加权半衰期（交易日）
MIN_SAMPLES = 30      # 最小相似样本
MAX_BARS = 1200       # 历史窗口


def _bucket(v, edges):
    for i, e in enumerate(edges):
        if v < e:
            return i
    return len(edges)


def _feat_trend(slope):
    if slope > 0.2:
        return "上"
    if slope < -0.2:
        return "下"
    return "平"


def _feat_macd(dif, dea, hist, hist_prev):
    side = "多" if dif > dea else "空"
    if hist is None or hist_prev is None or hist != hist or hist_prev != hist_prev:
        mag = "?"
    elif abs(hist) > abs(hist_prev):
        mag = "扩"
    else:
        mag = "敛"
    return side + mag


def _bar_features(d, i):
    """第 i 行（收盘后）的可观测条件。返回 dict 或 None（指标未就绪）。"""
    row = d.iloc[i]
    ma20 = row.get("ma20")
    ma20_p = d["ma20"].iloc[i - 5] if i >= 5 else None
    if ma20 is None or ma20 != ma20 or ma20_p is None or ma20_p != ma20_p:
        return None
    vma5 = row.get("vma5")
    if vma5 is None or vma5 != vma5 or vma5 <= 0:
        return None
    vr = float(row["volume"]) / vma5
    chg = (float(row["close"]) / float(d["close"].iloc[i - 1]) - 1) * 100 if i >= 1 else 0
    rsi = row.get("rsi6")
    rsi = float(rsi) if rsi is not None and rsi == rsi else 50
    dif, dea = row.get("dif"), row.get("dea")
    hist, hist_p = row.get("macd"), d["macd"].iloc[i - 1] if i >= 1 else None
    return {
        "trend": _feat_trend((ma20 / ma20_p - 1) * 100),
        "pos": "上" if float(row["close"]) > ma20 else "下",
        "vol": _bucket(vr, (0.6, 0.9, 1.3, 1.8)),          # 0~4
        "macd": _feat_macd(dif, dea, hist, hist_p),        # 多扩/多敛/空扩/空敛/? 5类
        "rsi": _bucket(rsi, (30, 50, 70)),                 # 0~3
        "chg": _bucket(chg, (-3, 0, 1.5, 3)),              # 0~4
    }


def _match_count(a, b):
    return sum(1 for k in a if a[k] == b[k])


def predict_next_day(code, bars=MAX_BARS):
    r = TdxData()
    df = r.read_recent_cached(code, n=bars)
    if df is None or len(df) < 120:
        return {"ok": False, "error": "历史数据不足（<120 个交易日），无法做条件统计"}
    d = enrich(df)
    n = len(d)
    cur = _bar_features(d, n - 1)
    if cur is None:
        return {"ok": False, "error": "最新交易日指标未就绪（停牌过久或数据异常）"}

    # --- 1. 历史相似日统计（kNN）---
    for level, min_m in (("6/6 全维匹配", 6), ("≥5/6 匹配", 5), ("≥4/6 匹配（放宽）", 4)):
        samples = []
        for i in range(60, n - 1):
            f = _bar_features(d, i)
            if f is None:
                continue
            if _match_count(cur, f) >= min_m:
                nxt = (float(d["close"].iloc[i + 1]) / float(d["close"].iloc[i]) - 1) * 100
                # 三日累计（观察延续性）
                fwd3 = ((float(d["close"].iloc[min(i + 3, n - 1)]) / float(d["close"].iloc[i]) - 1) * 100)
                age = n - 1 - i
                w = 0.5 ** (age / HALF_LIFE)
                samples.append((nxt, fwd3, w))
        if len(samples) >= MIN_SAMPLES:
            used_level = level
            break
    else:
        used_level = None
        samples = []

    stats = None
    p1 = None
    if samples:
        tw = sum(s[2] for s in samples)
        up_w = sum(s[2] for s in samples if s[0] > 0)
        p1 = up_w / tw
        rets = sorted(s[0] for s in samples)
        k = len(rets)
        stats = {
            "n": k,
            "win_rate": round(sum(1 for s in samples if s[0] > 0) / k * 100, 1),
            "avg": round(sum(s[0] for s in samples) / k, 2),
            "median": round(rets[k // 2], 2),
            "best": round(rets[-1], 2),
            "worst": round(rets[0], 2),
            "avg3": round(sum(s[1] for s in samples) / k, 2),
        }

    # --- 2. 研判评分 → Logistic 概率 ---
    verdicts = daily_verdicts(d, 1)
    score = verdicts[-1]["score"] if verdicts else 0
    p2 = 1 / (1 + math.exp(-score / 40))

    # --- 3. 融合 ---
    if p1 is not None:
        p = 0.65 * p1 + 0.35 * p2
        basis = "经验统计 + 研判评分"
    else:
        p = p2
        basis = "仅研判评分（相似样本不足）"
    p = min(max(p, 0.02), 0.98)
    pct = round(p * 100, 1)

    if p >= 0.62:
        grade, label = "A", "强偏多"
    elif p >= 0.55:
        grade, label = "B", "偏多"
    elif p >= 0.45:
        grade, label = "C", "中性"
    elif p >= 0.38:
        grade, label = "D", "偏空"
    else:
        grade, label = "E", "强偏空"

    stars = max(1, min(5, round(p * 5)))  # 买入侧星级
    low_conf = (stats is None or stats["n"] < 60 or used_level and "放宽" in used_level)

    return {
        "ok": True,
        "code": code,
        "as_of": str(d["date"].iloc[-1])[:10],
        "p_up": pct,
        "p_down": round(100 - pct, 1),
        "grade": grade,
        "label": label,
        "stars_buy": stars,
        "basis": basis,
        "conditions": cur,
        "match_level": used_level,
        "stats": stats,
        "verdict_score": score,
        "p_empirical": round(p1 * 100, 1) if p1 is not None else None,
        "p_verdict": round(p2 * 100, 1),
        "low_confidence": low_conf,
        "history_bars": n,
        "note": "概率来自该股自身历史相似条件的次日统计与六维研判评分的加权融合，"
                "描述性参考非预测保证；突发消息与大盘系统性风险不在输入内，市场非平稳时失效。",
    }
