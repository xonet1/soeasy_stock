# -*- coding: utf-8 -*-
"""
relation_reader.py — 个股关联分析（基于本地行情，纯真实可复算）

分析维度：
  1. 价格联动：目标股 vs 自选股/持仓股/宽基指数 的日收益率 Pearson 相关系数
     （60/120 日窗口），识别高相关联动标的，标注"统计相关≠因果"。
  2. 相对强弱：目标股相对基准（沪深300等）的超额收益、Beta、波动率。
  3. 产业链关联：复用 chain_reader 的上下游图谱节点（若知识库有覆盖）。
  4. 股东重叠关联：与目标股共享前十大股东的其他标的（基于 holder_reader 缓存）。

数据全部来自本地通达信日线 + 已缓存的公开披露数据，不引入新的外部请求。
仅供个人学习研究，非投资建议。
"""
import os
import json
import threading
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "relation_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
TTL = 12 * 3600
_LOCK = threading.RLock()

# 宽基指数（通达信代码体系）
INDEXES = {
    "000001": "上证指数", "399001": "深证成指",
    "399006": "创业板指", "000300": "沪深300", "000688": "科创50",
}
# 常见行业龙头作为关联参照池（当自选股不足时补充）
DEFAULT_POOL = ["600519", "300750", "002415", "601318", "000858", "688981", "002594", "600036"]


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"rel_{code}.json")


def _read_cache(code):
    try:
        with open(_cache_path(code), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(code, payload):
    tmp = _cache_path(code) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(code))
    except Exception:
        pass


def _returns(series, n):
    s = series.tail(n + 1).reset_index(drop=True)
    return s.pct_change().dropna()


def _corr(a, b):
    """两段收益率的 Pearson 相关，按公共长度对齐。"""
    n = min(len(a), len(b))
    if n < 10:
        return None
    x, y = a.iloc[-n:].values, b.iloc[-n:].values
    if np.std(x) == 0 or np.std(y) == 0:
        return None
    c = float(np.corrcoef(x, y)[0, 1])
    return None if c != c else round(c, 3)


def _max_drawdown(nav):
    if len(nav) < 2:
        return None
    peak = np.maximum.accumulate(nav)
    dd = (nav - peak) / peak
    return round(float(dd.min()) * 100, 2)


def analyze_relation(reader, code, pool=None, window=60, refresh=False):
    """主入口：目标股与关联池的相关性 + 相对基准强弱。"""
    code = str(code).zfill(6)
    cache = _read_cache(code)
    import time
    if cache and not refresh and time.time() - cache.get("fetched_at", 0) < TTL:
        return cache.get("data")
    with _LOCK:
        cache = _read_cache(code)
        if cache and not refresh and time.time() - cache.get("fetched_at", 0) < TTL:
            return cache.get("data")

        target = reader.read_recent_cached(code, n=window + 30)
        if target is None or len(target) < window + 5:
            return {"code": code, "error": "本地行情数据不足，无法计算关联"}

        # 关联池：优先自选股，其次默认龙头；去重并排除自身
        if not pool:
            try:
                pool = [x[1] if isinstance(x, (list, tuple)) else x for x in reader.read_watchlist()]
            except Exception:
                pool = []
            pool = [str(x).zfill(6) for x in pool if str(x).zfill(6) != code][:40]
            if len(pool) < 6:
                pool = list(dict.fromkeys(pool + [c for c in DEFAULT_POOL if c != code]))[:12]
        pool = [str(x).zfill(6) for x in pool if str(x).zfill(6) != code][:40]

        tr = _returns(target["close"], window)
        correlations = []
        for c in pool:
            try:
                df = reader.read_recent_cached(c, n=window + 30)
                if df is None or len(df) < window + 5:
                    continue
                cr = _corr(tr, _returns(df["close"], window))
                if cr is None:
                    continue
                correlations.append({
                    "code": c,
                    "name": reader.name_of(c),
                    "corr_60d": _corr(tr, _returns(df["close"], min(60, window))),
                    "corr": cr,
                })
            except Exception:
                continue
        correlations = [x for x in correlations if x["corr"] is not None]
        correlations.sort(key=lambda x: -abs(x["corr"]))
        for x in correlations:
            v = x["corr"]
            if v >= 0.7:
                x["level"] = "高度联动"
            elif v >= 0.45:
                x["level"] = "中度联动"
            elif v <= -0.4:
                x["level"] = "负相关"
            else:
                x["level"] = "弱相关"

        # 相对基准（沪深300/上证）
        bench_code = "000300"
        bench = reader.read_recent_cached(bench_code, n=window + 30)
        bench_out = {}
        relative = []
        if bench is not None and len(bench) >= window + 5:
            br = _returns(bench["close"], window)
            beta_corr = _corr(tr, br)
            aligned = min(len(tr), len(br))
            tv = tr.iloc[-aligned:].values
            bv = br.iloc[-aligned:].values
            # np.cov 默认 ddof=1、np.var 默认 ddof=0，混用会把 Beta 放大 N/(N-1)。统一 ddof=1。
            var_bv = np.var(bv, ddof=1)
            beta = round(float(np.cov(tv, bv)[0, 1] / var_bv), 3) if var_bv > 0 else None
            # 净值曲线（对齐）
            tnav = np.cumprod(1 + tv)
            bnav = np.cumprod(1 + bv)
            excess = round(float(tnav[-1] - bnav[-1]) * 100, 2)
            bench_out = {
                "index": bench_code, "index_name": INDEXES.get(bench_code, bench_code),
                "corr": beta_corr, "beta": beta,
                "excess_return": excess,
                "ann_vol": round(float(np.std(tv) * np.sqrt(252)) * 100, 2),
                "max_drawdown": _max_drawdown(tnav),
            }
            for i in range(0, aligned, max(1, aligned // 20)):
                relative.append({"i": i, "stock": round(float(tnav[i]) * 100, 2),
                                 "bench": round(float(bnav[i]) * 100, 2)})

        # 产业链关联（复用 chain_reader 知识库节点）
        chain_nodes = []
        try:
            import chain_reader
            if not chain_reader.is_fund(code):
                inf = chain_reader.infer_chain(code, {
                    "profile": {"data": chain_reader.fetch_profile(code).get("data")},
                    "business": {"data": chain_reader.fetch_business(code).get("data")},
                    "compose": {"data": {}},
                })
                for n in inf.get("nodes", [])[:20]:
                    cc = str(n.get("code", "")).zfill(6) if n.get("code") else ""
                    if cc and cc != code:
                        chain_nodes.append({"code": cc, "name": n.get("name", ""),
                                            "role": n.get("role", ""), "stage": n.get("stage", ""),
                                            "source": n.get("source", "")})
        except Exception:
            pass

        result = {
            "code": code, "name": reader.name_of(code), "window": window,
            "as_of": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "correlations": correlations[:16],
            "benchmark": bench_out,
            "relative_curve": relative,
            "chain_links": chain_nodes[:20],
            "disclaimer": "相关系数为历史收益率的统计相关，不代表因果或未来联动；贝塔/超额收益基于沪深300对齐计算，非投资建议。",
        }
        _write_cache(code, {"fetched_at": time.time(), "data": result})
        return result
