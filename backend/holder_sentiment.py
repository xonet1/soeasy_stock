# -*- coding: utf-8 -*-
"""
holder_sentiment.py — 股东变动事件 × 舆情联动解读

背景：股东数据只告诉我们"谁新进/增持"，舆情只做全市场情绪打分，两者割裂。
本模块把「重点股东变动事件」与个股新闻/公告/研报做关键词关联，输出：
  - 每个事件命中的相关报道（含来源、日期、情绪分）
  - 媒体报道倾向（正面/中性/负面/未见直接报道）
  - 结合股东性质的可解释解读（明确为规则推断，非公司披露动机）

所有结论均标注来源与不确定性，不构成投资建议。
"""
import re

try:
    import sentiment as _sent
except Exception:
    _sent = None


# 股东变动类报道的泛化信号词（无股东名称命中时作为"间接相关"线索）
_CONTEXT_KWS = (
    "前十大股东", "十大股东", "机构风向标", "持仓", "持股", "增持", "减持",
    "新进", "股东", "机构调研", "举牌", "入驻",
)


def _text_of(item, kind):
    parts = [str(item.get("title") or "")]
    if kind == "news":
        parts.append(str(item.get("content") or "")[:300])
    return " ".join(parts)


def _keywords(event, stock_name=""):
    """事件的匹配关键词：核心字号优先，全称兜底。

    排除与标的公司名重合的字号（如股东"格力" vs 股票"格力电器"），
    否则该股票的全部新闻都会被误判为"股东相关报道"。
    """
    kws = []
    short = str(event.get("short") or "").strip()
    name = str(event.get("name") or "").strip()
    stock = str(stock_name or "").strip()
    if short and len(short) >= 2 and not (stock and (short in stock or stock in short)):
        kws.append(short)
    if name and name not in kws:
        kws.append(name)
    return [k for k in kws if k]


def _score(title, kind):
    if _sent is None:
        return None, "neutral"
    try:
        sc, _hits = _sent._score_item(title=title, category=kind)
        return sc, _sent._label(sc)
    except Exception:
        return None, "neutral"


def link_events(events, payload, stock_name=""):
    """把股东事件与舆情 payload 关联。

    payload: {news:{items}, notice:{items}, research:{items}}（news_reader 原样输出）
    返回 {events:[...], coverage, note}
    """
    events = events or []
    corpus = []
    for kind in ("news", "notice", "research"):
        block = (payload or {}).get(kind) or {}
        for it in (block.get("items") or []):
            title = str(it.get("title") or "")
            date = str(it.get("time") or it.get("date") or "")[:10]
            corpus.append({
                "kind": kind,
                "source": {"news": "新闻", "notice": "公告", "research": "研报"}.get(kind, kind),
                "title": title,
                "date": date,
                "url": str(it.get("url") or ""),
                "text": _text_of(it, kind),
            })

    out = []
    for ev in events:
        kws = _keywords(ev, stock_name)
        direct, context = [], []
        for c in corpus:
            title_hit = [k for k in kws if k and k in c["title"]]
            body_hit = [k for k in kws if k and k in c["text"]]
            if title_hit:
                # 标题直接点名股东 → 直接相关
                sc, lab = _score(c["title"], c["kind"])
                direct.append({**{k: v for k, v in c.items() if k != "text"},
                               "matched": title_hit, "relation": "标题点名",
                               "score": sc, "label": lab})
            elif body_hit:
                # 仅正文顺带提及（可能只是列举）→ 间接相关
                context.append({**{k: v for k, v in c.items() if k != "text"},
                                "matched": body_hit, "relation": "正文提及"})
            elif any(kw in c["text"] for kw in _CONTEXT_KWS):
                context.append({**{k: v for k, v in c.items() if k != "text"},
                                "relation": "股东/持仓背景"})
        direct.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        scores = [x["score"] for x in direct if x.get("score") is not None]
        avg = round(sum(scores) / len(scores), 1) if scores else None
        if not direct:
            tone = "none"
            body_hits = sum(1 for x in context if x.get("relation") == "正文提及")
            summary = (f"舆情标题中未见直接以{ev.get('short') or ev.get('name')}为主题的报道"
                       + (f"（{body_hits} 条正文顺带提及）" if body_hits else "")
                       + (f"；另有 {len(context) - body_hits} 条股东/持仓相关资讯可作背景参考"
                          if len(context) > body_hits else "")
                       + "。股东动机需以公司公告及股东自身披露为准。")
        elif avg is not None and avg >= 60:
            tone, summary = "positive", f"相关报道 {len(direct)} 条，媒体情绪偏正面（均分 {avg}）。"
        elif avg is not None and avg <= 40:
            tone, summary = "negative", f"相关报道 {len(direct)} 条，媒体情绪偏负面（均分 {avg}）。"
        else:
            tone, summary = "neutral", f"相关报道 {len(direct)} 条，媒体情绪中性（均分 {avg}）。"
        item = dict(ev)
        item.update({
            "related_items": direct[:6],
            "related_count": len(direct),
            "context_items": context[:3],
            "context_count": len(context),
            "media_tone": tone,
            "media_score": avg,
            "media_summary": summary,
            "combined_reading": (ev.get("reading") or "") + " " + summary,
        })
        out.append(item)

    direct_total = sum(1 for e in out if e["related_count"] > 0)
    return {
        "events": out,
        "coverage": {
            "event_count": len(out),
            "with_direct_coverage": direct_total,
            "without_direct_coverage": len(out) - direct_total,
        },
        "note": ("关联依据为股东名称/核心字号在新闻标题（直接相关）与正文、公告研报标题（间接相关）"
                 "中的字面匹配，已排除与标的公司名重合的字号；可能漏报未含股东名称的报道；"
                 "情绪为词典规则打分。股东入股动机属推断，非公司披露事实，不构成投资建议。"),
    }
