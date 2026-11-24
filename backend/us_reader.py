# -*- coding: utf-8 -*-
"""
us_reader.py — 美股日线读取层（免费数据源，个人自用）

数据源（均为大陆可直连、免费、无需 API Key）：
  1) 东方财富 push2his 美股 K 线（主）：含中文名 + 日线（开/收/高/低/量/额），前复权
  2) 新浪美股日线（兜底）：东财失败时自动切换

设计目标：
  - 接口与 tdx_reader.TdxData 完全对齐（read_day / read_recent_cached / name_of /
    load_names / build_index / status / data_version），让上层分析模块零改动。
  - 返回 DataFrame 列结构与通达信 .day 严格一致：
    date(datetime) / open / high / low / close / amount / volume
  - 本地磁盘缓存，避免重复联网；缓存按"最新交易日"失效，盘中不反复请求。
  - 市场代码（105 纳斯达克 / 106 纽交所 / 107 美交所）自动探测并持久化缓存。

注意：免费源为 15 分钟级延迟的日线数据，仅供个人研究，不构成投资建议。
"""
import os
import re
import json
import time
import threading
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import requests
except ImportError:
    requests = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "us_cache")
MARKET_MAP_FILE = os.path.join(CACHE_DIR, "market_map.json")
NAMES_FILE = os.path.join(CACHE_DIR, "us_names.json")

# 东财美股市场代码（按命中概率排序：纳斯达克 → 纽交所 → 美交所）
US_MARKETS = ("105", "106", "107")
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid={mkt}.{sym}"
            "&fields1=f1,f2,f3"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            "&klt=101&fqt=1&beg=0&end=20500101&lmt={lmt}")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}

# 缓存有效期（秒）：日线数据按最新日期刷新即可，设 20 分钟足够
CACHE_TTL = 20 * 60
# 每次拉取的 K 线根数（留足统计窗口，与 A 股 500 展示 / 300 统计一致）
FETCH_LIMIT = 1200

# 常见美股代码 → 中文名（离线兜底 + 搜索用），联网后会被东财真实名称覆盖
DEFAULT_NAMES = {
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌", "GOOG": "谷歌",
    "AMZN": "亚马逊", "META": "Meta", "TSLA": "特斯拉", "AVGO": "博通", "BRK.B": "伯克希尔",
    "JPM": "摩根大通", "V": "Visa", "UNH": "联合健康", "XOM": "埃克森美孚", "JNJ": "强生",
    "WMT": "沃尔玛", "MA": "万事达", "PG": "宝洁", "HD": "家得宝", "COST": "好市多",
    "ABBV": "艾伯维", "KO": "可口可乐", "PEP": "百事可乐", "TMO": "赛默飞", "MCD": "麦当劳",
    "CSCO": "思科", "ABT": "雅培", "DIS": "迪士尼", "CRM": "赛富时", "AMD": "超威半导体",
    "ADBE": "奥多比", "NFLX": "奈飞", "INTC": "英特尔", "QCOM": "高通", "TXN": "德州仪器",
    "BABA": "阿里巴巴", "PDD": "拼多多", "JD": "京东", "BIDU": "百度", "NIO": "蔚来",
    "TSM": "台积电", "ASML": "阿斯麦", "MU": "美光", "AMAT": "应用材料", "LRCX": "泛林集团",
    "PLTR": "Palantir", "COIN": "Coinbase", "HOOD": "Robinhood", "MSTR": "微策略",
    "SPY": "标普500ETF", "QQQ": "纳指100ETF", "DIA": "道指ETF", "IWM": "罗素2000ETF",
    "GLD": "黄金ETF", "SLV": "白银ETF", "USO": "原油ETF", "VXX": "波动率ETF",
}
# 大小写不敏感的代码规范化表
_UPPER_NAMES = {k.upper(): v for k, v in DEFAULT_NAMES.items()}


def is_us_code(code):
    """判断是否为美股代码：含字母的 ticker（可含 . 或 -）。

    兼容上游误传的 zfill 代码（如 '00AAPL'）：先剥离前导 0 再判断。
    纯数字（含 A 股 6 位代码）一律不是美股。
    """
    s = str(code or "").strip().upper()
    if not s or s.isdigit():
        return False
    s2 = s.lstrip("0")
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s2)) and s2 not in ("SH", "SZ", "BJ")


def norm_code(code):
    """规范化美股代码：去空格、转大写、剥离误加的前导 0、BRK-B → BRK.B。"""
    s = str(code or "").strip().upper()
    if s.isdigit():
        return s
    return s.lstrip("0").replace("-", ".")


class USData:
    """与 TdxData 同接口的美股数据源。"""

    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._market_map = self._load_json(MARKET_MAP_FILE, {})
        self._names = self._load_json(NAMES_FILE, dict(DEFAULT_NAMES))
        self._mem_cache = {}      # {sym: (fetch_ts, df)}
        self._lock = threading.RLock()
        self._session = requests.Session() if requests else None
        self.last_error = None

    # ---------- 基础工具 ----------
    @staticmethod
    def _load_json(path, default):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    @staticmethod
    def _dump_json(path, obj):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def _cache_path(self, sym):
        return os.path.join(CACHE_DIR, sym + ".json")

    @staticmethod
    def _df_columns():
        """返回与通达信 .day 一致的列顺序。"""
        return ["date", "open", "high", "low", "close", "amount", "volume"]

    def _empty_df(self):
        return pd.DataFrame(columns=self._df_columns())

    # ---------- HTTP ----------
    def _get(self, url, timeout=12):
        if not self._session:
            raise RuntimeError("未安装 requests 库，无法获取美股数据")
        r = self._session.get(url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        return r

    def _detect_market(self, sym):
        """探测 ticker 所属市场代码，结果持久化缓存。"""
        if sym in self._market_map:
            return self._market_map[sym]
        err = None
        for mkt in US_MARKETS:
            try:
                r = self._get(EM_KLINE.format(mkt=mkt, sym=sym, lmt=3))
                j = r.json()
                data = j.get("data")
                if data and data.get("klines"):
                    self._market_map[sym] = mkt
                    self._dump_json(MARKET_MAP_FILE, self._market_map)
                    return mkt
            except Exception as e:
                err = e
                continue
        self.last_error = f"东财市场探测失败：{err}"
        return None

    def _fetch_em(self, sym):
        """东方财富主源：返回 (df, name)。"""
        mkt = self._detect_market(sym)
        if not mkt:
            return None, None
        r = self._get(EM_KLINE.format(mkt=mkt, sym=sym, lmt=FETCH_LIMIT), timeout=20)
        j = r.json()
        data = j.get("data")
        if not data or not data.get("klines"):
            return None, None
        name = data.get("name") or ""
        rows = []
        for line in data["klines"]:
            p = line.split(",")
            # f51 日期, f52 开, f53 收, f54 高, f55 低, f56 量, f57 额
            if len(p) < 7:
                continue
            rows.append((p[0], p[1], p[3], p[4], p[2], p[6], p[5]))
        return self._rows_to_df(rows), name

    def _fetch_sina(self, sym):
        """新浪兜底：返回 (df, name=None)。"""
        url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/var%20_"
               + sym + "=/US_MinKService.getDailyK?symbol=" + sym)
        r = self._get(url, timeout=20)
        text = r.text
        m = re.search(r"\(\s*(\[.*\])\s*\)", text, re.S)
        if not m:
            return None, None
        arr = json.loads(m.group(1))
        rows = []
        for it in arr:
            try:
                rows.append((it["d"], it["o"], it["h"], it["l"], it["c"],
                             it.get("a", "0"), it["v"]))
            except Exception:
                continue
        return self._rows_to_df(rows), None

    @staticmethod
    def _rows_to_df(rows):
        """rows: (date, open, high, low, close, amount, volume) 字符串元组。"""
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "amount", "volume"])
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "amount", "volume"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for c in ("open", "high", "low", "close", "amount", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["date"]).dropna(subset=["open", "high", "low", "close"])
        df = df[df["close"] > 0]          # 过滤前复权产生的非正价格
        df = df.sort_values("date").reset_index(drop=True)
        return df

    # ---------- 缓存 ----------
    def _read_disk_cache(self, sym):
        try:
            path = self._cache_path(sym)
            if not os.path.exists(path):
                return None
            if time.time() - os.path.getmtime(path) > CACHE_TTL:
                return None
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            df = self._rows_to_df(obj["rows"])
            if obj.get("name"):
                self._names[sym] = obj["name"]
            return df if not df.empty else None
        except Exception:
            return None

    def _write_disk_cache(self, sym, df, name):
        try:
            rows = []
            for _, r in df.iterrows():
                rows.append((r["date"].strftime("%Y-%m-%d"),
                             r["open"], r["high"], r["low"], r["close"],
                             r.get("amount", 0), r["volume"]))
            tmp = self._cache_path(sym) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rows": rows, "name": name, "ts": time.time()}, f, ensure_ascii=False)
            os.replace(tmp, self._cache_path(sym))
            if name:
                self._names[sym] = name
                self._dump_json(NAMES_FILE, self._names)
        except Exception:
            pass

    def _load(self, sym, force=False):
        """加载日线：内存 → 磁盘 → 联网（东财 → 新浪）。"""
        sym = norm_code(sym)
        with self._lock:
            if not force:
                mem = self._mem_cache.get(sym)
                if mem and time.time() - mem[0] < CACHE_TTL:
                    return mem[1]
                disk = self._read_disk_cache(sym)
                if disk is not None:
                    self._mem_cache[sym] = (time.time(), disk)
                    return disk

            df, name = (None, None)
            errs = []
            try:
                df, name = self._fetch_em(sym)
            except Exception as e:
                errs.append(f"东财：{e}")
            if df is None or df.empty:
                try:
                    df, name2 = self._fetch_sina(sym)
                    name = name or name2
                except Exception as e:
                    errs.append(f"新浪：{e}")
            if df is None or df.empty:
                self.last_error = "；".join(errs) or "无数据"
                return self._empty_df()
            self._write_disk_cache(sym, df, name)
            self._mem_cache[sym] = (time.time(), df)
            return df

    # ---------- 与 TdxData 对齐的公开接口 ----------
    def read_day(self, code, recent=None):
        sym = norm_code(code)
        df = self._load(sym).copy()
        if recent and not df.empty:
            df = df.tail(recent)
        return df.reset_index(drop=True)

    def read_recent_cached(self, code, n=260, check_update=True):
        return self.read_day(code, recent=n)

    def name_of(self, code):
        sym = norm_code(code)
        if sym in self._names and self._names[sym]:
            return self._names[sym]
        # 触发一次加载以补全名称
        self._load(sym)
        return self._names.get(sym, "")

    def load_names(self, force=False):
        return dict(self._names)

    def build_index(self):
        """美股无本地全市场索引；返回已缓存/常见 ticker 集合供搜索显示。"""
        idx = {}
        for f in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
            if f.endswith(".json") and f not in ("market_map.json", "us_names.json"):
                sym = f[:-5]
                idx[sym] = os.path.join(CACHE_DIR, f)
        return idx

    @staticmethod
    def data_version():
        return "us-free-v1"

    def status(self):
        return {
            "source": "eastmoney+sina (free, ~15min delay)",
            "market": "US",
            "cached_symbols": len(self.build_index()),
            "last_error": self.last_error,
        }


if __name__ == "__main__":
    u = USData()
    for code in ("AAPL", "MSFT", "BABA"):
        df = u.read_day(code, recent=3)
        print("===", code, u.name_of(code))
        print(df.tail(3).to_string(index=False) if not df.empty else "EMPTY " + str(u.last_error))
