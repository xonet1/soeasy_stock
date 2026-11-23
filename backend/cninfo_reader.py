# -*- coding: utf-8 -*-
"""
cninfo_reader.py — 巨潮资讯网公告证据层（免费、零成本）

定位：给「全市场实控人 / 投资人穿透图谱」补一层**法定披露证据**。
东财 F10（controller_reader）给出结构化的实控人+前十大股东，本模块从巨潮年报原文
抽取同样的事实并附公告链接，形成可追溯的证据链：
  - 年报「股东和实际控制人情况」→ 前十大股东、控股股东/实控人原文表述
  - 权益变动 / 收购报告书 / 详式权益变动 → 持股动机与控制权变更线索

数据源全部为巨潮公开接口（证监会指定披露平台），无需 key、无调用费：
  - orgId 查询：POST /new/information/topSearch/query
  - 公告列表：POST /new/hisAnnouncement/query
  - PDF 正文：GET https://static.cninfo.com.cn/{adjunctUrl}

设计要点：
  - 沪深 orgId 用规则构造（gssh/gssz+6位代码）走快速路径，北交所用 topSearch 兜底；
  - 每只股票独立 JSON 缓存 data/cninfo_cache/cn_{code}.json，TTL 60 天（年报一年一次）；
  - PDF 用 PyMuPDF(fitz) 抽文本，正则解析股东表；断行名称合并、比例/性质/持股数对齐；
  - 「无实控人」如实抽取原文，不用第一大股东冒充实控人；
  - 全程限速 + 重试 + 失败隔离，任何异常都返回 available=False 而非中断；
  - 单股解析限较大的 PDF（>25MB 跳过），避免异常文件拖垮批量任务。
"""
import os
import re
import json
import time
import random
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cninfo_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

ANNOUNCEMENT_API = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
TOPSearch_API = "https://www.cninfo.com.cn/new/information/topSearch/query"
STATIC_BASE = "https://static.cninfo.com.cn/"
TTL = 60 * 24 * 3600
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
_LOCAL = threading.local()
_RETRY = 2
_MAX_PDF_BYTES = 25 * 1024 * 1024

# 公告类别：年报、摘要、一季度/半年报、权益变动、收购报告书
CATEGORY_ANNUAL = "category_ndbg_szsh"
CATEGORY_CHANGE = "category_quyedong_szsh;category_sf_szsh;category_qtdc_szsh"

_NATURE_RE = re.compile(r"(境内非国有法人|境内国有法人|国有法人|境内自然人|境外法人|境外自然人|其他|基金|社保|QFII|一般法人|机构投资者)")


def _session():
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _LOCAL.s = s
    return s


def _stock_names():
    try:
        with open(os.path.join(BASE_DIR, "data", "stock_names.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"cn_{str(code).zfill(6)}.json")


def _read_cache(code):
    try:
        with open(_cache_path(code), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(code, rec):
    path = _cache_path(code)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _column_of(code):
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "9")):
        return "sse"
    if code.startswith(("4", "8")):
        return "bj"
    return "szse"


def _org_id_fast(code):
    """沪深 orgId 规则构造（前缀 + 7 位代码，如 gssz0000651）；
    北交所不规则，返回 None 走 topSearch。"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "9")):
        return "gssh" + code.zfill(7)
    if code.startswith(("4", "8")):
        return None
    return "gssz" + code.zfill(7)


def org_id(code, name=""):
    """查询 orgId：优先 topSearch（权威，部分股票 orgId 不规则），
    失败再用规则构造兜底。返回 (orgId, column)。"""
    code = str(code).zfill(6)
    column = _column_of(code)
    for kw in (code, name):
        if not kw:
            continue
        try:
            r = _session().post(TOPSearch_API, data={"keyWord": kw, "maxNum": 10}, timeout=15)
            rows = r.json() or []
            for x in rows:
                if str(x.get("code")) == code and x.get("orgId"):
                    return x["orgId"], column
        except Exception:
            pass
    return _org_id_fast(code), column


def _fetch_announcements(code, org, column, category, years=3, page_size=30):
    """拉取某类公告列表（返回原始 announcements）。

    注意：巨潮该接口 pageSize 需 >= 30，设小值会静默返回 0 条，故强制下限 30。
    """
    end_year = time.localtime().tm_year
    se_date = f"{end_year - years}-01-01~{end_year}-12-31"
    page_size = max(30, int(page_size))
    data = {
        "pageNum": 1, "pageSize": page_size, "column": column, "tabName": "fulltext",
        "plate": "", "stock": f"{code},{org}", "searchkey": "", "secid": "",
        "category": category, "trade": "", "seDate": se_date,
        "sortName": "", "sortType": "true", "isHLtitle": "true",
    }
    last = None
    for attempt in range(_RETRY + 1):
        try:
            r = _session().post(ANNOUNCEMENT_API, data=data, timeout=20)
            d = r.json()
            return d.get("announcements") or []
        except Exception as e:
            last = e
            if attempt < _RETRY:
                time.sleep(0.4 * (attempt + 1) + random.random() * 0.3)
    return []


def _to_int(v):
    try:
        return int(float(v))
    except Exception:
        return 0


def _pub_date(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts) / 1000))
    except Exception:
        return ""


def _pick_annual(anns):
    """从公告中挑最近一份中文年报（排除英文版/摘要/问询函）。"""
    cands = []
    for a in anns:
        title = str(a.get("announcementTitle") or "")
        sub = str(a.get("announcementTitle") or "")
        if "年度报告" not in title:
            continue
        if any(k in title for k in ("英文", "摘要", "英文版", "取消", "更正后", "问询", "回复")):
            continue
        if not (a.get("adjunctUrl") or "").lower().endswith(".pdf"):
            continue
        year = 0
        m = re.search(r"(20\d{2})\s*年", title)
        if m:
            year = int(m.group(1))
        cands.append((year, _to_int(a.get("announcementTime")), a))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][2]


def _download_pdf(url):
    full = url if url.startswith("http") else STATIC_BASE + url
    r = _session().get(full, timeout=45)
    r.raise_for_status()
    if len(r.content) > _MAX_PDF_BYTES:
        raise ValueError("PDF 超过 25MB，跳过")
    return r.content


def _clean_name(lines, idx, stop_words):
    """从 idx 往前收集跨行的股东名称，处理括号/断行，返回 (name, 下一个可复用位置)。"""
    parts = []
    j = idx
    nature_words = ("境内非国有法人", "境内国有法人", "国有法人", "境内自然人", "境外法人",
                    "境外自然人", "一般法人", "境内一般法人", "国有", "法人", "其他")
    while j >= 0 and len(parts) < 4:
        cand = lines[j]
        if re.fullmatch(r"[\d,\.\-\s]+", cand) or re.fullmatch(r"无|未知|质押|冻结", cand):
            break
        if re.search(r"\d+\.\d+\s*%?", cand):
            break
        if any(w in cand for w in stop_words) and "）" not in cand and ")" not in cand:
            break
        # 纯性质词跳过（继续往前找真正名称），但不终止
        if cand in nature_words or _NATURE_RE.fullmatch(cand):
            j -= 1
            continue
        parts.insert(0, cand)
        joined = "".join(parts)
        has_open = "（" in joined or "(" in joined
        has_close = "）" in joined or ")" in joined
        if has_close and not has_open:
            j -= 1
            continue
        if (has_open and has_close) or (not has_open and not has_close):
            break
        j -= 1
    return "".join(parts).strip(), j


def _extract_shareholders(text):
    """从年报文本抽取前十大股东 (name, ratio, shares, nature)。

    兼容两种披露模板：
      - 深交所：名称 / 性质 / 比例% / 持股数 …
      - 上交所：名称 / 增减 / 持股数 / 比例(无%) / … / 性质（常被拆行）
    统一用「比例数值」做锚点（两种模板都有），向前回溯名称，
    向后近邻找持股数与性质。名称的跨行（「…（有限」+「合伙）」）做合并。
    """
    t = re.sub(r"[ \t]+", " ", text)
    anchor = re.search(r"持股\s*5\s*%以上的股东或前\s*10\s*名股东持股情况|前\s*10\s*名股东持股情况|前十名股东持股情况", t)
    if not anchor:
        return []
    # 严格截断到股东表结束：下一个章节标题出现处，最长 3500 字符
    tail = t[anchor.start():anchor.start() + 3500]
    ends = [m.start() for m in re.finditer(
        r"上述股东关联|无限售条件股东|表决权恢复|控股股东情况|实际控制人情况|优先股股东|前10名无限售|前十名无限售", tail)]
    end = min(ends) if ends else 3000
    seg = tail[:max(end, 600)]
    lines = [ln.strip() for ln in seg.split("\n")]
    lines = [ln for ln in lines if ln]
    stop_words = ("股东名称", "股东性质", "持股比例", "报告期末", "股份状态", "数量",
                  "质押", "冻结", "标记", "变动情况", "持股情况", "无限售", "有限售",
                  "全称", "报告期内增减", "期末持股", "情况表", "流通股东")
    rows = []
    ratio_pat_pct = re.compile(r"(\d{1,3}\.\d{1,2})\s*%")
    ratio_pat_bare = re.compile(r"^(\d{1,3}\.\d{2})$")
    num_pat = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{7,})(?![\d,])")

    for i, ln in enumerate(lines):
        # 比例锚点：带 % 或纯 xx.xx 行（排除日期/代码）
        ratio = None
        m = ratio_pat_pct.search(ln)
        if m:
            ratio = round(float(m.group(1)), 2)
        else:
            m2 = ratio_pat_bare.match(ln.strip())
            if m2:
                ratio = round(float(m2.group(1)), 2)
        if ratio is None or ratio > 100 or ratio <= 0:
            continue
        name, nm_end = _clean_name(lines, i - 1, stop_words)
        if not name or len(name) < 3 or len(name) > 45:
            continue
        if any(x in name for x in stop_words) or "公司" == name:
            continue
        # 有效性校验：排除财务科目等噪声。须像机构名或≤5字自然人名
        noise = ("收入", "利润", "成本", "费用", "资产", "负债", "现金流", "利率", "比率",
                 "增长率", "审计", "事项", "情况", "股权情况", "硕士", "博士", "先生", "女士",
                 "偿还", "报酬", "占比", "合计", "总计", "小计", "比例", "数量", "年度")
        if any(x in name for x in noise):
            continue
        org_kw = ("公司", "合伙", "基金", "银行", "集团", "中心", "计划", "有限", "股份", "投资",
                 "资管", "证券", "保险", "信托", "社", "QFII", "法人", "管理局", "财政", "大学", "研究院")
        is_org = any(x in name for x in org_kw)
        is_person = re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", name) is not None
        if not (is_org or is_person):
            continue
        # 持股数：优先锚点前一行（上交所：增减/持股数在前），其次后向 6 行
        shares = ""
        for k in range(i - 1, max(-1, i - 4), -1):
            s = num_pat.search(lines[k])
            if s and s.group(1).count(",") >= 2:
                shares = s.group(1)
                break
        if not shares:
            for k in range(i + 1, min(i + 7, len(lines))):
                s = num_pat.search(lines[k])
                if s and s.group(1).count(",") >= 2:
                    shares = s.group(1)
                    break
        # 性质：前向 6 行拼接（沪市「国有」+「法人」分两行）
        nature = ""
        window = " ".join(lines[max(0, i - 7):i + 8])
        nm = _NATURE_RE.search(window)
        if nm:
            nature = nm.group(1)
        elif "国有" in window and "法人" in window:
            nature = "国有法人"
        rows.append({"name": name, "ratio": ratio, "shares": shares, "nature": nature})

    seen, uniq = set(), []
    for r in rows:
        key = r["name"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    # 按持股比例降序，保留前 12
    uniq.sort(key=lambda x: -x["ratio"])
    return uniq[:12]


def _extract_controller(text):
    """抽取控股股东/实控人原文表述，含「无实际控制人」这类否定结论。"""
    t = re.sub(r"[ \t]+", " ", text)
    results = []
    pats = [
        r"公司(?:无|没有)(?:控股股东|实际控制人)[^。]{0,60}。",
        r"无控股股东[^。]{0,60}。",
        r"无实际控制人[^。]{0,60}。",
        r"公司(?:的)?实际控制人(?:为|是)[^。]{0,80}。",
        r"公司(?:的)?控股股东(?:为|是)[^。]{0,80}。",
        r"实际控制人(?:为|是)[^。]{0,80}。",
        r"控股股东(?:为|是)[^。]{0,80}。",
    ]
    for p in pats:
        for m in re.finditer(p, t):
            s = m.group(0).strip()
            if s not in results:
                results.append(s)
            if len(results) >= 6:
                return results
    return results


def _extract_holder_count(text):
    t = re.sub(r"[ \t]+", " ", text)
    m = re.search(r"报告期末普通股股东总数\s*([\d,]+)", t)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except Exception:
            pass
    return None


def _parse_annual_pdf(pdf_bytes):
    """用 PyMuPDF 抽文本并解析股东/实控人。"""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = min(doc.page_count, 260)
    # 全文但需要逐页定位关键章节；先拼全文再正则，控制在合理长度
    chunks = []
    for i in range(pages):
        try:
            chunks.append(doc[i].get_text())
        except Exception:
            continue
    doc.close()
    text = "\n".join(chunks)
    return {
        "shareholders": _extract_shareholders(text),
        "controller_texts": _extract_controller(text),
        "holder_count": _extract_holder_count(text),
        "pages_parsed": pages,
    }


def fetch_one(code, name="", refresh=False, with_pdf=True):
    """采集单只股票的巨潮公告证据。任何异常都返回 available=False 的降级结构。"""
    code = str(code).zfill(6)
    cached = None if refresh else _read_cache(code)
    if cached and time.time() - cached.get("fetched_at", 0) < TTL:
        return cached
    rec = {
        "code": code, "name": name or _stock_names().get(code, ""),
        "fetched_at": time.time(), "available": False,
        "org_id": "", "annual": None, "shareholders": [],
        "controller_texts": [], "holder_count": None,
        "change_announcements": [], "source_urls": [], "note": "",
    }
    try:
        org, column = org_id(code, rec["name"])
        if not org:
            rec["note"] = "orgId 未找到（北交所/新代码），可稍后重试"
            _write_cache(code, rec)
            return rec
        rec["org_id"] = org
        # 年报列表
        anns = _fetch_announcements(code, org, column, CATEGORY_ANNUAL, years=4)
        annual = _pick_annual(anns)
        # 权益变动列表
        changes = _fetch_announcements(code, org, column, CATEGORY_CHANGE, years=4, page_size=20)
        change_rows = []
        for a in changes[:12]:
            change_rows.append({
                "title": str(a.get("announcementTitle") or ""),
                "date": _pub_date(a.get("announcementTime")),
                "url": STATIC_BASE + str(a.get("adjunctUrl") or ""),
            })
        rec["change_announcements"] = change_rows
        rec["source_urls"] = [x["url"] for x in change_rows[:6]]
        if annual:
            aurl = STATIC_BASE + str(annual.get("adjunctUrl") or "")
            rec["annual"] = {
                "title": str(annual.get("announcementTitle") or ""),
                "date": _pub_date(annual.get("announcementTime")),
                "url": aurl,
            }
            rec["source_urls"].insert(0, aurl)
            if with_pdf:
                try:
                    pdf = _download_pdf(str(annual.get("adjunctUrl") or ""))
                    parsed = _parse_annual_pdf(pdf)
                    rec["shareholders"] = parsed["shareholders"]
                    rec["controller_texts"] = parsed["controller_texts"]
                    rec["holder_count"] = parsed["holder_count"]
                    rec["pages_parsed"] = parsed["pages_parsed"]
                except Exception as e:
                    rec["note"] = f"PDF 解析失败：{str(e)[:100]}"
        else:
            rec["note"] = "近 4 年未找到中文年报公告"
        rec["available"] = bool(annual or change_rows)
        _write_cache(code, rec)
        return rec
    except Exception as e:
        rec["note"] = f"采集异常：{str(e)[:150]}"
        return rec


def get_cached(code):
    """只读缓存，不触发网络；供图谱/详情快速引用。"""
    code = str(code).zfill(6)
    rec = _read_cache(code)
    if rec and time.time() - rec.get("fetched_at", 0) < TTL:
        return rec
    return None


def scan_all(codes, workers=4, delay=0.12, limit=None, progress_cb=None, with_pdf=True):
    """批量补采公告证据。断点续采：新鲜缓存跳过。"""
    codes = [str(c).zfill(6) for c in (codes or [])]
    if limit:
        codes = codes[:int(limit)]
    todo, cached_n = [], 0
    for c in codes:
        cached = get_cached(c)
        if cached:
            cached_n += 1
        else:
            todo.append(c)
    total = len(todo)
    stats = {"total": total, "cached": cached_n, "done": 0, "failed": 0,
             "with_annual": 0, "errors": []}
    if not total:
        if progress_cb:
            progress_cb(0, 0, "公告证据全部命中缓存")
        return stats

    def task(code):
        time.sleep(random.random() * delay)
        return code, fetch_one(code, refresh=False, with_pdf=with_pdf)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = [ex.submit(task, c) for c in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                code, rec = fut.result()
                if rec and rec.get("available"):
                    stats["done"] += 1
                    if rec.get("annual"):
                        stats["with_annual"] += 1
                else:
                    stats["failed"] += 1
                    if rec:
                        stats["errors"].append({"code": code, "note": rec.get("note", "")})
            except Exception:
                stats["failed"] += 1
            if progress_cb and (i % 10 == 0 or i == total):
                progress_cb(i, total, f"公告证据 {i}/{total}，成功 {stats['done']}，跳过缓存 {cached_n}")
    return stats


def evidence_summary(code):
    """给控制链用的精简证据：最近年报标题/日期/链接 + 实控人原文 + 股东数。"""
    rec = get_cached(code)
    if not rec:
        return None
    return {
        "available": rec.get("available", False),
        "annual": rec.get("annual"),
        "controller_texts": (rec.get("controller_texts") or [])[:3],
        "shareholder_count": len(rec.get("shareholders") or []),
        "holder_count": rec.get("holder_count"),
        "change_count": len(rec.get("change_announcements") or []),
        "source_url": (rec.get("source_urls") or [None])[0],
        "fetched_at": rec.get("fetched_at"),
    }


def status():
    """缓存覆盖统计。"""
    total = with_annual = with_pdf = 0
    for fn in os.listdir(CACHE_DIR):
        if not re.match(r"cn_\d{6}\.json$", fn):
            continue
        total += 1
        try:
            with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("annual"):
                with_annual += 1
            if rec.get("shareholders"):
                with_pdf += 1
        except Exception:
            pass
    return {"cached": total, "with_annual": with_annual, "with_shareholders": with_pdf,
            "cache_dir": CACHE_DIR}
