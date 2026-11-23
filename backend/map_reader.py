# -*- coding: utf-8 -*-
"""
map_reader.py — 全市场个股地理/多维分布数据层

数据来源（均为公开接口，多线程 curl 传输 + 磁盘缓存）：
  1. 公司静态属性 —— 东方财富 datacenter F10 公司概况（一次全量分页）：
     代码/名称/注册省份/东财行业(三级)/证监会行业/上市日期，缓存 3 天
  2. 行情快照 —— 腾讯行情 qt.gtimg.cn 批量接口（并发）：
     现价/涨跌幅/成交额/换手率/市盈率/振幅/量比/流通市值/总市值/市净率，缓存 5 分钟
  3. 中国省界 GeoJSON —— 阿里 DataV 公共边界数据（一次性缓存）

维度（前端本地聚合，切换零延迟）：省份/东财行业/证监会行业/上市板块/市值/市盈率/
市净率/价格/涨跌/换手/成交额/振幅/量比/上市年份/股本规模，共 15 个。
"""
import os
import re
import json
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "map_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TTL_QUOTE = 300           # 行情快照 5 分钟
TTL_ATTRS = 3 * 24 * 3600 # 公司属性 3 天
TTL_GEO = 30 * 24 * 3600  # 省界数据 30 天

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 板块名 → GeoJSON 省名（东财 PROVINCE 字段为省级行政区简称/全称混合，统一映射）
PROV_MAP = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省",
    "黑龙江": "黑龙江省", "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省", "河南": "河南省",
    "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省", "海南": "海南省",
    "四川": "四川省", "贵州": "贵州省", "云南": "云南省", "陕西": "陕西省",
    "甘肃": "甘肃省", "青海": "青海省", "台湾": "台湾省",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区", "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区", "香港": "香港特别行政区",
}

F10_COLS = "SECUCODE,SECURITY_NAME_ABBR,PROVINCE,INDUSTRYCSRC1,BOARD_NAME_LEVEL,LISTING_DATE"
A_PREFIX = ("60", "68", "00", "30", "43", "83", "87", "88", "92")


def _curl_text(url, timeout=15, encoding="utf-8"):
    p = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "--compressed", "--max-time", str(timeout),
         "-A", _UA, url],
        capture_output=True, timeout=timeout + 5)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"curl 失败 rc={p.returncode}")
    return p.stdout.decode(encoding, errors="replace")


# ---------- 磁盘缓存 ----------
def _cache_path(name):
    return os.path.join(CACHE_DIR, name)


def _read_cache(name):
    try:
        with open(_cache_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(name, payload):
    tmp = _cache_path(name) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(name))
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _num(v, scale=1.0, nd=2):
    try:
        f = float(v)
        if f != f or f in (1e308, -1e308):
            return None
        return round(f * scale, nd)
    except Exception:
        return None


# ---------- 公司静态属性（datacenter F10，全量分页） ----------
def fetch_attrs(refresh=False):
    cached = _read_cache("attrs.json")
    now = time.time()
    if not refresh and cached and now - cached.get("ts", 0) < TTL_ATTRS:
        return cached
    attrs = {}
    page = 1
    while True:
        url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get"
               f"?reportName=RPT_F10_BASIC_ORGINFO&columns={F10_COLS}"
               f"&pageSize=500&pageNumber={page}&source=HSF10&client=PC")
        try:
            j = json.loads(_curl_text(url, 20))
        except Exception:
            break
        result = (j or {}).get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        for r in rows:
            secu = str(r.get("SECUCODE") or "")
            code = secu.split(".")[0]
            if not re.match(r"^\d{6}$", code):
                continue
            listing = str(r.get("LISTING_DATE") or "")[:10]
            attrs[code] = {
                "n": str(r.get("SECURITY_NAME_ABBR") or ""),
                "prov": str(r.get("PROVINCE") or ""),
                "ind": str(r.get("BOARD_NAME_LEVEL") or "").split("-")[0],
                "ind3": str(r.get("BOARD_NAME_LEVEL") or ""),
                "csrc": str(r.get("INDUSTRYCSRC1") or ""),
                "year": int(listing[:4]) if listing[:4].isdigit() else None,
                "ldate": listing,
            }
        if page >= int(result.get("pages") or 1):
            break
        page += 1
    payload = {"ts": now, "count": len(attrs), "attrs": attrs}
    if len(attrs) > 5000:
        _write_cache("attrs.json", payload)
    return payload


# ---------- 行情快照（腾讯批量并发） ----------
def _code_prefix(code):
    if code.startswith(("60", "68")):
        return "sh" + code
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj" + code
    return "sz" + code


def _parse_quote_line(line):
    f = line.split("~")
    if len(f) < 50 or not f[2]:
        return None
    try:
        return {
            "close": _num(f[3], 1, 3),
            "chg": _num(f[32], 1, 2),
            "amt": _num(f[37], 1e-4, 2),   # 万元 → 亿元
            "tr": _num(f[38], 1, 2),
            "pe": _num(f[39], 1, 2),       # PE(TTM)
            "amp": _num(f[43], 1, 2),      # 振幅%
            "fmv": _num(f[44], 1, 1),      # 流通市值 亿
            "mv": _num(f[45], 1, 1),       # 总市值 亿
            "pb": _num(f[46], 1, 2),
            "lb": _num(f[49], 1, 2),       # 量比
            "ped": _num(f[52], 1, 2),      # 市盈率(动)
        }
    except Exception:
        return None


def fetch_quotes(codes, workers=8, refresh=False):
    """腾讯批量行情，8 线程并发；磁盘缓存 5 分钟。返回 {code: quote}。"""
    cached = _read_cache("quotes.json")
    now = time.time()
    if not refresh and cached and now - cached.get("ts", 0) < TTL_QUOTE:
        return cached.get("quotes", {})
    out = {}
    batch = 80
    batches = [codes[i:i + batch] for i in range(0, len(codes), batch)]

    def fetch_one(batch_codes):
        q = ",".join(_code_prefix(c) for c in batch_codes)
        try:
            txt = _curl_text(f"https://qt.gtimg.cn/q={q}", 15, "gbk")
        except Exception:
            return {}
        res = {}
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            qt = _parse_quote_line(line)
            if qt:
                m = re.search(r"v_(?:sh|sz|bj)(\d{6})=", line)
                if m:
                    res[m.group(1)] = qt
        return res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(fetch_one, batches):
            out.update(res)
    if out:
        _write_cache("quotes.json", {"ts": now, "quotes": out})
    return out


# ---------- GeoJSON ----------
def get_geo():
    path = _cache_path("china_geo.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < TTL_GEO:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    data = json.loads(_curl_text(
        "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json", 30))
    if data.get("features"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data
    return None


# ---------- 主入口 ----------
def _board_of(code):
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith(("43", "83", "87", "88", "920")):
        return "北交所"
    if code.startswith("60"):
        return "沪市主板"
    return "深市主板"


def get_map_stocks(refresh=False):
    """主入口：F10 属性 + 腾讯行情合并为轻量数组（前端本地聚合，切换零延迟）。"""
    attrs_data = fetch_attrs(refresh)
    attrs = attrs_data.get("attrs", {})
    cand = [c for c in attrs if c.startswith(A_PREFIX)]
    quotes = fetch_quotes(cand, refresh=refresh)
    stocks = []
    for code in cand:
        q = quotes.get(code)
        if not q or q.get("close") is None:
            continue  # 无行情（退市/长期停牌）剔除
        a = attrs[code]
        prov = PROV_MAP.get(a["prov"], a["prov"] + ("省" if a["prov"] and not a["prov"].endswith(("市", "省", "自治区", "行政区")) else "")) if a["prov"] else "未知"
        sh = round(q["mv"] / q["close"], 2) if q.get("mv") and q.get("close") else None  # 总股本 亿股
        stocks.append({
            "c": code, "n": a["n"],
            "p": prov, "ind": a["ind"], "ind3": a["ind3"], "csrc": a["csrc"].split("-")[0] if a["csrc"] else "",
            "b": _board_of(code), "year": a["year"], "ldate": a["ldate"],
            "close": q["close"], "chg": q["chg"], "amt": q["amt"], "tr": q["tr"],
            "pe": q["ped"], "pett": q["pe"], "pb": q["pb"], "amp": q["amp"],
            "lb": q["lb"], "mv": q["mv"], "fmv": q["fmv"], "sh": sh,
        })
    return {
        "updated": int(attrs_data.get("ts", time.time())),
        "attr_ts": int(attrs_data.get("ts", 0)),
        "count": len(stocks),
        "stocks": stocks,
    }
