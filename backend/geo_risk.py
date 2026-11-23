# -*- coding: utf-8 -*-
"""
geo_risk.py — 国际政治形势 / 地缘冲突因子分析（规则映射，标注来源）

数据源：复用 policy_reader 的合规采集层（akshare stock_info_global_em 全球财经快讯，
东方财富公开资讯页，投资者无需登录），不额外增加网络请求。

方法（纯财经视角，不做政治立场判断）：
  1. 采集全球快讯，用「地缘事件词典」归类：军事冲突 / 制裁与关税 / 能源供应 /
     外交会晤 / 国际金融与汇率 / 科技管制 等，并给事件严重度。
  2. 每类事件映射到「受影响行业」：利好侧 / 利空侧（如油价上行利好油气开采、利空航空化工）。
  3. 结合个股所属行业 + 主营业务，判断该股对每类事件的敏感度与方向。
  4. 输出：全市场事件流 + 该股相关的地缘风险/机会因子，供综合分析打分。

【重要】所有影响映射均为关键词规则，不是语义理解，也不是对政治事件的立场判断；
行业影响方向是常见传导路径的推断，实际影响取决于公司业务结构与事件进展，
须以官方信息与公司公告为准。仅供个人研究，非投资建议。
"""
import os
import json
import time
import threading

import policy_reader as pr

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "geo_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
_GEO_PATH = os.path.join(CACHE_DIR, "geo_events.json")
TTL = 3 * 3600          # 事件流 3 小时缓存（比政策快讯短，地缘事件时效性强）
_LOCK = threading.RLock()

# 事件类型 -> (识别关键词, 严重度权重, 利好行业, 利空行业)
# 严重度仅用于排序/热度，不代表对事件的政治或价值判断
EVENTS = {
    "军事冲突": {
        "kw": ("战争", "战事", "冲突", "空袭", "导弹", "交火", "军事行动", "交战", "炮击", "开战",
               "无人机袭击", "武装", "停火", "撤军", "军事演习", "海上封锁", "局势升级",
               "轰炸", "军事打击", "边境冲突"),
        "severity": 3,
        "benefit": ("军工", "国防", "航天", "航空", "船舶", "石油", "油气", "黄金", "有色", "贵金属"),
        "harm": ("航空", "机场", "旅游", "航运", "化工", "交通运输", "消费"),
    },
    "制裁与关税": {
        "kw": ("制裁", "关税", "加征", "贸易战", "贸易摩擦", "出口管制", "禁运", "实体清单",
               "限制出口", "反制", "贸易壁垒", "进口限制", "反倾销"),
        "severity": 3,
        "benefit": ("半导体", "芯片", "国产替代", "稀土", "军工", "信创", "自主可控"),
        "harm": ("出口", "外贸", "纺织", "家电", "电子", "机械", "汽车", "光伏"),
    },
    "能源供应": {
        "kw": ("油价", "原油价格", "天然气价格", "限电", "断供", "减产", "能源危机", "供应中断",
               "OPEC", "能源价格", "气价", "电力短缺", "石油供应", "天然气供应", "能源短缺"),
        "severity": 2,
        "benefit": ("石油", "油气", "煤炭", "能源", "油服", "新能源", "储能"),
        "harm": ("航空", "化工", "化纤", "交通运输", "物流", "电力"),
    },
    "外交会晤": {
        "kw": ("会晤", "会谈", "访问", "峰会", "外交", "谈判", "协议", "建交", "通话", "磋商", "会面"),
        "severity": 1,
        "benefit": ("外贸", "出口", "跨境", "旅游", "航空", "港口", "一带一路"),
        "harm": (),
    },
    "国际金融与汇率": {
        "kw": ("美联储", "加息", "降息", "美元", "汇率", "人民币", "国债收益率", "美股", "欧央行",
               "通胀", "金融危机", "债市", "流动性", "资本流出"),
        "severity": 2,
        "benefit": ("黄金", "贵金属", "银行", "证券", "保险"),
        "harm": ("房地产", "成长股", "科技", "新能源", "高估值"),
    },
    "科技管制": {
        "kw": ("芯片法案", "出口管制", "实体清单", "技术封锁", "断供", "禁售", "AI管制",
               "半导体限制", "光刻机", "EDA", "算力限制"),
        "severity": 3,
        "benefit": ("半导体", "芯片", "国产替代", "信创", "自主可控", "算力", "AI"),
        "harm": ("电子", "消费电子", "通信", "半导体设备"),
    },
}

# 风险情绪词（影响打分方向，中性财经语境）
_RISK_UP = ("升级", "加剧", "恶化", "紧张", "中断", "暴跌", "大涨", "制裁", "封锁", "断供", "冲突", "袭击", "警告", "风险")
_RISK_DOWN = ("缓和", "降温", "停火", "达成协议", "谈判", "恢复", "和解", "回落", "稳定", "利好", "会谈")


def _s(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s[:400]


def _cache_read():
    try:
        with open(_GEO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_write(payload):
    try:
        with open(_GEO_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_events(refresh=False):
    """采集并分类全球地缘事件快讯（复用政策模块合规采集层）。"""
    cached = _cache_read()
    if cached and not refresh and time.time() - cached.get("fetched_at", 0) < TTL:
        return cached
    with _LOCK:
        cached = _cache_read()
        if cached and not refresh and time.time() - cached.get("fetched_at", 0) < TTL:
            return cached
        try:
            payload = pr._fetch_news("global", refresh=refresh)
        except Exception as e:
            if cached.get("data") is not None:
                return {**cached, "stale": True, "error": str(e)[:150]}
            return {"fetched_at": 0, "stale": False, "data": [], "error": str(e)[:150]}
        items = []
        for raw in (payload.get("data") or []):
            text = _s(raw.get("title")) + " " + _s(raw.get("summary"))
            hits = []
            for etype, cfg in EVENTS.items():
                matched = [k for k in cfg["kw"] if k in text]
                if matched:
                    sev = cfg["severity"] + min(2, len(matched) // 2)
                    risk = sum(1 for k in _RISK_UP if k in text) - sum(1 for k in _RISK_DOWN if k in text)
                    hits.append({
                        "type": etype, "keywords": matched[:5],
                        "severity": min(5, sev),
                        "direction": "偏空" if risk > 0 else ("偏多" if risk < 0 else "中性"),
                        "benefit": list(cfg["benefit"]), "harm": list(cfg["harm"]),
                    })
            if not hits:
                continue
            hits.sort(key=lambda x: -x["severity"])
            items.append({
                "title": _s(raw.get("title")), "summary": _s(raw.get("summary"))[:200],
                "time": _s(raw.get("time"))[:19], "url": _s(raw.get("url")),
                "events": hits,
                "severity": hits[0]["severity"],
            })
        items.sort(key=lambda x: (x.get("time") or ""), reverse=True)
        out = {"fetched_at": time.time(), "stale": bool(payload.get("stale")),
               "source": "东方财富全球财经快讯（公开）", "data": items[:120]}
        _cache_write(out)
        return out


def _company_profile(code):
    return pr._company_profile(None, code)


def analyze_geo(reader, code, refresh=False):
    """个股地缘因子：事件流 + 该股行业敏感度 + 方向判断。"""
    code = str(code).zfill(6)
    feed = fetch_events(refresh=refresh)
    events = feed.get("data") or []
    _, industry, profile_text = _company_profile(code)
    text = (industry + " " + profile_text)

    # 个股对各类事件的敏感度：主营/行业文本命中该事件利好或利空行业的词
    sensitivity = {}
    for etype, cfg in EVENTS.items():
        ben = [k for k in cfg["benefit"] if k in text]
        harm = [k for k in cfg["harm"] if k in text]
        if ben or harm:
            # 方向：利空侧命中更强时视为承压；否则视为潜在受益
            score = len(ben) * 2 - len(harm) * 2 + len(ben) - len(harm)
            sensitivity[etype] = {
                "benefit_hits": ben[:6], "harm_hits": harm[:6],
                "stance": "潜在受益" if score > 0 else ("承压" if score < 0 else "中性"),
                "weight": min(5, len(ben) + len(harm)),
            }

    # 与该股相关的事件（事件类型在敏感度表中）
    related = []
    for ev in events:
        rel = [h for h in ev.get("events", []) if h["type"] in sensitivity]
        if not rel:
            continue
        # 事件方向与个股站点的合成：事件偏空且个股承压 → 双重承压
        stance = []
        for h in rel:
            st = sensitivity[h["type"]]["stance"]
            if h["direction"] == "偏空" and st == "承压":
                stance.append("承压")
            elif h["direction"] == "偏多" and st == "潜在受益":
                stance.append("受益")
            else:
                stance.append("观察")
        related.append({
            "title": ev["title"], "time": ev["time"], "url": ev["url"],
            "severity": ev["severity"], "events": rel,
            "impact": "承压" if "承压" in stance else ("受益" if "受益" in stance else "观察"),
        })
    related.sort(key=lambda x: (-x["severity"], x["time"]), reverse=False)

    # 因子分：0-100。无相关事件时不强行打分（返回 None，综合引擎跳过）
    factor = None
    if related:
        # 承压 -、受益 +，按严重度加权；基线 50
        delta = 0
        for r in related[:20]:
            w = r["severity"]
            delta += (-6 if r["impact"] == "承压" else (5 if r["impact"] == "受益" else 0)) * w / 3
        factor = max(0, min(100, round(50 + delta, 1)))

    # 全市场事件统计（按类型）
    by_type = {}
    for ev in events:
        for h in ev.get("events", []):
            b = by_type.setdefault(h["type"], {"count": 0, "max_severity": 0})
            b["count"] += 1
            b["max_severity"] = max(b["max_severity"], h["severity"])

    return {
        "ok": True, "code": code, "industry": industry,
        "source": feed.get("source", "东方财富全球财经快讯"),
        "fetched_at": feed.get("fetched_at", 0),
        "stale": bool(feed.get("stale")),
        "event_count": len(events),
        "factor": factor,
        "sensitivity": sensitivity,
        "related": related[:20],
        "by_type": by_type,
        "top_events": events[:15],
        "note": "地缘事件影响为关键词规则映射，仅作财经视角的风险/机会线索，"
                "不代表政治立场，也不预测事件走向；须以官方信息与公司公告为准，非投资建议。",
    }


def status():
    feed = _cache_read()
    return {
        "cached": len(feed.get("data") or []),
        "fetched_at": feed.get("fetched_at", 0),
        "stale": bool(feed.get("stale")),
        "event_types": list(EVENTS.keys()),
    }
