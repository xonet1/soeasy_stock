# -*- coding: utf-8 -*-
"""
synthesis.py — 多维综合分析（决策辅助，非荐股）

汇总六个维度 → 评分 + 共振/矛盾信号 + 风险预警：
  1. 技术面：MA/MACD/KDJ/RSI 等最新指标状态（本地行情）
  2. 舆情面：新闻/公告/研报情绪分（sentiment）
  3. 股东面：集中度变化、增减持、控制权（holder_reader）
  4. 关联面：相对基准强弱、Beta、联动性（relation_reader）
  5. 政策面：政策主题情绪与热度（policy_reader）
  6. 产业链：所处的产业链位置（chain_reader，若有知识库）

【定位】这是"决策辅助"工具：把多维数据整理成结构化信号，帮助用户自己判断，
不输出"买入/卖出"建议，不给目标价。所有维度标注数据来源，评分是规则化的
参考刻度而非预测。强制免责声明。
"""
import pandas as pd
import numpy as np

import indicators
import signals
import sentiment as sent_mod
import holder_reader
import relation_reader
import policy_reader
import chain_reader

UP_COLOR = "up"    # 红（A股涨）
DOWN_COLOR = "down"  # 绿


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _tech_score(df):
    """技术面评分：多指标投票，50 为中性。"""
    if df is None or len(df) < 30:
        return None, {"detail": "本地数据不足"}
    e = indicators.enrich(df.copy())
    last = e.iloc[-1]
    votes = []
    detail = {}
    # MA 多头排列
    if all(c in e.columns for c in ("ma5", "ma10", "ma20", "ma60")):
        ma_bull = last["ma5"] > last["ma10"] > last["ma20"]
        detail["ma_bull"] = bool(ma_bull)
        votes.append(1 if ma_bull else -1)
    # MACD 金叉/红柱：indicators 生成的是 dif/dea/macd(柱=2*(dif-dea))，
    # 不存在 macd_signal 列。金叉用 dif>dea 判定，等价于 macd 柱>0。
    if "dif" in e.columns and "dea" in e.columns:
        macd_bull = last["dif"] > last["dea"]
        detail["macd_bull"] = bool(macd_bull)
        votes.append(1 if macd_bull else -1)
    # KDJ 超买超卖
    if "k" in e.columns and "d" in e.columns:
        if last["k"] < 20:
            detail["kdj"] = "超卖"
            votes.append(1)
        elif last["k"] > 80:
            detail["kdj"] = "超买"
            votes.append(-1)
        else:
            detail["kdj"] = "中性"
    # RSI
    if "rsi6" in e.columns:
        if last["rsi6"] < 30:
            detail["rsi"] = "超卖"
            votes.append(1)
        elif last["rsi6"] > 70:
            detail["rsi"] = "超买"
            votes.append(-1)
        else:
            detail["rsi"] = "中性"
    # 价格相对 MA20
    if "ma20" in e.columns:
        above = last["close"] > last["ma20"]
        detail["above_ma20"] = bool(above)
        votes.append(1 if above else -1)
    if not votes:
        return None, detail
    net = sum(votes) / len(votes)
    score = round(_clamp(50 + net * 35), 1)
    detail["votes"] = votes
    detail["close"] = round(float(last["close"]), 2)
    return score, detail


def _sent_score(code, sentiment_data=None):
    """舆情面：从已有情绪结果取 overall_score，否则 None。"""
    if sentiment_data and isinstance(sentiment_data, dict):
        s = sentiment_data.get("overall_score")
        if s is not None:
            return float(s), {"label": sentiment_data.get("overall_label", "")}
    return None, {}


def _holder_score(deep=None, basic=None):
    """股东面：增减持家数 + 集中度变化 + 控制权。"""
    if not deep and not basic:
        return None, {}
    detail = {}
    score = 50
    # 两种数据源的 summary 层级不同：
    # fetch_deep 的 summary 直接是 {cr5, increase, decrease,...}；
    # fetch_holders 的 summary 是 {top:{...}, free_top:{...}}，增减持在 top 内。
    if deep:
        summary = deep.get("summary") or {}
    else:
        summary = (basic.get("summary") or {}).get("top", {}) if basic else {}
    inc, dec = summary.get("increase", 0), summary.get("decrease", 0)
    detail["increase"] = inc
    detail["decrease"] = dec
    score += (inc - dec) * 4
    if deep:
        series = (deep.get("trend_top") or {}).get("series", [])
        if len(series) >= 2 and series[0].get("cr5") is not None and series[-1].get("cr5") is not None:
            delta = series[0]["cr5"] - series[-1]["cr5"]  # 正=集中度上升
            detail["cr5_change"] = round(delta, 2)
            score += delta * 3  # 温和加分（集中度上升略偏多，但幅度小）
        ctl = deep.get("control", {})
        detail["control"] = ctl.get("control_level", "")
        if ctl.get("control_level") in ("绝对控股", "相对控股"):
            score += 3
    return round(_clamp(score), 1), detail


def _relation_score(rel=None):
    """关联面：相对基准超额收益 + Beta 合理性。"""
    if not rel or not rel.get("benchmark"):
        return None, {}
    b = rel["benchmark"]
    excess = b.get("excess_return")
    detail = {"excess_return": excess, "beta": b.get("beta"),
              "index": b.get("index_name", "")}
    if excess is None:
        return None, detail
    score = 50 + excess * 1.5  # 超额收益 ±20% → ±30 分
    return round(_clamp(score), 1), detail


def _policy_score(policy=None):
    """政策面：相关政策的情绪分。"""
    if not policy:
        return None, {}
    ps = policy.get("policy_sentiment", {})
    detail = {"score": ps.get("score"), "label": ps.get("label"),
              "related_count": ps.get("related_count", 0),
              "themes": [x["theme"] for x in policy.get("stock_themes", [])[:4]]}
    s = ps.get("score")
    return (float(s), detail) if s is not None else (None, detail)


def _chain_score(code):
    """产业链面：仅当有专属知识库时给位置描述，不强行打分。"""
    try:
        if chain_reader.is_fund(code):
            return None, {}
        # 轻量判断：是否有该股知识库条目
        kb = chain_reader._load_kb()
        stock_kb = kb.get("stocks", {}).get(code)
        if stock_kb:
            return 60, {"kb_level": "stock", "nodes": len(stock_kb.get("nodes", []))}
        return None, {"kb_level": "none"}
    except Exception:
        return None, {}


def synthesize(reader, code, technical=None, sentiment_data=None, deep=None,
               basic=None, relation=None, policy=None):
    """主入口：汇总各维度，产出决策辅助信号（非荐股）。"""
    code = str(code).zfill(6)
    # 技术面：若未传入则从本地数据算
    tech_score, tech_detail = (technical if technical is not None else (None, {}))
    if tech_score is None:
        df = reader.read_recent_cached(code, n=260)
        tech_score, tech_detail = _tech_score(df)

    dims = {}
    s, d = _sent_score(code, sentiment_data); dims["sentiment"] = {"score": s, "detail": d, "label": "舆情面"}
    s, d = _holder_score(deep, basic);        dims["holder"] = {"score": s, "detail": d, "label": "股东面"}
    s, d = _relation_score(relation);         dims["relation"] = {"score": s, "detail": d, "label": "关联面"}
    s, d = _policy_score(policy);             dims["policy"] = {"score": s, "detail": d, "label": "政策面"}
    s, d = _chain_score(code);                dims["chain"] = {"score": s, "detail": d, "label": "产业链"}
    dims["technical"] = {"score": tech_score, "detail": tech_detail, "label": "技术面"}

    # 综合评分（对有值的维度等权平均）
    scored = [(k, v["score"]) for k, v in dims.items() if v["score"] is not None]
    if scored:
        overall = round(sum(x[1] for x in scored) / len(scored), 1)
        overall_label = "多维偏多" if overall >= 62 else ("多维偏空" if overall <= 38 else "中性观望")
    else:
        overall, overall_label = None, "数据不足"

    # 共振/矛盾信号
    bullish = [k for k, v in dims.items() if v["score"] is not None and v["score"] >= 65]
    bearish = [k for k, v in dims.items() if v["score"] is not None and v["score"] <= 35]
    labels = {k: v["label"] for k, v in dims.items()}
    resonance = [labels[k] for k in bullish] if len(bullish) >= 2 else []
    conflict = []
    if bullish and bearish:
        conflict = [labels[k] for k in bullish] + ["vs"] + [labels[k] for k in bearish]

    notes = []
    if resonance:
        notes.append({"type": "resonance", "tone": "good",
                      "text": "多维共振偏多：" + "、".join(resonance) + " 同时给出偏多信号，但需结合价格位置判断。"})
    if conflict:
        notes.append({"type": "conflict", "tone": "warn",
                      "text": "维度分歧：" + "、".join(conflict) + "，信号不一致时谨慎。"})
    if not resonance and not conflict:
        notes.append({"type": "neutral", "tone": "info",
                      "text": "各维度信号中性或数据不足，暂无明确共振/矛盾。"})

    # 风险预警（基于真实数据）
    risks = []
    b = (relation or {}).get("benchmark", {})
    if b.get("beta") and b["beta"] > 1.5:
        risks.append(f"高Beta（{b['beta']}），波动放大，回撤风险高。")
    if b.get("max_drawdown") and b["max_drawdown"] < -20:
        risks.append(f"近{(relation or {}).get('window', 60)}日最大回撤 {b['max_drawdown']}%。")
    if tech_detail.get("kdj") == "超买" or tech_detail.get("rsi") == "超买":
        risks.append("短期指标超买，追高风险上升。")
    sm = (deep or {}).get("summary", {}).get("top", {})
    if sm.get("decrease", 0) >= 3:
        risks.append(f"本期{sm['decrease']}家前十大股东减持，关注筹码松动。")

    return {
        "code": code, "name": reader.name_of(code),
        "overall": overall, "overall_label": overall_label,
        "dimensions": dims,
        "resonance": resonance, "conflict": conflict,
        "notes": notes, "risks": risks,
        "as_of": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "disclaimer": ("综合评分为各维度规则化打分等权平均，是决策辅助参考而非预测或荐股。"
                       "不构成投资建议，不提供买入/卖出或目标价。请结合公司公告、自身风险承受能力独立判断。"
                       "市场有风险，投资需谨慎。"),
    }
