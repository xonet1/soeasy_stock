# -*- coding: utf-8 -*-
"""
market_router.py — 统一数据源路由（A 股 / 美股）

设计目标：
  上层 30+ 处模块只认一个 reader 对象。本类实现与 TdxData 完全一致的接口，
  内部按代码格式分发：6 位数字 → 通达信本地数据；字母 ticker → 美股免费源。
  这样 indicators / signals / decision_engine / screener 等模块零改动。

关键约束：
  - build_index() 只返回 A 股代码。全市场选股/信号扫描会遍历它并对每个代码
    调用 read_day；若混入美股将触发数千次联网。美股仅通过"按需个股查询"接入。
  - load_names() 默认只返回 A 股名称，避免全市场选择器把美股当 A 股处理。
    美股搜索走独立的 search_us()。
"""
import os
import threading

from tdx_reader import TdxData
from us_reader import USData, is_us_code, norm_code


class MarketRouter:
    """同时代理 A 股（本地通达信）与美股（免费源）的统一数据入口。"""

    def __init__(self):
        self.tdx = None
        self.tdx_error = None
        try:
            self.tdx = TdxData()
        except Exception as e:
            self.tdx_error = str(e)
        self.us = USData()
        self._lock = threading.RLock()

    # ---------- 内部路由 ----------
    @staticmethod
    def is_us(code):
        return is_us_code(code)

    def _src(self, code):
        """按代码返回对应的数据源与规范化代码。"""
        if is_us_code(code):
            return self.us, norm_code(code)
        return self.tdx, str(code).zfill(6)

    # ---------- 与 TdxData 对齐的接口 ----------
    def read_day(self, code, recent=None):
        src, c = self._src(code)
        if src is None:
            import pandas as pd
            return pd.DataFrame()
        return src.read_day(c, recent=recent)

    def read_recent_cached(self, code, n=260, check_update=True):
        src, c = self._src(code)
        if src is None:
            import pandas as pd
            return pd.DataFrame()
        if isinstance(src, USData):
            return src.read_recent_cached(c, n=n, check_update=check_update)
        return src.read_recent_cached(c, n=n, check_update=check_update)

    def name_of(self, code):
        src, c = self._src(code)
        if src is None:
            return ""
        return src.name_of(c)

    def load_names(self, force=False):
        """仅返回 A 股名称表（保持全市场选择器语义不变）。"""
        return self.tdx.load_names(force=force) if self.tdx else {}

    def build_index(self):
        """仅返回 A 股索引（保持全市场扫描语义不变）。"""
        return self.tdx.build_index() if self.tdx else {}

    def read_watchlist(self):
        return self.tdx.read_watchlist() if self.tdx else []

    def data_version(self):
        """版本指纹需同时反映 A 股本地数据更新与美股缓存状态。"""
        a = self.tdx.data_version() if self.tdx else "none"
        return f"{a}|us-v1"

    def status(self):
        if self.tdx:
            s = self.tdx.status()
        else:
            s = {"root": None, "total": 0, "names": 0, "latest_date": None,
                 "init_error": self.tdx_error}
        s["market"] = "A+US"
        s["us"] = self.us.status()
        return s

    # ---------- 美股专属接口 ----------
    def search_us(self, q):
        """按代码/名称搜索美股，供 /api/search 合并结果。"""
        q = (q or "").strip().upper()
        if not q:
            return []
        res = []
        names = self.us.load_names()
        for sym, nm in names.items():
            if q in sym or q in str(nm).upper() or q in str(nm):
                res.append({"code": sym, "name": nm,
                            "has_data": True, "market": "US"})
        # 即使不在名称表，只要是合法的字母 ticker 就允许直接查询
        if is_us_code(q) and not any(x["code"] == norm_code(q) for x in res):
            sym = norm_code(q)
            res.insert(0, {"code": sym, "name": self.us.name_of(sym) or sym,
                           "has_data": True, "market": "US"})
        return res[:30]

    @property
    def vipdoc(self):
        return self.tdx.vipdoc if self.tdx else None

    @property
    def root(self):
        return self.tdx.root if self.tdx else None
