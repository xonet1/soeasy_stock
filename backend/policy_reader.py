# -*- coding: utf-8 -*-
"""
policy_reader.py — 政策与宏观资讯分析（规则映射，标注来源）

数据源（东方财富公开资讯接口，投资者无需登录）：
  - stock_info_cjzc_em    财经政策/要闻快讯
  - stock_info_global_em  全球财经资讯（含海外政策）

方法：
  1. 采集快讯，按"政策主题词典"归类（半导体/AI/新能源/医药/地产/消费/军工等）
  2. 政策情绪打分（支持/限制/中性词典），识别"增量"信号词（首次/上调/补贴/强制）
  3. 结合个股所属行业+主营业务+主营构成，判断主题相关度（关键词重合）
  4. 输出与该股最相关的政策主题、代表快讯、主题热度与情绪

【重要】主题映射为关键词规则，非语义理解，存在误匹配；政策影响需以官方原文
与公司公告为准。所有映射项标注 source=规则映射·需核实。仅供个人研究，非投资建议。
"""
import os
import json
import time
import threading
import datetime as dt
import pandas as pd

import holder_reader as hr  # 复用合规采集层（限速/退避/审计）

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "policy_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
TTL = 6 * 3600
_LOCK = threading.RLock()

# 政策主题词典：主题 -> 关键词（政策文本侧）
THEMES = {
    "半导体/芯片": ("半导体", "芯片", "集成电路", "晶圆", "光刻", "存储", "DRAM", "先进制程", "算力", "出口管制", "实体清单"),
    "人工智能": ("人工智能", "AI", "大模型", "算力", "数据中心", "智能算力", "AIGC", "生成式"),
    "新能源/光伏": ("新能源", "光伏", "储能", "锂电", "风电", "碳中和", "双碳", "电力设备"),
    "新能源汽车": ("新能源汽车", "汽车", "智能驾驶", "自动驾驶", "充电桩", "以旧换新"),
    "医药医疗": ("医药", "医疗", "创新药", "集采", "生物医药", "医保", "疫苗", "医疗器械"),
    "房地产": ("房地产", "地产", "楼市", "住建", "房贷", "限购", "保障房"),
    "消费": ("消费", "零售", "以旧换新", "免税", "餐饮", "家电", "促消费"),
    "军工国防": ("军工", "国防", "航天", "航空", "导弹", "军贸"),
    "金融科技": ("金融", "证券", "银行", "保险", "资本市场", "注册制", "降准", "降息", "量化"),
    "国企改革": ("国企改革", "央企", "国资", "混合所有制", "市值管理"),
    "机器人": ("机器人", "人形机器人", "减速器", "伺服", "智能制造"),
    "低空经济": ("低空", "无人机", "eVTOL", "通航"),
}

# 政策情绪词典
POSITIVE_KW = ("支持", "鼓励", "扶持", "补贴", "减税", "降准", "降息", "放宽", "准入", "试点",
               "上调", "增长", "突破", "加快", "推动", "促进", "利好", "专项资金", "采购", "开放")
NEGATIVE_KW = ("限制", "管制", "禁止", "收紧", "调查", "处罚", "下调", "加征", "关税", "制裁",
               "实体清单", "出口管制", "风险", "规范", "整顿", "退市", "减持")

# 增量信号词（政策"增量"价值，见 policy-impact 方法论）
INCREMENT_KW = ("首次", "新规", "上调", "扩大", "加码", "强制", "专项", "落地", "实施细则", "发布会")


def _s(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s[:400]


def _sentiment(text):
    p = sum(1 for k in POSITIVE_KW if k in text)
    n = sum(1 for k in NEGATIVE_KW if k in text)
    inc = [k for k in INCREMENT_KW if k in text]
    if p > n:
        label = "偏多"
    elif n > p:
        label = "偏空"
    else:
        label = "中性"
    score = 50 + (p - n) * 8 + len(inc) * 3
    score = max(0, min(100, score))
    return {"label": label, "score": score, "increment": inc[:5]}


def _match_themes(text):
    hits = []
    for theme, kws in THEMES.items():
        matched = [k for k in kws if k in text]
        if matched:
            hits.append((theme, matched))
    return hits


def _cache_path(kind):
    return os.path.join(CACHE_DIR, f"{kind}.json")


def _read_cache(kind):
    try:
        with open(_cache_path(kind), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(kind, payload):
    tmp = _cache_path(kind) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(kind))
    except Exception:
        pass


def _fetch_news(kind, refresh=False):
    """合规采集政策/全球快讯。"""
    cached = _read_cache(kind)
    if cached and not refresh and time.time() - cached.get("fetched_at", 0) < TTL:
        return cached
    with _LOCK:
        cached = _read_cache(kind)
        if cached and not refresh and time.time() - cached.get("fetched_at", 0) < TTL:
            return cached
        ak = hr._ak()
        endpoint = "stock_info_cjzc_em" if kind == "policy" else "stock_info_global_em"
        fn = ak.stock_info_cjzc_em if kind == "policy" else ak.stock_info_global_em
        try:
            df = hr._compliant_call(fn, endpoint, kind)
            items = []
            for _, r in df.head(120).iterrows():
                title = _s(r.get("标题"))
                summary = _s(r.get("摘要"))
                text = title + " " + summary
                if not text.strip():
                    continue
                sent = _sentiment(text)
                themes = _match_themes(text)
                items.append({
                    "title": title,
                    "summary": summary[:200],
                    "time": _s(r.get("发布时间"))[:19],
                    "url": _s(r.get("链接")),
                    "sentiment": sent["label"],
                    "score": sent["score"],
                    "increment": sent["increment"],
                    "themes": [t for t, _ in themes],
                })
            payload = {"fetched_at": time.time(), "stale": False, "data": items}
            _write_cache(kind, payload)
            return payload
        except Exception as e:
            if cached and cached.get("data") is not None:
                stale = dict(cached)
                stale["stale"] = True
                stale["error"] = str(e)[:150]
                return stale
            return {"fetched_at": 0, "stale": False, "data": [], "error": str(e)[:150]}


def _company_profile(reader, code):
    """从 chain_reader 缓存拿个股行业/主营，避免重复请求。"""
    try:
        import chain_reader
        p = chain_reader.fetch_profile(code)
        b = chain_reader.fetch_business(code)
        pd_data = p.get("data") or {}
        bd = b.get("data") or {}
        text = " ".join([
            pd_data.get("industry", ""), pd_data.get("main_business", ""),
            bd.get("main_business", ""), " ".join(bd.get("products", [])),
            " ".join(bd.get("product_types", [])),
        ])
        return pd_data.get("company", ""), pd_data.get("industry", ""), text
    except Exception:
        return "", "", ""


def analyze_policy(reader, code, refresh=False):
    """主入口：与该股相关的政策主题 + 代表快讯 + 主题热度/情绪。"""
    code = str(code).zfill(6)
    policy = _fetch_news("policy", refresh)
    global_news = _fetch_news("global", refresh)
    all_items = (policy.get("data") or []) + (global_news.get("data") or [])
    company, industry, profile_text = _company_profile(reader, code)

    # 个股主题关键词：从行业/主营文本匹配主题词典
    stock_themes = {}
    for theme, kws in THEMES.items():
        matched = [k for k in kws if k in profile_text]
        if matched:
            # 关键词在主营文本出现的次数作为权重
            weight = sum(profile_text.count(k) for k in matched)
            stock_themes[theme] = weight
    if not stock_themes:
        # 无明确主题时，仍给出全市场政策概览，但不强行关联
        stock_themes = {}

    # 找与个股主题相关的政策快讯
    related = []
    for item in all_items:
        overlap = set(item.get("themes", [])) & set(stock_themes.keys())
        if overlap:
            related.append({**item, "matched_themes": list(overlap)})
    # 按增量信号+情绪强度排序
    related.sort(key=lambda x: (len(x.get("increment", [])), abs(x.get("score", 50) - 50)), reverse=True)

    # 主题热度：全市场快讯中各主题出现次数 + 平均情绪
    theme_stats = {}
    for theme in THEMES:
        hits = [x for x in all_items if theme in x.get("themes", [])]
        if hits:
            avg = round(sum(x["score"] for x in hits) / len(hits), 1)
            theme_stats[theme] = {
                "count": len(hits),
                "avg_score": avg,
                "tone": "偏多" if avg >= 60 else ("偏空" if avg <= 40 else "中性"),
                "relevance": "高" if theme in stock_themes else "一般",
            }
    theme_stats = dict(sorted(theme_stats.items(), key=lambda x: -x[1]["count"])[:10])

    # 综合政策情绪（仅基于与个股相关快讯）
    if related:
        avg_score = round(sum(x["score"] for x in related) / len(related), 1)
        pol_tone = "偏多" if avg_score >= 60 else ("偏空" if avg_score <= 40 else "中性")
    else:
        avg_score, pol_tone = 50, "中性"

    return {
        "code": code, "company": company, "industry": industry,
        "stock_themes": [{"theme": k, "weight": v} for k, v in
                         sorted(stock_themes.items(), key=lambda x: -x[1])],
        "policy_sentiment": {"score": avg_score, "label": pol_tone,
                             "related_count": len(related)},
        "related_news": related[:20],
        "theme_heat": theme_stats,
        "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stale": policy.get("stale") or global_news.get("stale"),
        "disclaimer": ("政策主题映射为关键词规则匹配，存在误匹配可能；政策影响需以官方原文与公司公告为准，"
                       "快讯为媒体二次来源需核实原文。数据存在时效滞后，非投资建议。"),
        "source": "东方财富财经政策/全球快讯（公开）+ 关键词规则映射",
    }


def fetch_status():
    p = _read_cache("policy")
    g = _read_cache("global")
    return {
        "policy_cache_at": p.get("fetched_at", 0) if p else 0,
        "global_cache_at": g.get("fetched_at", 0) if g else 0,
        "throttle_interval": hr.MIN_INTERVAL,
    }
