# -*- coding: utf-8 -*-
"""
news_reader.py — 个股舆情数据采集（基于 akshare / 东方财富公开数据）

数据源（均为公开网页数据，仅供个人学习研究，版权归原作者）：
  - 新闻：ak.stock_news_em(symbol)              东方财富个股新闻
  - 公告：ak.stock_individual_notice_report()   东方财富公告大全-个股
  - 研报：ak.stock_research_report_em(symbol)   东方财富个股研报

设计要点：
  - 磁盘缓存：新闻 TTL 短（30 分钟），公告/研报 TTL 长（6 小时），避免重复拉取耗时接口
  - 失败回退：网络异常时返回过期缓存（stale），保证离线也能看到上次结果
  - 标准化输出：统一字段、去除多余长文本、严格 JSON 友好
"""
import os
import json
import time
import threading
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "sentiment_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 各类数据缓存有效期（秒）
TTL = {"news": 30 * 60, "notice": 6 * 3600, "research": 6 * 3600}

_LOCK = threading.RLock()

# akshare 延迟导入：没装/断网时不影响本地技术分析模块
def _ak():
    try:
        import akshare as ak
        return ak
    except Exception as e:
        raise RuntimeError(f"akshare 不可用：{e}")


def _cache_path(kind, code):
    return os.path.join(CACHE_DIR, f"{kind}_{code}.json")


def _read_cache(kind, code):
    path = _cache_path(kind, code)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(kind, code, payload):
    path = _cache_path(kind, code)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _s(val, default=""):
    """DataFrame 单元格 → 干净字符串。"""
    if val is None:
        return default
    if isinstance(val, float) and pd.isna(val):
        return default
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _safe_fetch(kind, code, fetch_fn):
    """带缓存的统一取数：命中新鲜缓存直接用；否则联网拉取，失败回退旧缓存。"""
    cached = _read_cache(kind, code)
    now = time.time()
    if cached and now - cached.get("fetched_at", 0) < TTL.get(kind, 3600) and not cached.get("stale"):
        return cached

    with _LOCK:
        # 双重检查：等待锁期间其它线程可能已更新缓存
        cached = _read_cache(kind, code)
        if cached and now - cached.get("fetched_at", 0) < TTL.get(kind, 3600) and not cached.get("stale"):
            return cached
        try:
            items = fetch_fn()
            payload = {"fetched_at": time.time(), "stale": False, "items": items}
            _write_cache(kind, code, payload)
            return payload
        except Exception as e:
            if cached and cached.get("items") is not None:
                stale = dict(cached)
                stale["stale"] = True
                stale["error"] = str(e)[:300]
                return stale
            return {"fetched_at": 0, "stale": False, "items": [], "error": str(e)[:300]}


def _fetch_news(code, limit=30):
    df = _ak().stock_news_em(symbol=code)
    items = []
    for _, r in df.head(limit).iterrows():
        content = _s(r.get("新闻内容"))
        items.append({
            "title": _s(r.get("新闻标题")),
            "content": content[:300],          # 截断，避免前端体积过大
            "time": _s(r.get("发布时间")),
            "source": _s(r.get("文章来源")),
            "url": _s(r.get("新闻链接")),
        })
    return items


def _fetch_notice(code, limit=40):
    df = _ak().stock_individual_notice_report(security=code)
    items = []
    for _, r in df.head(limit).iterrows():
        items.append({
            "title": _s(r.get("公告标题")),
            "type": _s(r.get("公告类型")),
            "date": _s(r.get("公告日期"))[:10],
            "url": _s(r.get("网址")),
        })
    return items


def _fetch_research(code, limit=40):
    df = _ak().stock_research_report_em(symbol=code)
    items = []
    for _, r in df.head(limit).iterrows():
        items.append({
            "title": _s(r.get("报告名称")),
            "org": _s(r.get("机构")),
            "rating": _s(r.get("东财评级")),
            "date": _s(r.get("日期"))[:10],
            "industry": _s(r.get("行业")),
            "url": _s(r.get("报告PDF链接")),
        })
    return items


def fetch(kind, code, refresh=False):
    """对外入口：fetch('news'|'notice'|'research', code, refresh=False)
    返回 {fetched_at, stale, items, error?}
    """
    code = str(code).zfill(6)
    if refresh:
        c = _cache_path(kind, code)
        try:
            os.remove(c)
        except Exception:
            pass
    if kind == "news":
        fn = lambda: _fetch_news(code)
    elif kind == "notice":
        fn = lambda: _fetch_notice(code)
    elif kind == "research":
        fn = lambda: _fetch_research(code)
    else:
        raise ValueError(f"未知舆情类型：{kind}")
    return _safe_fetch(kind, code, fn)


def cache_status(code):
    """查看三类数据的缓存时间与新鲜度，供前端提示。"""
    code = str(code).zfill(6)
    out = {}
    now = time.time()
    for kind, ttl in TTL.items():
        c = _read_cache(kind, code)
        if not c:
            out[kind] = {"cached": False}
        else:
            age = int(now - c.get("fetched_at", 0))
            out[kind] = {
                "cached": True,
                "age_seconds": age,
                "stale": bool(c.get("stale")),
                "within_ttl": age < ttl,
                "count": len(c.get("items", [])),
            }
    return out
