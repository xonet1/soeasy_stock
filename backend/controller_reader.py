# -*- coding: utf-8 -*-
"""
controller_reader.py — 全市场实控人 / 股权穿透 / 投资人关联版图

数据源：东方财富 F10「股东研究」PageAjax（公开披露页），每只股票一次请求可拿到：
  - sjkzr：实际控制人（HOLDER_NAME / HOLD_RATIO）
  - sdltgd：前十大股东（名次、名称、持股数、占比、股份类型、变动）
  - sjkzr 为空时如实标注「未披露/无实际控制人」，不用第一大股东冒充实控人。

设计要点：
  - 每只股票独立 JSON 缓存，7 天 TTL；扫描可跳过新鲜缓存，支持断点续采。
  - 并发采集 + 限速 + 重试；失败只记录该股，不中断全市场任务。
  - 图谱按「实控人 → 控股平台/关联主体 → 上市公司」三层构建；
    关联主体来自该公司前十大股东，再反查该主体在全市场其他上市公司的持股，
    形成投资人的跨公司投资链条。
  - 覆盖范围仅限 A 股上市公司的公开披露；未上市关联公司需工商数据，未接入。
"""
import os
import re
import json
import time
import random
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import cninfo_reader  # 巨潮公告证据层（可选，失败不影响主流程）
except Exception:
    cninfo_reader = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "controller_cache")
MODEL_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

GRAPH_PATH = os.path.join(MODEL_DIR, "controller_graph.json")
HOLDER_INDEX_PATH = os.path.join(MODEL_DIR, "holder_index.json")
TTL = 7 * 24 * 3600
API = "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
}
_LOCAL = threading.local()


def _session():
    """线程内复用连接：东财 HTTPS 握手约 0.7s/次，复用后单请求降到 ~0.8s（4 倍提速）。"""
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _LOCAL.s = s
    return s


_LOCK = threading.RLock()
_RETRY = 2


def secucode(code):
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "9")):
        return "SH" + code
    if code.startswith(("4", "8")):
        return "BJ" + code
    return "SZ" + code


def _norm(s):
    s = re.sub(r"\s+", "", str(s or ""))
    return s.replace("（", "(").replace("）", ")")


_A_PREFIX = ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301",
             "430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839",
             "870", "871", "872", "873", "874", "920")


def list_a_share_codes(index):
    """从本地行情索引中筛出 A 股代码（剔除指数/基金）。index 可为 dict 或键列表。"""
    keys = index.keys() if isinstance(index, dict) else index
    return sorted(c for c in keys if str(c).zfill(6).startswith(_A_PREFIX))


def _stock_names():
    try:
        with open(os.path.join(BASE_DIR, "data", "stock_names.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _type_of(controller):
    s = str(controller or "")
    if not s or s == "未披露/无实际控制人":
        return "未披露"
    if any(k in s for k in ("人民政府", "国资委", "国有资产监督", "国务院", "财政部")):
        return "国资"
    if any(k in s for k in ("有限公司", "集团", "股份", "合伙企业", "研究院", "大学", "基金会")):
        return "企业/机构"
    if len(s) <= 5:
        return "自然人"
    return "其他机构"


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"ctl_{str(code).zfill(6)}.json")


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


# ---------------------------------------------------------------- 单股采集
def fetch_one(code, refresh=False, name=""):
    """采集单只股票的实控人 + 前十大股东，带磁盘缓存。失败返回 None。"""
    code = str(code).zfill(6)
    cached = None if refresh else _read_cache(code)
    if cached and time.time() - cached.get("fetched_at", 0) < TTL:
        return cached
    err = None
    for attempt in range(_RETRY + 1):
        try:
            r = _session().get(API, params={"code": secucode(code)}, timeout=18)
            r.raise_for_status()
            d = r.json()
            ctl_rows = d.get("sjkzr") or []
            top_rows = d.get("sdltgd") or []
            ctl = ctl_rows[0] if ctl_rows else {}
            ctl_name = _norm(ctl.get("HOLDER_NAME")) or "未披露/无实际控制人"
            holders = []
            for x in top_rows:
                nm = _norm(x.get("HOLDER_NAME"))
                if not nm:
                    continue
                holders.append({
                    "name": nm,
                    "rank": x.get("HOLDER_RANK"),
                    "shares": x.get("HOLD_NUM"),
                    "ratio": round(float(x.get("FREE_HOLDNUM_RATIO") or 0), 3),
                    "type": _norm(x.get("HOLDER_TYPE")),
                    "shares_type": _norm(x.get("SHARES_TYPE")),
                    "change": _norm(x.get("HOLD_NUM_CHANGE")),
                    "end_date": str(x.get("END_DATE") or "")[:10],
                })
            holders.sort(key=lambda x: x["rank"] or 99)
            rec = {
                "code": code,
                "name": name or _stock_names().get(code, ""),
                "fetched_at": time.time(),
                "report_date": holders[0]["end_date"] if holders else "",
                "controller": ctl_name,
                "controller_type": _type_of(ctl_name),
                "controller_ratio": ctl.get("HOLD_RATIO"),
                "owner": holders[0]["name"] if holders else "",
                "owner_ratio": holders[0]["ratio"] if holders else None,
                "holders": holders,
            }
            _write_cache(code, rec)
            return rec
        except Exception as e:
            err = str(e)[:200]
            if attempt < _RETRY:
                time.sleep(0.5 * (attempt + 1) + random.random() * 0.3)
    if cached:
        stale = dict(cached)
        stale["stale"] = True
        stale["error"] = err
        return stale
    return None


def scan_all(codes, refresh=False, workers=6, delay=0.1, progress_cb=None, limit=None):
    """并发扫描全市场。codes 为股票代码列表。返回统计信息。"""
    codes = [str(c).zfill(6) for c in (codes or [])]
    if limit:
        codes = codes[:int(limit)]
    todo, cached_n = [], 0
    for c in codes:
        if not refresh:
            cached = _read_cache(c)
            if cached and time.time() - cached.get("fetched_at", 0) < TTL:
                cached_n += 1
                continue
        todo.append(c)
    total = len(todo)
    stats = {"total": total, "cached": cached_n, "done": 0, "failed": 0, "errors": []}
    if not total:
        if progress_cb:
            progress_cb(0, 0, "全部命中缓存，无需重新采集")
        return stats

    def task(code):
        time.sleep(random.random() * delay)
        return code, fetch_one(code, refresh=refresh)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = [ex.submit(task, c) for c in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                code, rec = fut.result()
                if rec is None:
                    stats["failed"] += 1
                    stats["errors"].append(code)
                else:
                    stats["done"] += 1
            except Exception:
                stats["failed"] += 1
            if progress_cb and (i % 20 == 0 or i == total):
                progress_cb(i, total, f"已采集 {i}/{total}，跳过缓存 {cached_n}，失败 {stats['failed']}")
    return stats


# ---------------------------------------------------------------- 图谱构建
def load_all():
    out = []
    for fn in os.listdir(CACHE_DIR):
        m = re.match(r"ctl_(\d{6})\.json$", fn)
        if not m:
            continue
        try:
            with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


def build_holder_index(refresh=False):
    """全市场「股东实体 → 出现在哪些上市公司前十大股东」反查索引。

    供股东穿透面板回答「这个股东还投资了哪些股票」。带落盘缓存，
    扫描完成后随图谱一起刷新。索引只覆盖前十大股东（公开披露口径）。
    """
    if not refresh and os.path.exists(HOLDER_INDEX_PATH):
        try:
            with open(HOLDER_INDEX_PATH, "r", encoding="utf-8") as f:
                idx = json.load(f)
            if time.time() - idx.get("generated_at", 0) < TTL:
                return idx["index"]
        except Exception:
            pass

    rows = load_all()
    index = {}
    for r in rows:
        rec_base = {"code": r["code"], "stock": r.get("name", ""),
                    "report_date": r.get("report_date", "")}
        for h in r.get("holders", []):
            nm = str(h.get("name") or "").strip()
            if not nm:
                continue
            index.setdefault(nm, []).append({
                **rec_base,
                "rank": h.get("rank"), "ratio": h.get("ratio"),
                "shares": h.get("shares"), "change": h.get("change"),
                "end_date": h.get("end_date"), "holder_type": h.get("type", ""),
            })
    # 每条记录按占比从高到低，便于直接展示最重仓的标的
    for nm in index:
        index[nm].sort(key=lambda x: -(x.get("ratio") or 0))
    try:
        with open(HOLDER_INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.time(), "stock_count": len(rows),
                       "entity_count": len(index), "index": index}, f, ensure_ascii=False)
    except Exception:
        pass
    return index


def build_graph(refresh=False):
    """按实控人聚合，构建跨公司关联图谱并落盘。"""
    if not refresh and os.path.exists(GRAPH_PATH):
        try:
            with open(GRAPH_PATH, "r", encoding="utf-8") as f:
                g = json.load(f)
            age = time.time() - g.get("generated_at", 0)
            if age < 3600:
                return g
        except Exception:
            pass

    rows = load_all()
    by_code = {r["code"]: r for r in rows}
    # 全市场「股东实体 → 出现的上市公司」，用于反查关联公司
    entity_codes = {}
    for r in rows:
        for h in r.get("holders", []):
            entity_codes.setdefault(h["name"], []).append({
                "code": r["code"], "name": r.get("name", ""), "rank": h.get("rank"),
                "ratio": h.get("ratio"), "end_date": h.get("end_date"),
            })

    groups = {}
    for r in rows:
        ctl = r.get("controller") or "未披露/无实际控制人"
        g = groups.setdefault(ctl, {
            "name": ctl, "type": r.get("controller_type") or _type_of(ctl),
            "companies": [], "entities": {}, "related": {},
        })
        comp = {
            "code": r["code"], "name": r.get("name", ""),
            "owner": r.get("owner", ""), "owner_ratio": r.get("owner_ratio"),
            "controller_ratio": r.get("controller_ratio"),
            "report_date": r.get("report_date", ""),
        }
        # 巨潮公告证据（只读缓存，不触发网络，不阻塞图谱构建）
        if cninfo_reader:
            try:
                ev = cninfo_reader.evidence_summary(r["code"])
                if ev:
                    comp["cninfo"] = ev
            except Exception:
                pass
        g["companies"].append(comp)
        # 该公司的控股平台 / 实控人关联主体
        ent_seen = set()
        for h in r.get("holders", []):
            nm = h["name"]
            in_group = (nm == ctl or (len(ctl) >= 2 and ctl in nm) or
                        (len(nm) >= 3 and nm in ctl))
            is_top = h.get("rank") == 1
            if not (in_group or is_top):
                continue
            if nm in ent_seen:
                continue
            ent_seen.add(nm)
            e = g["entities"].setdefault(nm, {
                "name": nm, "role": [], "codes": set(), "market_codes": set(),
            })
            if is_top:
                e["role"].append("控股平台/第一大股东")
            if in_group:
                e["role"].append("实控人关联主体")
            e["codes"].add(r["code"])
            for x in entity_codes.get(nm, []):
                e["market_codes"].add(x["code"])
                g["related"].setdefault(x["code"], {
                    "code": x["code"], "name": x.get("name", ""),
                    "via": set(), "ratio": x.get("ratio"),
                })["via"].add(nm)

    controllers = []
    for ctl, g in groups.items():
        own_codes = {c["code"] for c in g["companies"]}
        ents = []
        for nm, e in g["entities"].items():
            ents.append({
                "name": nm, "role": "、".join(dict.fromkeys(e["role"])),
                "own_codes": sorted(e["codes"]),
                "market_count": len(e["market_codes"]),
                "market_codes": sorted(e["market_codes"])[:30],
            })
        ents.sort(key=lambda x: (-len(x["own_codes"]), -x["market_count"]))
        related = [v for k, v in g["related"].items() if k not in own_codes]
        related.sort(key=lambda x: (-x["ratio"] if x.get("ratio") else 0, x["code"]))
        g["companies"].sort(key=lambda x: -(x.get("owner_ratio") or 0))
        controllers.append({
            "name": ctl, "type": g["type"],
            "company_count": len(g["companies"]),
            "companies": g["companies"],
            "entities": ents[:40],
            "related_count": len(related),
            "related": [{**{k: v for k, v in r.items() if k != "via"},
                         "via": sorted(r["via"])} for r in related[:40]],
        })
    controllers.sort(key=lambda x: -x["company_count"])

    graph = {
        "generated_at": time.time(),
        "stock_count": len(rows),
        "controller_count": len(controllers),
        "with_controller": sum(1 for r in rows if r.get("controller") not in ("", "未披露/无实际控制人")),
        "entity_count": len(entity_codes),
        "controllers": controllers,
        "code_to_controller": {r["code"]: r.get("controller") for r in rows},
    }
    try:
        with open(GRAPH_PATH, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False)
    except Exception:
        pass
    return graph


def graph_summary():
    """轻量摘要，避免前端一次性拉取完整图谱。"""
    g = build_graph()
    return {
        "generated_at": g["generated_at"],
        "stock_count": g["stock_count"],
        "controller_count": g["controller_count"],
        "with_controller": g["with_controller"],
        "entity_count": g["entity_count"],
        "top": [{"name": c["name"], "type": c["type"], "count": c["company_count"]}
                for c in g["controllers"][:20]],
    }


def controller_list(limit=100):
    """实控人榜单摘要（不返回完整图谱，供左侧列表使用）。"""
    g = build_graph()
    out = [{
        "name": c["name"], "type": c["type"], "count": c["company_count"],
        "sample": [x.get("name") or x["code"] for x in c["companies"][:5]],
    } for c in g["controllers"]
        if c["company_count"] > 0 and c["type"] != "未披露"]
    return out[:max(1, min(500, int(limit or 100)))]


def company_focus(code):
    """以单家上市公司为中心的股权穿透图：公司 → 前十大股东 → 该股东在全市场的其它持股。

    用于无实控人公司（如实控人未披露）的穿透展示，避免把整个「未披露」分组塞进一张图。
    返回与 controller_detail 兼容的结构（name/type/nodes/links/companies/entities/related）。
    """
    code = str(code).zfill(6)
    rec = _read_cache(code) or next((r for r in load_all() if r["code"] == code), None)
    if not rec:
        return None
    names = _stock_names()
    stock_name = rec.get("name") or names.get(code, "")
    ctl = rec.get("controller") or "未披露/无实际控制人"
    center_id = "S:" + code
    nodes = [{
        "id": center_id, "name": stock_name or code, "category": 2,
        "symbolSize": 46, "code": code, "type": rec.get("controller_type"),
        "ratio": rec.get("owner_ratio"), "role": "穿透主体",
    }]
    links = []
    index = None
    try:
        index = build_holder_index()
    except Exception:
        index = None
    index = index or {}
    entities = []
    related = []
    related_seen = set()
    for h in rec.get("holders", []):
        hname = h.get("name")
        if not hname:
            continue
        eid = "E:" + hname
        role = "第一大股东" if h.get("rank") == 1 else "前十大股东"
        nodes.append({
            "id": eid, "name": hname, "category": 1, "symbolSize": 24,
            "role": role, "ratio": h.get("ratio"),
        })
        links.append({"source": center_id, "target": eid, "value": "持股 " + (str(h.get("ratio")) if h.get("ratio") is not None else "") + "%"})
        market_positions = index.get(hname, [])
        entities.append({"name": hname, "role": role, "own_codes": [code],
                         "market_count": len(market_positions), "ratio": h.get("ratio")})
        # 泛词主体（中央结算/代理人/基金/证券等）在全市场命中数千家，展开无意义，跳过
        generic = ("中央结算", "代理人", "结算有限公司", "基金", "证券", "保险", "社保", "QFII",
                   "养老", "年金", "信托", "资管", "银行", "投资管理", "资产管理")
        if any(x in hname for x in generic):
            links[-1]["value"] = "持股（关联略）"
            continue
        # 该股东在全市场的其它上市公司持股（关联公司层）
        for pos in market_positions[:40]:
            if pos.get("code") == code or pos["code"] in related_seen:
                continue
            related_seen.add(pos["code"])
            sid = "S:" + pos["code"]
            nodes.append({
                "id": sid, "name": pos.get("stock") or pos["code"], "category": 3,
                "symbolSize": 15, "code": pos["code"], "ratio": pos.get("ratio"),
            })
            links.append({"source": eid, "target": sid, "value": "关联持股"})
            related.append({"code": pos["code"], "name": pos.get("stock") or "",
                            "ratio": pos.get("ratio"), "via": [hname]})
    related.sort(key=lambda x: -(x.get("ratio") or 0))
    return {
        "name": stock_name or code, "type": ctl if ctl and ctl != "未披露/无实际控制人" else "未披露/无实际控制人",
        "company_count": 1, "related_count": len(related),
        "nodes": nodes, "links": links,
        "companies": [{"code": code, "name": stock_name, "owner": rec.get("owner", ""),
                       "owner_ratio": rec.get("owner_ratio"), "controller_ratio": rec.get("controller_ratio"),
                       "report_date": rec.get("report_date", ""), "cninfo": (cninfo_reader.evidence_summary(code) if cninfo_reader else None)}],
        "entities": entities[:40], "related": related[:40],
        "focus": True,
    }


def controller_detail(name, q=""):
    """单个实控人的图谱节点/连线与关联清单（ECharts 友好）。"""
    g = build_graph()
    target = None
    for c in g["controllers"]:
        if c["name"] == name:
            target = c
            break
    if not target:
        # 支持按公司代码反查：无实控人公司走聚焦图，其余走实控人分组
        code = str(name).zfill(6)
        ctl = g["code_to_controller"].get(code)
        if ctl in ("未披露/无实际控制人", "", None):
            focus = company_focus(code)
            if focus:
                return focus
        target = next((c for c in g["controllers"] if c["name"] == ctl), None)
    if not target:
        # 支持按公司名称反查。若该公司无实控人，返回以该公司为中心的聚焦图，
        # 而非把 700+ 家「未披露」公司塞进一张图。
        code_by_name, found_code = {}, None
        for c in g["controllers"]:
            for comp in c["companies"]:
                key = _norm(comp.get("name"))
                if key:
                    code_by_name[key] = comp["code"]
        nm = _norm(name)
        if nm in code_by_name:
            found_code = code_by_name[nm]
        else:
            for key, ccode in code_by_name.items():
                if nm and (nm in key or key in nm):
                    found_code = ccode
                    break
        if found_code:
            return company_focus(found_code)
    if not target:
        return None
    nodes, links = [], []
    nodes.append({"id": "C:" + target["name"], "name": target["name"],
                  "category": 0, "symbolSize": 46, "type": target["type"]})
    for e in target["entities"]:
        eid = "E:" + e["name"]
        nodes.append({"id": eid, "name": e["name"], "category": 1,
                      "symbolSize": 26, "role": e["role"], "market_count": e["market_count"]})
        links.append({"source": "C:" + target["name"], "target": eid, "value": "控制/关联"})
        for code in e["own_codes"]:
            links.append({"source": eid, "target": "S:" + code, "value": "持股"})
    for c in target["companies"]:
        sid = "S:" + c["code"]
        if not any(n["id"] == sid for n in nodes):
            nodes.append({"id": sid, "name": c["name"] or c["code"], "category": 2,
                          "symbolSize": 20, "code": c["code"],
                          "ratio": c.get("owner_ratio")})
        links.append({"source": "C:" + target["name"], "target": sid, "value": "实际控制人"})
    for r in target["related"]:
        sid = "S:" + r["code"]
        if not any(n["id"] == sid for n in nodes):
            nodes.append({"id": sid, "name": r["name"] or r["code"], "category": 3,
                          "symbolSize": 14, "code": r["code"], "ratio": r.get("ratio")})
        for v in r["via"]:
            links.append({"source": "E:" + v, "target": sid, "value": "关联持股"})
    return {
        "name": target["name"], "type": target["type"],
        "company_count": target["company_count"],
        "related_count": target["related_count"],
        "nodes": nodes, "links": links,
        "companies": target["companies"],
        "entities": target["entities"],
        "related": target["related"],
    }


def search(q, limit=30):
    """按实控人名称或公司名称搜索。"""
    q = _norm(q)
    if not q:
        return []
    g = build_graph()
    out = []
    for c in g["controllers"]:
        if q in _norm(c["name"]):
            out.append({"kind": "controller", "name": c["name"], "type": c["type"],
                        "count": c["company_count"]})
        for comp in c["companies"]:
            if q in _norm(comp.get("name")) or q in comp["code"]:
                out.append({"kind": "company", "name": comp.get("name"), "code": comp["code"],
                            "controller": c["name"]})
        for e in c.get("entities", []):
            if q in _norm(e.get("name")):
                out.append({"kind": "entity", "name": e["name"],
                            "controller": c["name"], "count": e.get("market_count", 0)})
    seen, uniq = set(), []
    for x in out:
        k = (x["kind"], x.get("code") or x["name"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


def status():
    g = build_graph()
    cached = len(load_all())
    out = {
        "cached": cached,
        "generated_at": g["generated_at"],
        "stock_count": g["stock_count"],
        "with_controller": g["with_controller"],
        "controller_count": g["controller_count"],
        "entity_count": g.get("entity_count", 0),
        "graph_age_seconds": int(time.time() - g["generated_at"]),
    }
    if cninfo_reader:
        try:
            out["cninfo"] = cninfo_reader.status()
        except Exception:
            pass
    return out
