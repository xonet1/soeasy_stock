# -*- coding: utf-8 -*-
"""
holder_reader.py — 个股前十大股东数据层（合规采集 + 深度分析）

【合规采集原则】
  1. 数据源仅为东方财富 F10 公开披露页面（投资者无需登录即可查看），
     底层为上市公司法定披露的定期报告，不涉及任何登录/私有/付费墙数据。
  2. 限速：全局最小请求间隔 + 单站点节流，避免高频请求。
  3. 重试：指数退避，失败不轰炸；缓存优先，减少重复请求。
  4. 用途：仅个人学习研究，不分发、不商用、不构成投资建议。
  5. 审计：记录请求时间/接口/结果到本地日志，便于追溯。

【数据源】akshare → 东方财富 F10 股东研究
  - stock_gdfx_top_10_em       十大股东（占总股本比例）
  - stock_gdfx_free_top_10_em  十大流通股东（占流通股本比例、股东性质）

【深度分析】基于多期真实披露数据的时间序列统计，推断项均标注来源：
  - 控制权结构、股东性质分类、新进/退出追踪、连续增减持、
    集中度曲线、增减持强度信号。

仅供个人学习研究，非投资建议。
"""
import os
import re
import json
import time
import random
import threading
import datetime as dt
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "holder_cache")
AUDIT_LOG = os.path.join(BASE_DIR, "data", "holder_fetch_audit.log")
os.makedirs(CACHE_DIR, exist_ok=True)
TTL = 7 * 24 * 3600       # 季度数据 7 天缓存
HISTORY_TTL = 7 * 24 * 3600
_LOCK = threading.RLock()

# ---------- 合规采集参数 ----------
MIN_INTERVAL = 1.2        # 相邻网络请求最小间隔（秒）
MAX_RETRIES = 2           # 单请求最大重试次数
BACKOFF_BASE = 1.5        # 指数退避基数
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 PersonalResearch/1.0")

FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")

_last_request = {"t": 0.0}


def is_fund(code):
    return str(code).zfill(6).startswith(FUND_PREFIXES)


def _ak():
    try:
        import akshare as ak
        return ak
    except Exception as e:
        raise RuntimeError(f"akshare 不可用：{e}")


def _market_prefix(code):
    code = str(code).zfill(6)
    if code[0] in ("6", "9"):
        return "SH" + code
    if code[0] == "8" or code.startswith("4"):
        return "BJ" + code
    return "SZ" + code


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _num(v):
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace(",", "")
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


# ---------- 合规：限速 + 审计 ----------
def _throttle():
    """全局最小请求间隔，避免对公开数据源造成压力。"""
    with _LOCK:
        now = time.time()
        wait = MIN_INTERVAL - (now - _last_request["t"])
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.2))  # 轻微抖动
        _last_request["t"] = time.time()


def _audit(endpoint, code, ok, detail=""):
    """本地请求审计日志（不上传）。"""
    try:
        line = (f"{dt.datetime.now().isoformat(timespec='seconds')}\t{endpoint}\t{code}\t"
                f"{'OK' if ok else 'FAIL'}\t{detail[:80]}\n")
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _compliant_call(fn, endpoint, code):
    """合规调用：限速 + 指数退避重试 + UA + 审计。"""
    if hasattr(fn, "__globals__") and "requests" in fn.__globals__:
        try:
            sess = fn.__globals__["requests"]
            if hasattr(sess, "utils"):
                sess.utils.default_headers()["User-Agent"] = UA
        except Exception:
            pass
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            df = fn()
            _audit(endpoint, code, True, f"rows={0 if df is None else len(df)}")
            return df
        except Exception as e:
            last_err = e
            _audit(endpoint, code, False, f"attempt={attempt + 1} {type(e).__name__}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt + random.uniform(0, 0.3))
    raise last_err


# ---------- 缓存 ----------
def _cache_path(kind, code):
    return os.path.join(CACHE_DIR, f"{kind}_{code}.json")


def _read_cache(kind, code):
    try:
        with open(_cache_path(kind, code), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(kind, code, payload):
    tmp = _cache_path(kind, code) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(kind, code))
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _safe_fetch(kind, code, fn, refresh=False, ttl=TTL):
    code = str(code).zfill(6)
    if refresh:
        try:
            os.remove(_cache_path(kind, code))
        except Exception:
            pass
    cached = _read_cache(kind, code)
    now = time.time()
    if cached and not cached.get("stale") and now - cached.get("fetched_at", 0) < ttl:
        return cached
    with _LOCK:
        cached = _read_cache(kind, code)
        if cached and not cached.get("stale") and now - cached.get("fetched_at", 0) < ttl:
            return cached
        try:
            data = fn()
            payload = {"fetched_at": time.time(), "stale": False, "data": data}
            _write_cache(kind, code, payload)
            return payload
        except Exception as e:
            if cached and cached.get("data") is not None:
                stale = dict(cached)
                stale["stale"] = True
                stale["error"] = str(e)[:200]
                return stale
            return {"fetched_at": 0, "stale": False, "data": None, "error": str(e)[:200]}


# ---------- 报告期 ----------
def _report_periods(n=8):
    """从当前日期往前生成最近 n 个季末报告期（YYYYMMDD）。"""
    today = dt.date.today()
    year, month = today.year, today.month
    periods = []
    for i in range(n + 2):
        q_end_month = ((month - 1) // 3) * 3 + 3 - i * 3
        y = year
        while q_end_month <= 0:
            q_end_month += 12
            y -= 1
        day = {3: 31, 6: 30, 9: 30, 12: 31}[q_end_month]
        periods.append(f"{y}{q_end_month:02d}{day:02d}")
    return list(dict.fromkeys(periods))


def _fetch_top(code, prefix, date, free=False):
    """抓单个报告期的股东表并标准化（经合规调用层）。"""
    ak = _ak()
    if free:
        fn = lambda: ak.stock_gdfx_free_top_10_em(symbol=prefix, date=date)
        endpoint = "gdfx_free_top_10"
    else:
        fn = lambda: ak.stock_gdfx_top_10_em(symbol=prefix, date=date)
        endpoint = "gdfx_top_10"
    df = _compliant_call(fn, endpoint, code)
    if df is None or df.empty:
        return []
    ratio_col = "占总流通股本持股比例" if free else "占总股本持股比例"
    out = []
    for _, r in df.iterrows():
        chg_raw = _s(r.get("增减"))
        chg_num = _int(chg_raw) if chg_raw not in ("", "不变", "-") else None
        out.append({
            "rank": _int(r.get("名次")),
            "name": _s(r.get("股东名称")),
            "nature": _s(r.get("股东性质")),
            "share_type": _s(r.get("股份类型")),
            "shares": _int(r.get("持股数")),
            "ratio": round(_num(r.get(ratio_col)) or 0, 4),
            "change": chg_num,
            "change_raw": chg_raw or "不变",
            "change_ratio": round(_num(r.get("变动比率")) or 0, 4),
        })
    return out


def _detect_and_fetch(code, prefix, free=False):
    """从最新报告期往前探测，返回 (report_date, items, err)。"""
    last_err = None
    for d in _report_periods():
        try:
            items = _fetch_top(code, prefix, d, free)
            if items:
                return d, items, None
        except Exception as e:
            last_err = str(e)[:150]
            continue
    return "", [], last_err


def _summarize(items, ratio_key="ratio"):
    if not items:
        return {"cr5": None, "cr10": None, "increase": 0, "decrease": 0,
                "unchanged": 0, "nature_dist": {}}
    ratios = [x.get(ratio_key) or 0 for x in items]
    inc = sum(1 for x in items if (x.get("change") or 0) > 0)
    dec = sum(1 for x in items if (x.get("change") or 0) < 0)
    unc = len(items) - inc - dec
    nature = {}
    for x in items:
        n = x.get("nature") or "未分类"
        nature[n] = nature.get(n, 0) + 1
    return {
        "cr5": round(sum(ratios[:5]), 2),
        "cr10": round(sum(ratios[:10]), 2),
        "increase": inc, "decrease": dec, "unchanged": unc,
        "nature_dist": nature,
    }


# ---------- 基础接口 ----------
def fetch_holders(code, refresh=False):
    """十大股东 + 十大流通股东 + 集中度（快速接口）。"""
    code = str(code).zfill(6)
    if is_fund(code):
        return {
            "code": code, "is_fund": True,
            "message": "该代码为场内基金/ETF，属于一篮子证券，无单一公司的前十大股东。请输入个股代码。",
            "top": [], "free_top": [], "summary": {}, "report_date": "", "sources": [],
        }
    prefix = _market_prefix(code)

    def fn():
        d1, top, e1 = _detect_and_fetch(code, prefix, free=False)
        d2, free_top, e2 = _detect_and_fetch(code, prefix, free=True)
        if not top and not free_top:
            raise RuntimeError(f"未获取到股东数据：{e1 or e2 or '接口返回空'}")
        return {
            "report_date": d1 or d2,
            "top": top, "free_top": free_top,
            "summary": {"top": _summarize(top), "free_top": _summarize(free_top)},
        }

    cached = _safe_fetch("holders", code, fn, refresh)
    data = cached.get("data") or {}
    sources = [{
        "key": "top", "label": "东方财富F10·十大股东",
        "ok": bool(data.get("top")), "stale": bool(cached.get("stale")),
        "fetched_at": cached.get("fetched_at", 0), "error": cached.get("error"),
    }]
    return {
        "code": code, "is_fund": False, "name": "",
        "report_date": data.get("report_date", ""),
        "top": data.get("top", []), "free_top": data.get("free_top", []),
        "summary": data.get("summary", {}), "sources": sources,
        "disclaimer": "股东数据来自东方财富F10公开披露（报告期见标注），存在披露滞后；集中度与增减持为基于披露数据的统计，非投资建议。",
    }


# ---------- 深度分析 ----------
# 股东名称 → 性质分类（规则库，标注为推断）
_NATURE_RULES = [
    ("社保基金", ("社保基金", "社会保障基金", "全国社保")),
    ("公募基金", ("基金管理有限公司", "证券投资基金", "公募", "ETF", "交易型开放式指数")),
    ("外资/陆股通", ("香港中央结算", "QFII", "摩根", "高盛", "花旗", "瑞银", "UBS", "美林", "富通")),
    ("保险资金", ("保险", "人寿", "财险", "平安保险")),
    ("产业资本/实体企业", ("服饰", "服装", "纺织", "实业", "制造", "食品", "酒业", "酒厂",
                     "家电", "电器", "地产", "置业", "汽车", "医药", "制药", "电子",
                     "商贸", "贸易", "矿业", "能源", "化工", "建材", "农业", "乳业")),
    ("证券公司", ("证券股份有限公司", "证券公司", "中信证券", "国泰君安")),
    ("银行/理财", ("银行股份有限公司", "理财")),
    ("信托", ("信托",)),
    ("国资/产业集团", ("国有资产", "国资", "集团有限", "控股集团", "国有资本", "投资控股", "产业发展", "科技集团")),
    ("员工持股/高管", ("员工持股计划", "股权激励")),
    ("私募/创投", ("有限合伙", "创业投资", "投资基金合伙", "资产管理", "投资管理")),
]


def _classify_nature(name, raw_nature=""):
    """基于股东名称的规则分类；raw_nature 若非空优先采用（F10 已有字段）。"""
    n = _s(name)
    if not n:
        return "未知"
    if raw_nature and raw_nature not in ("未分类", "其它", "其他"):
        return raw_nature
    for label, kws in _NATURE_RULES:
        for kw in kws:
            if kw in n:
                return label
    # 2-4 位纯中文姓名视为自然人
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", n):
        return "自然人"
    return "其他机构/个人"


# 股东主营业务行业识别（仅用于"是否同行/跨界"的粗略判断，非工商经营范围核验）
_INDUSTRY_KWS = [
    ("服装服饰", ("服饰", "服装", "纺织", "雅戈尔", "海澜", "杉杉")),
    ("家电电器", ("家电", "电器", "格力", "美的", "海尔", "奥克斯")),
    ("地产建筑", ("地产", "置业", "房产", "建设", "建筑", "土木")),
    ("汽车交通", ("汽车", "车业", "轮胎", "航空", "物流")),
    ("医药医疗", ("医药", "制药", "生物", "医疗", "药业")),
    ("食品饮料", ("食品", "饮料", "酒业", "乳业", "农牧", "种业")),
    ("电子半导体", ("电子", "半导体", "芯片", "光电", "集成电路")),
    ("能源化工", ("能源", "石油", "化工", "矿业", "煤炭", "电力")),
    ("商贸零售", ("商贸", "贸易", "百货", "商业", "零售")),
]


def _stock_name(code):
    """从本地名称缓存读取股票简称（不依赖 tdx_reader/numpy）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "stock_names.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str(json.load(f).get(str(code).zfill(6), ""))
    except Exception:
        return ""


def _industry_of(name):
    n = _s(name)
    for label, kws in _INDUSTRY_KWS:
        if any(k in n for k in kws):
            return label
    return ""


def _short_name(name):
    """提取股东核心字号（去掉地域/行业/组织形式），用于舆情关键词匹配。"""
    n = _s(name)
    for tok in ("股份有限公司", "有限责任公司", "有限公司", "有限合伙", "普通合伙",
                "投资管理", "资产管理", "沪", "深", "北京", "上海", "深圳", "广州", "杭州",
                "宁波", "珠海", "天津", "重庆", "江苏", "浙江", "广东", "中国"):
        n = n.replace(tok, "")
    for tok in ("服饰", "服装", "纺织", "实业", "集团", "控股", "科技", "电子", "医药",
                "地产", "置业", "汽车", "电器", "家电", "贸易", "商贸", "投资", "发展"):
        n = n.replace(tok, "")
    m = re.search(r"[\u4e00-\u9fa5]{2,6}", n)
    return m.group(0) if m else _s(name)[:8]


def notable_events(top_items, company_industry=""):
    """从最新一期前十大股东中提取值得关注的变动事件（新进/产业资本/显著性变动）。

    返回 [{name, short, nature, industry, ratio, change, is_new, importance, tags, reading}]。
    reading 为规则推断，明确非公司披露动机。
    """
    out = []
    for x in (top_items or []):
        name = _s(x.get("name"))
        if not name:
            continue
        nature = _classify_nature(name, x.get("nature", ""))
        change = _s(x.get("change_raw"))
        is_new = "新进" in change
        ratio = x.get("ratio") or 0
        industry = _industry_of(name)
        # 只关注：新进 / 产业资本 / 国资 / 自然人（剔除纯财务机构的常规进出噪音）
        if not (is_new or nature in ("产业资本/实体企业", "国资/产业集团", "自然人")):
            continue
        tags = [nature] if nature else []
        if is_new:
            tags.append("新进")
        if industry:
            tags.append(industry)
        if industry and company_industry and industry != company_industry:
            tags.append("跨界持股")
        elif industry and company_industry and industry == company_industry:
            tags.append("同业持股")

        read = []
        if is_new:
            read.append("本期新进前十大股东")
        if nature == "产业资本/实体企业":
            if "跨界持股" in tags:
                read.append(f"属{industry}领域实体企业，与标的所处{company_industry}不同业，通常偏财务投资视角")
            elif "同业持股" in tags:
                read.append(f"属{industry}同行业产业资本，关注是否存在产业协同或整合预期")
            else:
                read.append("实体产业资本持股，关注其长期配置意图")
        elif nature == "自然人":
            read.append("自然人股东，关注后续增减持与持股稳定性")
        elif nature == "国资/产业集团":
            read.append("国资/产业集团持股，通常具长期属性")
        if ratio >= 3:
            read.append(f"持股比例较高（{ratio}%），对股东结构影响较显著")
        out.append({
            "name": name, "short": _short_name(name), "nature": nature,
            "industry": industry, "ratio": round(ratio, 2), "change": change,
            "is_new": is_new,
            "importance": "high" if (is_new and nature in ("产业资本/实体企业", "国资/产业集团")) else "medium",
            "tags": tags,
            "reading": "；".join(read) + "（基于名称规则与行业关键词的推断，非公司披露的持股动机）",
        })
    out.sort(key=lambda e: (-int(e["importance"] == "high"), -e["ratio"]))
    return out


def _fetch_history(code, prefix, periods=8, refresh=False, progress_cb=None):
    """抓取多期历史（经合规层），构建时间序列。返回 [{date, top, free_top}, ...] 由新到旧。"""
    def fn():
        out = []
        dates = _report_periods(periods)
        for i, d in enumerate(dates):
            try:
                top = _fetch_top(code, prefix, d, free=False)
                free = _fetch_top(code, prefix, d, free=True)
                if top or free:
                    out.append({"date": d, "top": top, "free_top": free})
                if progress_cb:
                    progress_cb(i + 1, len(dates), f"采集 {d[:4]}Q{(int(d[4:6]) - 1) // 3 + 1} 报告期")
            except Exception:
                # 单期失败不中断整体，符合"失败不轰炸"原则
                if progress_cb:
                    progress_cb(i + 1, len(dates), f"{d} 无披露数据，跳过")
                continue
        if not out:
            raise RuntimeError("未获取到任何历史报告期数据")
        return out
    cached = _safe_fetch("history", code, fn, refresh, ttl=HISTORY_TTL)
    return cached


def _analyze_control(latest_top):
    """控制权结构分析（基于持股比例，标注为统计推断）。"""
    if not latest_top:
        return {}
    first = latest_top[0]
    r1 = first.get("ratio") or 0
    second = latest_top[1].get("ratio") if len(latest_top) > 1 else 0
    third = latest_top[2].get("ratio") if len(latest_top) > 2 else 0
    if r1 >= 50:
        level, desc = "绝对控股", "第一大股东持股超50%，控制权稳固"
    elif r1 >= 30:
        level, desc = "相对控股", "第一大股东持股30%-50%，控制权较强"
    elif r1 >= 20:
        level, desc = "相对分散", "第一大股东持股20%-30%，存在制衡"
    else:
        level, desc = "高度分散", "第一大股东持股低于20%，无单一控股方"
    gap = round(r1 - second, 2)
    if gap >= 15:
        balance = "一超多强，控制权清晰"
    elif gap >= 5:
        balance = "领先明显，有一定制衡"
    else:
        balance = "股权接近，制衡较强"
    return {
        "top1_name": first.get("name", ""),
        "top1_ratio": r1,
        "top2_ratio": second,
        "top3_ratio": third,
        "gap_1_2": gap,
        "control_level": level,
        "control_desc": desc,
        "balance": balance,
        "nature": _classify_nature(first.get("name", ""), first.get("nature", "")),
    }


def _analyze_trends(history, key="top"):
    """多期股东变化：新进/退出、连续增减持、集中度曲线、分类分布演变。"""
    series = []
    for h in history:
        items = h.get(key) or []
        s = _summarize(items)
        nature_ratio = {}
        for x in items:
            cat = _classify_nature(x.get("name", ""), x.get("nature", ""))
            nature_ratio[cat] = nature_ratio.get(cat, 0) + (x.get("ratio") or 0)
        series.append({
            "date": h.get("date", ""),
            "cr5": s["cr5"], "cr10": s["cr10"],
            "nature_ratio": {k: round(v, 2) for k, v in nature_ratio.items()},
            "names": [x.get("name", "") for x in items],
            "count": len(items),
        })
    # 最近一期 vs 上一期：新进/退出
    entrants, exits = [], []
    if len(series) >= 2:
        cur, prev = set(series[0]["names"]), set(series[1]["names"])
        entrants = sorted(cur - prev)
        exits = sorted(prev - cur)
    # 连续增减持：按股东名统计最近 N 期中 change>0 / <0 的次数
    changes = {}
    for h in history[:6]:
        for x in (h.get(key) or []):
            nm = x.get("name", "")
            if not nm:
                continue
            d = changes.setdefault(nm, {"up": 0, "down": 0, "last_change": None, "last_ratio": 0})
            c = x.get("change")
            if c is not None:
                if c > 0:
                    d["up"] += 1
                elif c < 0:
                    d["down"] += 1
                d["last_change"] = c
            d["last_ratio"] = x.get("ratio") or 0
    continuous_add = [{"name": k, **v} for k, v in changes.items() if v["up"] >= 2 and v["up"] > v["down"]]
    continuous_cut = [{"name": k, **v} for k, v in changes.items() if v["down"] >= 2 and v["down"] > v["up"]]
    continuous_add.sort(key=lambda x: -x["up"])
    continuous_cut.sort(key=lambda x: -x["down"])
    return {
        "series": series,
        "entrants": entrants[:12],
        "exits": exits[:12],
        "continuous_add": continuous_add[:8],
        "continuous_cut": continuous_cut[:8],
    }


def _build_insights(control, trend_top, trend_free, latest_top, summary):
    """生成深度洞察（全部基于真实数据计算，措辞谨慎，标注统计口径）。"""
    notes = []
    # 控制权
    if control:
        notes.append({
            "type": "control", "tone": "info",
            "text": f"第一大股东{control['top1_name'][:18]}持股{control['top1_ratio']}%，"
                    f"与第二大股东相差{control['gap_1_2']}个百分点，呈「{control['control_level']}」格局（{control['balance']}）。",
        })
    # 集中度趋势
    cr = trend_top.get("series", [])
    if len(cr) >= 2:
        d0, d1 = cr[0], cr[-1]
        if d0.get("cr5") is not None and d1.get("cr5") is not None:
            delta = round(d0["cr5"] - d1["cr5"], 2)
            if abs(delta) >= 0.3:
                direction = "上升" if delta > 0 else "下降"
                notes.append({
                    "type": "concentration", "tone": "info" if delta > 0 else "warn",
                    "text": f"前五大股东合计持股由{d1['cr5']}%（{d1['date'][:6]}）变为{d0['cr5']}%（{d0['date'][:6]}），"
                            f"{direction}{abs(delta)}个百分点，筹码趋于{'集中' if delta > 0 else '分散'}。",
                })
    # 新进/退出
    ent, ex = trend_top.get("entrants", []), trend_top.get("exits", [])
    if ent:
        notes.append({"type": "entrant", "tone": "good",
                      "text": f"本期新进前十大：{'、'.join(x[:14] for x in ent[:4])}{' 等' if len(ent) > 4 else ''}。"})
    if ex:
        notes.append({"type": "exit", "tone": "warn",
                      "text": f"本期退出前十大：{'、'.join(x[:14] for x in ex[:4])}{' 等' if len(ex) > 4 else ''}。"})
    # 连续增减持
    ca, cc = trend_top.get("continuous_add", []), trend_top.get("continuous_cut", [])
    if ca:
        notes.append({"type": "add", "tone": "good",
                      "text": f"连续增持（近6期）：{'、'.join(x['name'][:14] for x in ca[:3])}。"})
    if cc:
        notes.append({"type": "cut", "tone": "warn",
                      "text": f"连续减持（近6期）：{'、'.join(x['name'][:14] for x in cc[:3])}。"})
    # 外资/陆股通
    for x in (latest_top or []):
        if "香港中央结算" in x.get("name", ""):
            notes.append({"type": "foreign", "tone": "info",
                          "text": f"香港中央结算（陆股通）持股{x.get('ratio')}%，本期变动{x.get('change_raw', '不变')}。"})
            break
    # 减持强度信号
    if summary:
        inc, dec = summary.get("increase", 0), summary.get("decrease", 0)
        if dec >= 3:
            notes.append({"type": "signal", "tone": "warn",
                          "text": f"本期前十大股东中{inc}家增持、{dec}家减持，减持家数偏多，关注筹码松动风险。"})
    if not notes:
        notes.append({"type": "none", "tone": "info", "text": "数据期数较少，暂不足以形成趋势性洞察。"})
    return notes


def fetch_deep(code, refresh=False, periods=8, progress_cb=None):
    """深度分析主入口：多期历史 + 控制权 + 趋势 + 洞察。"""
    code = str(code).zfill(6)
    if is_fund(code):
        return {
            "code": code, "is_fund": True,
            "message": "该代码为场内基金/ETF，属于一篮子证券，无单一公司的股东结构。请输入个股代码。",
        }
    prefix = _market_prefix(code)
    hist_bundle = _fetch_history(code, prefix, periods=periods, refresh=refresh, progress_cb=progress_cb)
    history = hist_bundle.get("data") or []
    if not history:
        raise RuntimeError(hist_bundle.get("error") or "未获取到历史数据")
    latest = history[0]
    latest_top = latest.get("top") or []
    latest_free = latest.get("free_top") or []
    control = _analyze_control(latest_top)
    trend_top = _analyze_trends(history, "top")
    trend_free = _analyze_trends(history, "free_top")
    summary = _summarize(latest_top)
    insights = _build_insights(control, trend_top, trend_free, latest_top, summary)
    # 重点股东变动：产业资本新进/跨界持股等，给出基于规则的可解释画像
    company_name = _stock_name(code)
    notable = notable_events(latest_top, _industry_of(company_name))
    for ev in notable:
        if ev["importance"] == "high":
            insights.append({
                "type": "notable", "tone": "info",
                "text": f"重点关注：{ev['name']}（{'、'.join(ev['tags']) or '未分类'}，持股{ev['ratio']}%）"
                        f"——{ev['reading']}",
            })
    # 股东分类分布（最近一期，按持股比例）
    cat_dist = {}
    for x in latest_top:
        cat = _classify_nature(x.get("name", ""), x.get("nature", ""))
        cat_dist[cat] = round(cat_dist.get(cat, 0) + (x.get("ratio") or 0), 2)
    return {
        "code": code, "is_fund": False,
        "report_date": latest.get("date", ""),
        "periods": len(history),
        "control": control,
        "summary": summary,
        "category_dist": {k: v for k, v in sorted(cat_dist.items(), key=lambda x: -x[1])},
        "trend_top": trend_top,
        "trend_free": trend_free,
        "notable_events": notable,
        "company_name": company_name,
        "insights": insights,
        "latest_top": latest_top,
        "latest_free": latest_free,
        "fetched_at": hist_bundle.get("fetched_at", 0),
        "stale": bool(hist_bundle.get("stale")),
        "disclaimer": ("控制权/分类/趋势均为基于公开披露数据的统计与规则推断，"
                       "股东关联关系需以公司公告为准；数据存在披露滞后，非投资建议。"),
    }


def fetch_status(code):
    """合规采集状态：缓存时间、审计日志是否存在。"""
    code = str(code).zfill(6)
    cached = _read_cache("holders", code)
    hist = _read_cache("history", code)
    return {
        "holders_cache_at": cached.get("fetched_at", 0) if cached else 0,
        "history_cache_at": hist.get("fetched_at", 0) if hist else 0,
        "audit_log": os.path.exists(AUDIT_LOG),
        "min_interval": MIN_INTERVAL,
        "max_retries": MAX_RETRIES,
    }
