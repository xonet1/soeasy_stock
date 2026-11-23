# -*- coding: utf-8 -*-
"""
tdx_reader.py
通达信内核券商客户端本地盘后数据读取层。

数据来源：
  C:\\zd_gjzq\\vipdoc\\{sh,sz,bj}\\lday\\*.day   日线（32 字节/条）
  C:\\zd_gjzq\\T0002\\hq_cache\\{shs,szs,bjs}.tnf  股票名称（GBK）

.day 记录结构（小端）：
  date int32(YYYYMMDD), open/high/low/close int32,
  amount float32, volume int32, reserved int32
  价格精度：股票=价格*100，场内基金ETF/LOF=价格*1000（价格<2元时精度到0.001）

仅供个人学习与自用分析。
"""
import os
import json
import glob
import threading
from datetime import datetime

import numpy as np
import pandas as pd

# 通达信日线 dtype
DAY_DTYPE = np.dtype([
    ("date", "<i4"),
    ("open", "<i4"),
    ("high", "<i4"),
    ("low", "<i4"),
    ("close", "<i4"),
    ("amount", "<f4"),
    ("volume", "<i4"),
    ("reserved", "<i4"),
])

# 场内基金（ETF/LOF/债券ETF等）代码前缀：价格按 *1000 存储（精度 0.001 元）
# 股票、指数按 *100 存储。判断错误会使价格放大 10 倍。
_FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")

# 常见客户端安装根目录（自动探测）
CANDIDATE_ROOTS = [
    r"C:\zd_gjzq",          # 常见券商客户端默认安装路径
    r"C:\new_tdx",
    r"D:\new_tdx",
    r"C:\zd_zxzq_gm",
    r"C:\Program Files\new_tdx",
    r"C:\Program Files (x86)\new_tdx",
]

# .tnf 结构：50 字节文件头 + 每条 360 字节定长记录
#   0-6     代码（ASCII）
#   31-47   名称（GBK，16 字节 NUL 结尾，最多 8 个汉字）
#   329-333 拼音缩写
# 注意：沪深两市存在同码不同标的（000001 沪=上证指数、深=平安银行），
# 所以必须按市场分别解析，再按代码段决定取哪个市场的名称。
_TNF_HEADER = 50
_TNF_STRIDE = 360
_TNF_NAME_OFF, _TNF_NAME_LEN = 31, 16

# 除息/除权临时标记，不属于公司名（TDX 会随分红自动加/去，保留会导致名称漂移）
_TNF_TMP_PREFIX = ("XD", "XR", "DR")

# 代码段 → 市场优先级（前者优先）
_MKT_PREF = (
    (("399",), ("sz", "sh", "bj")),                                        # 深证指数
    (("000", "001", "002", "003", "300", "301", "200",
      "15", "16", "18", "12"), ("sz", "sh", "bj")),                        # 深市个股/基金/转债
    (("60", "68", "69", "90", "11", "13", "50", "51", "52", "56", "58"),
     ("sh", "sz", "bj")),                                                  # 沪市个股/基金/转债
    (("43", "83", "87", "88", "89", "81", "92"), ("bj", "sh", "sz")),      # 北交所
)


def _market_order(code):
    """按代码段给出该取哪个市场的名称（同码时避免被另一市场的指数名覆盖）。"""
    for prefixes, order in _MKT_PREF:
        if code.startswith(prefixes):
            return order
    return ("sh", "sz", "bj")


def _clean_name(raw):
    """GBK 解码 + 去填充零/对齐空格 + 全角字母数字转半角 + 去除权临时前缀。"""
    s = raw.split(b"\x00", 1)[0].decode("gbk", "ignore")
    out = []
    for ch in s:
        o = ord(ch)
        # Ａ-Ｚ / ａ-ｚ / ０-９ → 半角（通达信把 "深振业Ａ" 这类后缀存成全角）
        if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A or 0xFF10 <= o <= 0xFF19:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out).replace(" ", "").replace("\u3000", "").strip()
    # 去掉 XD（除息）/XR（除权）/DR（除权除息）临时标记，"XD中国中免" → "中国中免"
    for pre in _TNF_TMP_PREFIX:
        if s.startswith(pre) and len(s) > len(pre) + 1:
            s = s[len(pre):]
            break
    return s


class TdxData:
    def __init__(self, root=None):
        self.root = root or self.detect_root()
        if not self.root:
            raise RuntimeError("未找到通达信 vipdoc 数据目录，请手动指定客户端安装路径")
        self.vipdoc = os.path.join(self.root, "vipdoc")
        self._names = None          # {code6: name}
        self._file_index = None     # {code6: filepath}
        self._cache = {}            # {code: DataFrame} 近期数据缓存
        self._file_mtime = {}       # {code: mtime} 用于检测数据更新
        self._lock = threading.RLock()
        self.last_scan = None

    # ---------- 路径探测 ----------
    @staticmethod
    def detect_root():
        for r in CANDIDATE_ROOTS:
            if os.path.isdir(os.path.join(r, "vipdoc", "sh", "lday")):
                return r
        return None

    def _market_of(self, code):
        """根据 6 位代码推断市场前缀。"""
        c = str(code).zfill(6)
        if c.startswith(("60", "68", "51", "58", "00")):
            # 000/001 指数在上交所，但个股 000 在深交所；优先按文件索引判断
            pass
        return c

    # ---------- 文件索引 ----------
    def build_index(self, force=False):
        if self._file_index is not None and not force:
            return self._file_index
        idx = {}
        for mkt in ("sh", "sz", "bj"):
            lday = os.path.join(self.vipdoc, mkt, "lday")
            if not os.path.isdir(lday):
                continue
            for fp in glob.glob(os.path.join(lday, "*.day")):
                code = os.path.basename(fp)[2:8]
                if code.isdigit():
                    idx[code] = fp
        with self._lock:
            self._file_index = idx
        return idx

    def all_codes(self):
        return sorted(self.build_index().keys())

    # ---------- 股票名称 ----------
    def _parse_tnf(self, path):
        """结构化解析单个 .tnf → {code6: name}。"""
        out = {}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            return out
        for off in range(_TNF_HEADER, len(data) - _TNF_STRIDE + 1, _TNF_STRIDE):
            rec = data[off:off + _TNF_STRIDE]
            code = rec[:6].decode("ascii", "ignore")
            if len(code) != 6 or not code.isdigit():
                continue
            name = _clean_name(rec[_TNF_NAME_OFF:_TNF_NAME_OFF + _TNF_NAME_LEN])
            if name:
                out[code] = name
        return out

    @staticmethod
    def _fill_truncated(names):
        """tnf 名称字段仅 8 字节（最多 4 个汉字），超长会被截断。
        用巨潮缓存里的公司全名补全，但只接受「tnf 名是全名前缀」的情况，
        避免把指数名（如 000018=180金融）误当成公司名。"""
        cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 "data", "cninfo_cache")
        if not os.path.isdir(cache_dir):
            return 0
        fixed = 0
        for fp in glob.glob(os.path.join(cache_dir, "cn_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            code = str(d.get("code") or "").zfill(6)
            full = (d.get("name") or "").strip()
            cur = names.get(code)
            if not cur or not full or full == "不存在":
                continue
            if len(full) > len(cur) and full.startswith(cur):
                names[code] = full
                fixed += 1
        return fixed

    def load_names(self, force=False):
        """名称表：按市场分别解析 tnf，再按代码段取对应市场的名称。

        修复点：早期版本用正则在全文件里抓「6位数字+名字」，同一代码在沪深两市
        含义不同（000858 沪=中证500信息指数、深=五粮液），后解析的沪市会把深市
        个股名覆盖掉，导致 000858 显示成"500信息"。
        """
        if self._names is not None and not force:
            return self._names
        cache_file = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  "data", "stock_names.json")
        if os.path.exists(cache_file) and not force:
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    self._names = json.load(f)
                return self._names
            except Exception:
                pass

        hq_cache = os.path.join(self.root, "T0002", "hq_cache")
        by_mkt = {m: self._parse_tnf(os.path.join(hq_cache, m + "s.tnf"))
                  for m in ("sh", "sz", "bj")}

        # 只保留本地能读到行情的代码（lday 索引），外加沪市独立指数（如 999999）
        codes = set(self.build_index().keys())
        if not codes:
            codes = set().union(*(set(v) for v in by_mkt.values()))

        names = {}
        for code in codes:
            for mkt in _market_order(code):
                nm = by_mkt.get(mkt, {}).get(code)
                if nm:
                    names[code] = nm
                    break

        fixed = self._fill_truncated(names)
        if fixed:
            print(f"[tdx] 名称补全（截断→全名）：{fixed} 条")

        with self._lock:
            self._names = names
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(names, f, ensure_ascii=False)
        except Exception:
            pass
        return names

    def name_of(self, code):
        return self.load_names().get(str(code).zfill(6), "")

    # ---------- 日线读取 ----------
    def read_day(self, code, recent=None):
        """读取单只股票日线，返回 DataFrame。recent=N 只取最近 N 条。"""
        code = str(code).zfill(6)
        idx = self.build_index()
        fp = idx.get(code)
        if not fp:
            return pd.DataFrame()
        try:
            arr = np.fromfile(fp, dtype=DAY_DTYPE)
        except Exception:
            return pd.DataFrame()
        if arr.size == 0:
            return pd.DataFrame()
        df = pd.DataFrame(arr)
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
        # 价格精度：场内基金（ETF/LOF）按价格*1000 存储，股票/指数按价格*100 存储。
        # 若对基金误用 /100，价格会被放大 10 倍，导致盈亏计算严重失真。
        price_div = 1000.0 if code.startswith(_FUND_PREFIXES) else 100.0
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] / price_div
        df["volume"] = df["volume"].astype("float64")
        df = df.dropna(subset=["date"]).drop(columns=["reserved"])
        df = df[df["close"] > 0]
        if recent:
            df = df.tail(recent)
        return df.reset_index(drop=True)

    def read_recent_cached(self, code, n=260, check_update=True):
        """带缓存的近期数据，供选股/信号扫描复用。
        动态感知：若源文件 mtime 变化（客户端更新了数据），自动重建缓存。"""
        code = str(code).zfill(6)
        key = (code, n)
        fp = self.build_index().get(code)
        mtime = os.path.getmtime(fp) if fp and os.path.exists(fp) else 0
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and (not check_update or self._file_mtime.get(code) == mtime):
                return cached
        df = self.read_day(code, recent=n)
        with self._lock:
            # 该文件更新 → 清理此代码的所有缓存，避免不同 n 的缓存不一致
            if self._file_mtime.get(code) != mtime:
                for k in [k for k in self._cache if k[0] == code]:
                    self._cache.pop(k, None)
                self._file_mtime[code] = mtime
            if len(self._cache) > 4000:
                self._cache.clear()
                self._file_mtime.clear()
            self._cache[key] = df
        return df

    def read_watchlist(self):
        """动态读取通达信内核客户端自选股（T0002/blocknew/zxg.blk）。
        格式：每行 7 位，首位市场标识（1=沪/0=深），后 6 位代码。"""
        blk = os.path.join(self.root, "T0002", "blocknew", "zxg.blk")
        if not os.path.exists(blk):
            return []
        with open(blk, "rb") as f:
            text = f.read().decode("gbk", "ignore")
        codes = []
        seen = set()
        idx = self.build_index()
        for line in text.splitlines():
            line = line.strip()
            if len(line) == 7 and line.isdigit():
                code = line[1:]
                if code in seen or code not in idx:
                    continue
                seen.add(code)
                codes.append({"code": code, "name": self.name_of(code),
                              "has_data": True, "market": "sh" if line[0] == "1" else "sz"})
        return codes

    def data_version(self):
        """数据版本指纹：前端据此判断自选股/行情数据是否已更新。"""
        idx = self.build_index()
        probes = ["600519", "000001", "300750", "601318"]
        vals = []
        for c in probes:
            fp = idx.get(c)
            if fp and os.path.exists(fp):
                vals.append(f"{c}:{int(os.path.getmtime(fp))}")
        return "|".join(vals)

    def status(self):
        idx = self.build_index()
        sh = sum(1 for p in idx.values() if os.sep + "sh" + os.sep in p)
        sz = sum(1 for p in idx.values() if os.sep + "sz" + os.sep in p)
        bj = len(idx) - sh - sz
        # 采样最新日期
        latest = None
        sample = idx.get("600519") or (next(iter(idx.values())) if idx else None)
        if sample:
            try:
                arr = np.fromfile(sample, dtype=DAY_DTYPE)
                if arr.size:
                    latest = str(int(arr["date"][-1]))
            except Exception:
                pass
        return {
            "root": self.root,
            "vipdoc": self.vipdoc,
            "total": len(idx),
            "sh": sh, "sz": sz, "bj": bj,
            "names": len(self.load_names()),
            "latest_date": latest,
        }


if __name__ == "__main__":
    import sys
    t = TdxData()

    if "--rebuild-names" in sys.argv:
        cache_file = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  "data", "stock_names.json")
        backup = cache_file + ".bak"
        old = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    old = json.load(f)
                with open(backup, "w", encoding="utf-8") as f:
                    json.dump(old, f, ensure_ascii=False)
            except Exception:
                old = {}
        new = t.load_names(force=True)
        print(f"名称表重建：{len(old)} 条 → {len(new)} 条（旧表已备份 stock_names.json.bak）")
        # 找出被纠正的条目（同一代码名称变化）
        changed = [(c, old[c], new[c]) for c in old
                   if c in new and old[c] != new[c]]
        print(f"名称被纠正：{len(changed)} 条")
        for c, a, b in changed[:25]:
            print(f"  {c}  {a}  →  {b}")
        rest = changed[25:]
        # 只展示个股类代码的变化，避免指数/债券噪声
        stock_like = [x for x in rest if x[0].startswith(("60", "68", "00", "30", "001", "002", "003"))]
        print(f"  ……其中个股类还有 {len(stock_like)} 条，例如：")
        for c, a, b in stock_like[:10]:
            print(f"  {c}  {a}  →  {b}")
        print("抽样校验：")
        for c in ("000001", "000858", "600519", "000651", "603019", "300750", "000006", "159915"):
            print(f"  {c} = {new.get(c)}")
        sys.exit(0)

    print("status:", t.status())
    print("600519 name:", t.name_of("600519"))
    df = t.read_day("600519", recent=3)
    print(df.tail(3).to_string())
