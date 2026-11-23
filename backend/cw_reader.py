# -*- coding: utf-8 -*-
"""
cw_reader.py — 通达信"专业财务数据"本地离线解析（零网络依赖）

数据源：通达信客户端专业财务数据下载器落盘的 zip 包，默认目录
C:/zd_gjzq/vipdoc/cw/（可用环境变量 TDX_CW_DIR 覆盖），文件名 gpcw{YYYYMMDD}.zip，
按报告期打包，每期含全市场所有已披露个股。

二进制布局（实测验证，与 pytdx HistoryFinancialCrawler 一致）：
  20B 头  <1hI1H3L> ：[1]=报告期 int(YYYYMMDD)  [2]=股票数  [4]=记录长度(report_size)
  每股 11B 索引 <6s1c1L>：6B 代码 + 1B 占位 + 4B 记录偏移
  记录 = report_size/4 个 float32，字段编号 1 基（见 FIELD_NAMES）

字段编号来源：QUANTAXIS financial_dict（通达信专业财务数据字段表）。
所有输出为描述性统计参考，不构成投资建议。
"""
import os
import re
import json
import math
import struct
import zipfile
import time

# 1 基字段编号 → 业务名（只解析用得到的字段，bounds 检查兼容旧格式小记录）
FIELDS = {
    1: "eps",             # 基本每股收益
    2: "eps_ded",         # 扣非每股收益
    3: "undist_ps",       # 每股未分配利润
    4: "bps",             # 每股净资产
    5: "cap_ps",          # 每股资本公积金
    6: "roe",             # 净资产收益率(%)
    7: "cfo_ps",          # 每股经营现金流量
    40: "assets",         # 资产总计
    63: "liab",           # 负债合计
    72: "equity",         # 所有者权益合计
    96: "np",             # 归属于母公司所有者的净利润
    107: "cfo",           # 经营活动产生的现金流量净额
    183: "rev_g",         # 营业收入增长率(%)
    184: "np_g",          # 净利润增长率(%)
    199: "net_margin",    # 销售净利率(%)
    202: "gross_margin",  # 销售毛利率(%)
    206: "np_ded",        # 扣非净利润
    210: "debt_ratio",    # 资产负债率(%)
    238: "shares",        # 总股本
    281: "roe_w",         # 加权净资产收益率(%)
    74: "rev",            # 营业收入（利润表口径；茅台等含财务公司的个体，
                          # 与东财"营业总收入"差利息收入部分，增长率口径一致）
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
_CACHE_VER = "v2"  # 字段集变更时递增，避免旧缓存混用


def cw_dir():
    return os.environ.get("TDX_CW_DIR", "C:/zd_gjzq/vipdoc/cw")


def is_available():
    d = cw_dir()
    return os.path.isdir(d) and any(f.startswith("gpcw") and f.endswith(".zip") for f in os.listdir(d))


def available_periods():
    """全部报告期升序 [YYYYMMDD, ...]。"""
    d = cw_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for f in os.listdir(d):
        m = re.match(r"gpcw(\d{8})\.zip$", f)
        if m:
            out.append(m.group(1))
    return sorted(out)


def _nan_clean(v):
    if v is None:
        return None
    v = float(v)
    return None if (math.isnan(v) or math.isinf(v)) else v


def parse_period(date, max_stocks=None):
    """解析单个报告期 zip → {code: {field: value}}。纯本地，无缓存。"""
    path = os.path.join(cw_dir(), f"gpcw{date}.zip")
    if not os.path.exists(path):
        return {}, date
    z = zipfile.ZipFile(path)
    dat_name = next(n for n in z.namelist() if n.endswith(".dat"))
    raw = z.read(dat_name)
    # 20 字节头
    _h, report_date, max_count, _pad, report_size = struct.unpack("<1hI1H3L", raw[:20])[:5]
    n_fields = report_size // 4
    wanted = sorted(k for k in FIELDS if k <= n_fields)
    rec_fmt = "<%df" % n_fields
    rec_size = 11  # <6s1c1L
    out = {}
    for i in range(max_count if max_stocks is None else min(max_count, max_stocks)):
        off = 20 + i * rec_size
        code_b = raw[off:off + 6]
        foa = struct.unpack("<L", raw[off + 7:off + 11])[0]
        code = code_b.decode("ascii", errors="replace").strip("\x00 ")
        if not code.isdigit():
            continue
        rec = struct.unpack_from(rec_fmt, raw, foa)
        d = {}
        for k in wanted:
            v = _nan_clean(rec[k - 1])
            if v is not None and v != 0.0:
                d[FIELDS[k]] = v
        if d:
            out[code] = d
    return out, str(report_date)


def load_period(date, refresh=False):
    """解析 + 磁盘缓存（zip 内容带 md5 不可变，缓存无 TTL；字段集变更走 _CACHE_VER）。"""
    fp = os.path.join(CACHE_DIR, f"cw_{_CACHE_VER}_{date}.json")
    if not refresh and os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    data, _rd = parse_period(date)
    if data:
        try:
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, fp)
        except Exception:
            pass
    return data


def history_for(code, n=8):
    """最近 n 个年报(12-31)的该股数据，旧→新。list[{date, ...fields}]，无则 []。"""
    periods = available_periods()
    ann = [p for p in periods if p.endswith("1231")]
    out = []
    for p in reversed(ann):
        d = load_period(p).get(code)
        if d:
            d = dict(d)
            d["date"] = f"{p[:4]}-12-31"
            out.append(d)
        if len(out) >= n:
            break
    out.reverse()
    return out


def latest_annual_roe_map(min_count=2000):
    """最近年报全市场加权 ROE：{code: roe%}，供选股因子横截面使用。"""
    ann = [p for p in available_periods() if p.endswith("1231")]
    for p in reversed(ann):
        data = load_period(p)
        m = {}
        for code, d in data.items():
            roe = d.get("roe_w") or d.get("roe")
            if roe is not None:
                m[code] = roe
        if len(m) >= min_count:
            return m, f"{p[:4]}-12-31"
    return {}, None
