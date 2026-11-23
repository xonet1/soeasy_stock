# -*- coding: utf-8 -*-
"""
sentiment.py — 舆情情绪打分（轻量财经词典规则，透明可解释）

说明：
  - 这是基于关键词词典的启发式打分，不是 NLP 模型，结果仅作快速参考
  - 标题词权重高于正文；研报评级作为独立强信号
  - 每条得分映射到 0~100：>60 偏多，40~60 中性，<40 偏空
"""
import math
import re
from collections import defaultdict

# 正面词：强信号权重 2，普通 1
POS_STRONG = [
    "涨停", "大涨", "暴涨", "利好", "超预期", "突破", "创新高", "新高", "增持",
    "回购", "中标", "扭亏", "翻倍", "倍增", "业绩预增", "业绩大增", "业绩飙升",
    "强烈推荐", "上调评级", "上调目标价", "获批", "重大合同", "订单", "签约",
    "扩产", "景气", "高分红", "分红", "收购", "重组", "注入", "减持完成",
]
POS_NORMAL = [
    "上涨", "走强", "回暖", "增长", "改善", "优化", "提升", "复苏", "反弹",
    "看好", "机会", "稳健", "盈利", "受益", "扩张", "量产", "放量",
    "盈利预测上调", "买入", "领先", "有望", "积极", "支撑",
]
# 负面词：强信号权重 2，普通 1
NEG_STRONG = [
    "跌停", "大跌", "暴跌", "利空", "暴雷", "巨亏", "亏损", "业绩预亏", "业绩下滑",
    "业绩大降", "减持", "卖出", "退市", "处罚", "立案", "调查", "诉讼", "违规",
    "警示", "商誉减值", "下调评级", "下调目标价", "质押", "冻结", "破产", "清盘",
    "终止", "取消", "失败", "风险提示", "重大风险", "亏损扩大",
]
NEG_NORMAL = [
    "下跌", "走弱", "承压", "回落", "放缓", "下降", "萎缩", "担忧", "压力",
    "谨慎", "疲软", "下滑", "减值", "减持计划", "解禁", "套现", "问询", "监管",
    "抛售", "流出", "低预期", "不及预期", "弱势", "衰退",
]
# 去重说明：从 POS_NORMAL/NEG_NORMAL 移除了与 STRONG 重复的"增持/卖出"，
# 以及 POS_NORMAL 内重复的"增长/盈利"，避免同一词被多档权重重复计分。

# 研报评级 → 分值（0~100）
RATING_SCORE = {
    "买入": 85, "强烈推荐": 90, "增持": 70, "推荐": 72, "审慎推荐": 60,
    "中性": 50, "持有": 50, "观望": 45, "减持": 30, "卖出": 15,
}

CLEAN_RE = re.compile(r"<[^>]+>|&nbsp;|\s+")


def _count_terms(text, terms, weight):
    """统计词典命中加权次数。"""
    if not text:
        return 0.0
    return float(sum(text.count(t) * weight for t in terms))


def _score_item(title="", content="", rating="", category="news"):
    """对单条文本打分，返回 (score 0~100, 命中详情)。"""
    title = CLEAN_RE.sub(" ", str(title or ""))
    content = CLEAN_RE.sub(" ", str(content or ""))

    # 标题权重 2，正文权重 1；强信号词权重再翻倍
    pos = (_count_terms(title, POS_STRONG, 4) + _count_terms(title, POS_NORMAL, 2)
           + _count_terms(content, POS_STRONG, 2) + _count_terms(content, POS_NORMAL, 1))
    neg = (_count_terms(title, NEG_STRONG, 4) + _count_terms(title, NEG_NORMAL, 2)
           + _count_terms(content, NEG_STRONG, 2) + _count_terms(content, NEG_NORMAL, 1))

    if category == "research" and rating:
        rs = RATING_SCORE.get(rating.strip())
        if rs is not None:
            # 评级作为一条强信号参与（等价于约 ±2 个普通词命中）
            delta = (rs - 50) / 50 * 3
            if delta > 0:
                pos += delta
            elif delta < 0:
                neg += -delta

    raw = (pos - neg) / (pos + neg + 1.0)   # [-1, 1]
    score = 50 + 50 * math.tanh(raw * 1.5)  # 平滑到 (0, 100)
    return round(score, 1), {"pos": round(pos, 1), "neg": round(neg, 1)}


def _label(score):
    if score >= 60:
        return "positive"
    if score <= 40:
        return "negative"
    return "neutral"


def _date_of(item, category):
    if category == "news":
        return str(item.get("time", ""))[:10]
    return str(item.get("date", ""))[:10]


def analyze(payload):
    """输入 {news:{items,stale,...}, notice:{...}, research:{...}}
    输出情绪分析结果，所有数值 JSON 友好。
    """
    catalog = [
        ("news", "新闻", "title", "content"),
        ("notice", "公告", "title", None),
        ("research", "研报", "title", None),
    ]
    scored = []
    by_source = {}
    day_scores = defaultdict(list)

    for kind, label, title_key, content_key in catalog:
        block = payload.get(kind) or {}
        items = block.get("items") or []
        scores = []
        source_counts = {"positive": 0, "neutral": 0, "negative": 0}
        for it in items:
            sc, hits = _score_item(
                title=it.get(title_key, ""),
                content=(it.get(content_key, "") if content_key else ""),
                rating=it.get("rating", "") if kind == "research" else "",
                category=kind,
            )
            lab = _label(sc)
            source_counts[lab] += 1
            scores.append(sc)
            day = _date_of(it, kind)
            if re.match(r"\d{4}-\d{2}-\d{2}", day):
                day_scores[day].append(sc)
            row = {
                "source": label, "source_key": kind,
                "title": it.get("title", ""),
                "date": _date_of(it, kind) or it.get("time", ""),
                "url": it.get("url", ""),
                "rating": it.get("rating", "") if kind == "research" else "",
                "org": it.get("org", "") if kind == "research" else it.get("source", ""),
                "score": sc, "label": lab, "hits": hits,
            }
            scored.append(row)
        by_source[kind] = {
            "label": label, "count": len(items),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "counts": source_counts,
            "stale": bool(block.get("stale")),
            "fetched_at": block.get("fetched_at", 0),
            "error": block.get("error"),
        }

    # 全量逐条结果（按日期倒序），供前端明细列表渲染
    full_items = sorted(scored, key=lambda r: r.get("date", ""), reverse=True)

    if not scored:
        return {
            "overall_score": None, "overall_label": "neutral",
            "counts": {"positive": 0, "neutral": 0, "negative": 0},
            "by_source": by_source, "trend": [], "full_items": [],
            "top_positive": [], "top_negative": [], "total": 0,
        }

    all_scores = [r["score"] for r in scored]
    overall = round(sum(all_scores) / len(all_scores), 1)
    counts = {
        "positive": sum(1 for r in scored if r["label"] == "positive"),
        "neutral": sum(1 for r in scored if r["label"] == "neutral"),
        "negative": sum(1 for r in scored if r["label"] == "negative"),
    }

    # 按日期聚合趋势（最近 30 个有数据的日期）
    trend = []
    for day in sorted(day_scores.keys())[-30:]:
        vals = day_scores[day]
        trend.append({
            "date": day,
            "score": round(sum(vals) / len(vals), 1),
            "count": len(vals),
        })

    desc = sorted(scored, key=lambda r: r["score"], reverse=True)
    top_pos = [r for r in desc if r["label"] == "positive"][:5] or desc[:3]
    top_neg = [r for r in desc[::-1] if r["label"] == "negative"][:5] or desc[::-1][:3]

    return {
        "overall_score": overall,
        "overall_label": _label(overall),
        "counts": counts,
        "by_source": by_source,
        "trend": trend,
        "full_items": full_items,
        "top_positive": top_pos,
        "top_negative": top_neg,
        "total": len(scored),
    }
