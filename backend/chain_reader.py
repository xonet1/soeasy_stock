# -*- coding: utf-8 -*-
"""
chain_reader.py — 个股上下游产业链穿透数据层

数据分层，严格区分来源（前端会逐条标注）：
  【真实数据】可溯源的一手/公开接口数据：
    - 公司资料（所属行业、主营业务、经营范围、入选指数）—— 巨潮资讯 stock_profile_cninfo
    - 业务简介（产品类型、产品名称）—— 同花顺 stock_zyjs_ths
    - 主营构成（按产品/行业/地区收入占比、毛利率）—— 东方财富 F10 stock_zygc_em
    - 概念板块归属 + 核心题材要点 —— 东方财富 F10 CoreConception
    - 关联公司（上游/下游/同行）—— 东方财富板块成分股（真实名单），方向由板块语义规则映射（规则推断需核实）
    - 合同/中标/订单公告 —— 巨潮资讯网公告标题检索（真实一手披露）
  【AI 推断】基于真实数据 + 产业链常识的推断，非一手数据，必须标注"需核实"
    - 从主营业务/产品名称/行业关键词，映射到产业链上中下游环节与关联方向
    - 通过 knowledge/chain_kb.json 可扩充人工/AI 研究结论（带来源字段）

仅供个人学习研究，非投资建议。
"""
import os
import re
import json
import time
import datetime
import threading
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "chain_cache")
KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge", "chain_kb.json")
os.makedirs(CACHE_DIR, exist_ok=True)
TTL = {"profile": 7 * 24 * 3600, "business": 7 * 24 * 3600, "compose": 7 * 24 * 3600,
       "concepts": 7 * 24 * 3600, "contracts": 24 * 3600, "related": 7 * 24 * 3600}
_LOCK = threading.RLock()
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 基金/ETF 前缀：这类标的是基金，无公司上下游产业链
FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")


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


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _safe_fetch(kind, code, fn, refresh=False):
    code = str(code).zfill(6)
    if refresh:
        try:
            os.remove(_cache_path(kind, code))
        except Exception:
            pass
    cached = _read_cache(kind, code)
    now = time.time()
    if cached and not cached.get("stale") and now - cached.get("fetched_at", 0) < TTL[kind]:
        return cached
    with _LOCK:
        cached = _read_cache(kind, code)  # 双检
        if cached and not cached.get("stale") and now - cached.get("fetched_at", 0) < TTL[kind]:
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


# ---------- 真实数据抓取 ----------
def fetch_profile(code, refresh=False):
    """巨潮个股资料：所属行业/主营业务/经营范围/入选指数。"""
    def fn():
        df = _ak().stock_profile_cninfo(symbol=str(code).zfill(6))
        if df.empty:
            return None
        r = df.iloc[0]
        return {
            "company": _s(r.get("公司名称")),
            "industry": _s(r.get("所属行业")),
            "main_business": _s(r.get("主营业务")),
            "business_scope": _s(r.get("经营范围")),
            "indexes": [x.strip() for x in re.split(r"[,，]", _s(r.get("入选指数"))) if x.strip()],
            "market": _s(r.get("所属市场")),
            "list_date": _s(r.get("上市日期"))[:10],
            "website": _s(r.get("官方网站")),
        }
    return _safe_fetch("profile", code, fn, refresh)


def fetch_business(code, refresh=False):
    """同花顺主营业务：产品类型/产品名称（补充业务颗粒度）。"""
    def fn():
        df = _ak().stock_zyjs_ths(symbol=str(code).zfill(6))
        if df.empty:
            return None
        r = df.iloc[0]
        return {
            "main_business": _s(r.get("主营业务")),
            "product_types": [x.strip() for x in re.split(r"[,，、]", _s(r.get("产品类型"))) if x.strip()],
            "products": [x.strip() for x in re.split(r"[,，、]", _s(r.get("产品名称"))) if x.strip()],
            "business_scope": _s(r.get("经营范围")),
        }
    return _safe_fetch("business", code, fn, refresh)


def fetch_compose(code, refresh=False):
    """东财 F10 主营构成：最新报告期按产品/行业/地区分类。"""
    def fn():
        df = _ak().stock_zygc_em(symbol=_market_prefix(code))
        if df.empty:
            return None
        latest = _s(df["报告日期"].max())
        cur = df[df["报告日期"].astype(str) == latest].copy()
        groups = {}
        for cat in ("按产品分类", "按行业分类", "按地区分类"):
            sub = cur[cur["分类类型"].astype(str) == cat]
            if sub.empty:
                continue
            items = []
            for _, r in sub.iterrows():
                ratio = r.get("收入比例")
                gpr = r.get("毛利率")
                items.append({
                    "name": _s(r.get("主营构成")),
                    "revenue_ratio": round(float(ratio) * 100, 2) if ratio == ratio and ratio is not None else None,
                    "gross_margin": round(float(gpr) * 100, 2) if gpr == gpr and gpr is not None else None,
                })
            items = [x for x in items if x["name"]]
            if items:
                groups[cat] = items[:12]
        return {"report_date": latest, "groups": groups}
    return _safe_fetch("compose", code, fn, refresh)


# ---------- 真实数据抓取（第二层：概念板块 / 关联公司 / 合同订单）----------
def _http_get_json(url, timeout=15):
    """优先 curl 子进程（绕过东财对 Python TLS 指纹的封锁），失败回退 urllib。"""
    import subprocess
    try:
        p = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--compressed", "--max-time", str(timeout),
             "-A", _UA["User-Agent"], url],
            capture_output=True, timeout=timeout + 5)
        if p.returncode == 0 and p.stdout:
            return json.loads(p.stdout.decode("utf-8", errors="replace"))
    except Exception:
        pass
    import urllib.request
    import gzip as _gzip
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA["User-Agent"],
        "Accept-Encoding": "gzip",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = _gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def fetch_concepts_em(code, refresh=False):
    """东财 F10 核心题材接口：所属概念板块 + 核心题材要点（真实数据）。"""
    def fn():
        url = (f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/"
               f"PageAjax?code={_market_prefix(code)}")
        j = _http_get_json(url)
        concepts = []
        for i, b in enumerate(j.get("ssbk") or []):
            nm = _s(b.get("BOARD_NAME"))
            if not nm:
                continue
            concepts.append({
                "name": nm,
                "board_code": _s(b.get("BOARD_CODE")),
                "is_industry": (i == 0),  # 东财 F10 返回第一条为所属行业
            })
        highlights = []
        for h in (j.get("hxtc") or [])[:14]:
            highlights.append({
                "keyword": _s(h.get("KEYWORD")),
                "content": _s(h.get("MAINPOINT_CONTENT"))[:220],
            })
        return {"concepts": concepts, "highlights": highlights}
    return _safe_fetch("concepts", code, fn, refresh)


# 板块名称语义规则：映射产业链方向（规则推断，前端标注需核实）
UPSTREAM_KW = ("芯片", "半导体", "存储", "元器件", "PCB", "印制电路", "面板", "光模块",
               "光器件", "光芯片", "电源", "电池", "稀土", "有色", "钢铁", "化工",
               "材料", "液冷", "散热", "连接器", "模具", "封测", "晶圆", "硅",
               "电感", "继电器", "铜缆")
DOWNSTREAM_KW = ("云计算", "云服务", "数据中心", "IDC", "智算", "算力", "AI应用",
                 "人工智能", "信创", "数字经济", "智慧", "无人驾驶", "智能驾驶",
                 "机器人", "低空", "车联网", "物联网", "政务", "金融科技",
                 "AI+", "智能体", "数据要素", "行业应用", "算力租赁")
# 宽口径/交易类板块：与产业链无关，排除
BOARD_EXCLUDE = ("融资融券", "转融券", "标普", "富时", "MSCI", "沪深300", "中证500",
                 "中证800", "中证1000", "中证A500", "上证50", "上证180", "上证380",
                 "科创50", "创业板综", "创业板指", "深证", "北证50", "证金", "汇金",
                 "机构重仓", "基金重仓", "社保重仓", "QFII", "券商重仓", "昨日",
                 "连涨", "连跌", "分红", "股权激励", "员工持股", "转债", "预盈",
                 "预亏", "次新股", "破净", "低价", "高换手", "百元股", "活跃",
                 "人气", "举牌", "重组", "IPO", "独角兽", "AB股", "AH股", "B股",
                 "GDR", "注册制", "同花顺", "标准普尔", "茅指数", "宁组合")


# push2 存在反爬间歇拒绝，配置多镜像容灾
_PUSH2_HOSTS = ("push2.eastmoney.com", "1.push2.eastmoney.com",
                "23.push2.eastmoney.com", "92.push2.eastmoney.com")


def _board_constituents(board_code, limit=60, exclude_code=None):
    """东财板块成分股（按总市值降序，真实数据）。board_code 兼容 '735' / 'BK735' 两种格式。"""
    bc = str(board_code)
    if not bc.startswith("BK"):
        bc = "BK" + bc
    rows, last_err = None, None
    for host in _PUSH2_HOSTS:
        try:
            url = (f"https://{host}/api/qt/clist/get?pn=1&pz={limit}&po=1&np=1"
                   f"&fltt=2&invt=2&fid=f20&fs=b:{bc}&fields=f12,f14")
            data = (_http_get_json(url, timeout=8) or {}).get("data") or {}
            rows = data.get("diff") or []
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if rows is None:
        raise RuntimeError(f"push2 镜像全部失败：{last_err}")
    out = []
    for row in rows:
        c = _s(row.get("f12"))
        nm = _s(row.get("f14"))
        if not c or not nm:
            continue
        if "ST" in nm or "退" in nm:
            continue
        out.append({"code": c, "name": nm})
    return out


_BOARD_CACHE = {}  # board_code -> (ts, comps) 进程内缓存，避免重复请求触发反爬


def _board_constituents_cached(board_code, exclude_code=None):
    hit = _BOARD_CACHE.get(str(board_code))
    now = time.time()
    if hit and now - hit[0] < 7 * 24 * 3600:
        comps = hit[1]
    else:
        comps = _board_constituents(board_code, limit=60)
        _BOARD_CACHE[str(board_code)] = (now, comps)
    if exclude_code:
        ex = str(exclude_code).zfill(6)
        comps = [c for c in comps if c["code"] != ex]
    return comps


def _dedup_companies(items, cap):
    seen, out = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        out.append(it)
        if len(out) >= cap:
            break
    return out


def fetch_related_companies(code, refresh=False):
    """产业链关联公司：上游/下游/同链/同行（成分股名单真实，方向映射为规则推断）。"""
    code = str(code).zfill(6)

    def fn():
        cp = fetch_concepts_em(code, refresh)
        data = (cp.get("data") or {}) if cp else {}
        boards = data.get("concepts") or []
        if not boards:
            raise RuntimeError("无概念板块数据")
        industry_board = next((b for b in boards if b.get("is_industry")), None)
        cand = []
        for b in boards:
            nm = b.get("name", "")
            if b.get("is_industry") or any(x in nm for x in BOARD_EXCLUDE):
                continue
            cand.append(b)
        picked_all = []
        for b in cand:
            nm = b["name"]
            if any(k in nm for k in UPSTREAM_KW):
                role = "上游"
            elif any(k in nm for k in DOWNSTREAM_KW):
                role = "下游"
            else:
                role = "同链"
            picked_all.append({"name": nm, "board_code": b.get("board_code"), "role": role})
        # 按方向配额混合挑选：上游 2 + 下游 2，不足时用同链概念补位
        by_role = {"上游": [], "下游": [], "同链": []}
        for p in picked_all:
            by_role[p["role"]].append(p)
        picked = by_role["上游"][:2] + by_role["下游"][:2]
        if len(picked) < 4:
            picked += by_role["同链"][:4 - len(picked)]

        up, down, peer = [], [], []
        bucket_map = {"上游": up, "下游": down}
        pending = list(picked)
        # push2 反爬间歇拒绝：逐概念重试两轮，成功结果进程内缓存
        for attempt in range(2):
            still = []
            for p in pending:
                try:
                    comps = _board_constituents_cached(p["board_code"], exclude_code=code)
                except Exception:
                    still.append(p)
                    continue
                bucket = bucket_map.get(p["role"], peer)
                for c in comps:
                    bucket.append({"code": c["code"], "name": c["name"],
                                   "concept": p["name"], "role": p["role"]})
            pending = still
            if not pending:
                break
            time.sleep(1.2)
        if industry_board and industry_board.get("board_code"):
            for attempt in range(2):
                try:
                    for c in _board_constituents_cached(industry_board["board_code"],
                                                        exclude_code=code)[:12]:
                        peer.append({"code": c["code"], "name": c["name"],
                                     "concept": "同行业·" + industry_board["name"], "role": "同行"})
                    break
                except Exception:
                    time.sleep(1.2)
        return {
            "upstream": _dedup_companies(up, 12),
            "downstream": _dedup_companies(down, 12),
            "peers": _dedup_companies(peer, 12),
            "concepts_used": [p["name"] for p in picked],
            "industry": (industry_board or {}).get("name", ""),
            "note": ("公司名单来自东方财富板块成分股（真实公开数据）；「上游/下游/同链」方向由板块名称"
                     "语义规则映射（规则推断，需核实）。同属板块代表产业相关，不等于直接供应商/客户关系。"),
        }
    return _safe_fetch("related", code, fn, refresh)


CONTRACT_KW = re.compile("中标|合同|订单|框架协议|签约|战略合作|中标候选人|采购")


def fetch_contract_announcements(code, refresh=False):
    """巨潮资讯网公告标题检索：近 13 个月合同/中标/订单类公告（真实一手披露）。"""
    def fn():
        ak = _ak()
        end = datetime.date.today().strftime("%Y%m%d")
        start = (datetime.date.today() - datetime.timedelta(days=395)).strftime("%Y%m%d")
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=str(code).zfill(6), market="沪深京", start_date=start, end_date=end)
        if df is None or df.empty:
            return {"count": 0, "total_announcements": 0, "items": [],
                    "window_days": 395, "source": "巨潮资讯网公告"}
        items = []
        for _, r in df.iterrows():
            title = _s(r.get("公告标题"))
            if CONTRACT_KW.search(title):
                items.append({
                    "date": _s(r.get("公告时间"))[:10],
                    "title": title,
                    "url": _s(r.get("公告链接")),
                })
        items.sort(key=lambda x: x["date"], reverse=True)
        return {"count": len(items), "total_announcements": int(len(df)),
                "items": items[:12], "window_days": 395,
                "source": "巨潮资讯网公告标题检索（真实一手披露）"}
    return _safe_fetch("contracts", code, fn, refresh)



def _load_kb():
    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"industries": {}, "stocks": {}}


def infer_chain(code, real):
    """基于真实数据 + 知识库推断产业链位置与上下游客群。
    返回结构带 source 字段区分 REAL / INFERRED，前端逐条标注。
    """
    code = str(code).zfill(6)
    kb = _load_kb()
    profile = (real.get("profile") or {}).get("data") or {}
    business = (real.get("business") or {}).get("data") or {}
    compose = (real.get("compose") or {}).get("data") or {}

    industry = profile.get("industry", "")
    desc = " ".join([profile.get("main_business", ""), business.get("main_business", ""),
                     " ".join(business.get("products", [])), " ".join(business.get("product_types", []))])

    # 个股专属知识库优先（人工/AI 研究结论，带来源）
    stock_kb = kb.get("stocks", {}).get(code)
    # 行业通用知识库：行业名关键字匹配
    industry_kb = None
    for key, val in kb.get("industries", {}).items():
        if key in industry or industry in key:
            industry_kb = val
            break

    nodes = []
    links = []
    notes = []

    if stock_kb:
        nodes = stock_kb.get("nodes", [])
        links = stock_kb.get("links", [])
        notes = stock_kb.get("notes", [])
        for n in nodes:
            n.setdefault("source", "AI_INFERRED")
        for l in links:
            l.setdefault("source", "AI_INFERRED")
    elif industry_kb:
        nodes = industry_kb.get("nodes", [])
        links = industry_kb.get("links", [])
        notes = industry_kb.get("notes", [])
        for n in nodes:
            n.setdefault("source", "INDUSTRY_KB")
        for l in links:
            l.setdefault("source", "INDUSTRY_KB")
    else:
        # 无知识库时，基于真实数据做最小推断：仅定位环节，不臆造具体公司
        stage = _guess_stage(industry, desc)
        nodes = [{"id": "self", "name": profile.get("company", ""), "stage": stage,
                  "role": "当前标的", "source": "INFERRED", "basis": f"行业：{industry or '未知'}"}]
        notes.append("该股暂无产业链知识库条目，图谱仅基于真实行业/主营业务做环节定位；未臆造上下游公司。"
                     "真实关联公司、合同订单公告见上方「产业链关联公司」「合同/中标/订单公告」区块（东财板块成分与巨潮披露）。"
                     "如需补充研究结论，可在 knowledge/chain_kb.json 扩充。")

    return {
        "nodes": nodes, "links": links, "notes": notes,
        "kb_level": "stock" if stock_kb else ("industry" if industry_kb else "basic"),
        "inference_basis": {"industry": industry, "description": desc[:300]},
        "disclaimer": "产业链关联关系为公开资料整理与规则推断，非一手数据，需核实原文；不构成投资建议。",
    }


def _guess_stage(industry, desc):
    """粗略判断在产业链中的位置（仅环节，不point具体公司）。"""
    # 注意顺序：下游消费类优先判断（"酒/饮料制造"含"制造"但属下游）
    down_kw = ("酒", "饮料", "食品", "零售", "商业", "汽车", "家电", "餐饮", "房地产",
               "银行", "证券", "保险", "传媒", "旅游", "纺织服装", "家居")
    upstream_kw = ("采矿", "煤炭", "石油", "有色金属", "钢铁", "化学原料", "农业", "养殖", "非金属矿")
    mid_kw = ("制造", "加工", "设备", "电子", "半导体", "医药", "化工", "材料", "工业", "能源")
    for kw in down_kw:
        if kw in industry or kw in desc:
            return "下游"
    for kw in upstream_kw:
        if kw in industry or kw in desc:
            return "上游"
    for kw in mid_kw:
        if kw in industry or kw in desc:
            return "中游"
    return "中游"


def fetch_chain(code, refresh=False):
    """对外主入口：聚合真实数据 + AI 推断层。ETF/基金返回友好提示。"""
    code = str(code).zfill(6)
    if is_fund(code):
        return {
            "code": code, "is_fund": True,
            "message": "该代码为场内基金/ETF，属于一篮子证券，无单一公司的上下游产业链。请输入个股代码。",
            "real": {}, "inference": None,
        }
    real = {
        "profile": fetch_profile(code, refresh),
        "business": fetch_business(code, refresh),
        "compose": fetch_compose(code, refresh),
    }
    concepts = fetch_concepts_em(code, refresh)
    contracts = fetch_contract_announcements(code, refresh)
    related = fetch_related_companies(code, refresh)
    inference = infer_chain(code, real)
    # 数据新鲜度汇总
    sources = []
    for kind, label in (("profile", "巨潮资讯·公司资料"), ("business", "同花顺·主营业务"),
                        ("compose", "东方财富F10·主营构成"),
                        ("concepts", "东方财富F10·概念板块与核心题材"),
                        ("related", "东方财富·板块成分关联公司"),
                        ("contracts", "巨潮资讯·合同中标公告")):
        b = real[kind] if kind in real else (concepts if kind == "concepts" else
                                             related if kind == "related" else contracts)
        sources.append({
            "key": kind, "label": label,
            "ok": b.get("data") is not None,
            "stale": bool(b.get("stale")),
            "fetched_at": b.get("fetched_at", 0),
            "error": b.get("error"),
        })
    cp_data = (concepts.get("data") or {}) if concepts else {}
    ct_data = (contracts.get("data") or {}) if contracts else {}
    rl_data = (related.get("data") or {}) if related else {}
    return {
        "code": code, "is_fund": False,
        "real": {
            "company": (real["profile"].get("data") or {}).get("company", ""),
            "industry": (real["profile"].get("data") or {}).get("industry", ""),
            "main_business": (real["profile"].get("data") or {}).get("main_business", ""),
            "business_scope": (real["profile"].get("data") or {}).get("business_scope", ""),
            "indexes": (real["profile"].get("data") or {}).get("indexes", []),
            "list_date": (real["profile"].get("data") or {}).get("list_date", ""),
            "products": (real["business"].get("data") or {}).get("products", []),
            "product_types": (real["business"].get("data") or {}).get("product_types", []),
            "compose": (real["compose"].get("data") or {}).get("groups", {}),
            "compose_date": (real["compose"].get("data") or {}).get("report_date", ""),
        },
        "concepts": cp_data.get("concepts", []),
        "highlights": cp_data.get("highlights", []),
        "related": rl_data,
        "contracts": ct_data,
        "sources": sources,
        "inference": inference,
    }
