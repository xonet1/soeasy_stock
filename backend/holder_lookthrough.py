# -*- coding: utf-8 -*-
"""
holder_lookthrough.py — 股东穿透分析（回答"它到底为什么持股"）

思路：把「持股主体 → 控制链条 → 主体自身经营结构 → 标的吸引力 → 公开证据」串成一条可追溯的链路。
结论分两级：
  - 已披露：公司公告/定期报告明确说明的事实
  - 规则推断：基于财务数据与公开报道的推断，明确标注非公司披露动机

数据优先级：本地缓存（股东历史 / 通达信财务 / 分红） > 人工核实的公开案例库。
案例库只收录有可点击原文链接的公开报道，避免模型臆造动机。
"""
import os
import re
import json
import time

import holder_reader as hr
import cw_reader
import fin_report

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "models_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_TTL = 6 * 3600


# ---------------------------------------------------------------- 人工核实的公开案例
# key = (股东核心字号, 标的股票代码)
CASES = {
    ("雅戈尔", "000651"): {
        "verdict": {
            "label": "高股息权益配置（类固收框架）",
            "confidence": "高（多源公开报道交叉印证，非公司正式披露）",
            "level": "inferred",
            "one_liner": "服装主业利润不足以消化账上现金，雅戈尔以投资板块为主要利润来源，"
                         "把格力当作现金流替代资产；市场共识是看中分红与回购，而非产业协同。",
        },
        "chain": [
            {"name": "宁波雅戈尔服饰有限公司", "role": "直接持股主体",
             "note": "格力电器 2026 三季报第六大股东，持股 9,245.60 万股（1.65%）", "source": "格力电器 2026 三季报"},
            {"name": "雅戈尔集团股份有限公司（600177.SH）", "role": "全资母公司",
             "note": "中国证券网 2026-10-26 明确称为雅戈尔全资子公司", "source": "中国证券网"},
            {"name": "宁波雅戈尔控股有限公司", "role": "控股股东",
             "note": "持有雅戈尔 40.33%，为第一大股东", "source": "雅戈尔 2026 三季报"},
            {"name": "李如成", "role": "实际控制人 / 董事长",
             "note": "直接持有雅戈尔 2.82%，并任董事长", "source": "雅戈尔 2026 三季报"},
        ],
        "reasons": [
            {
                "title": "主体穿透：钱来自上市公司投资盘，不是产业收购",
                "detail": "中国证券网报道明确，宁波雅戈尔服饰是雅戈尔(600177)全资子公司；"
                          "雅戈尔 2026 半年报显示，上半年已耗资约 3.48 亿元买入格力电器，"
                          "并将其列为证券投资组合中唯一的 A 股标的。",
                "tag": "已披露",
                "source": "雅戈尔 2026 半年报 / 中国证券网",
            },
            {
                "title": "动机内核：利润结构高度依赖投资，现金需要出路",
                "detail": "雅戈尔 2026 上半年归母净利润 21.23 亿元，其中投资板块贡献 18.15 亿元（约 85%），"
                          "服装板块仅 2.45 亿元。服装主业利润体量已不足以消化账上庞大现金，"
                          "而银行存款利率不足 2%，配置高股息权益资产本质上是用「类固收」框架管理闲散资金。",
                "tag": "数据推断",
                "source": "中国证券网（援引行业人士）",
            },
            {
                "title": "标的选择：按建仓成本测算股息率约 8%",
                "detail": "按格力 2025 年度每股 3 元分红与雅戈尔建仓成本计算，对应股息率约 8%；"
                          "即便盈利下修，5% 以上仍显著高于存款和多数固收产品。"
                          "格力同期推出每 10 股派 10 元（合计约 55.5 亿元）的中期分红，"
                          "并推进 50–100 亿元回购、其中不低于 70% 注销。",
                "tag": "数据推断",
                "source": "中国证券网 / 华夏时报",
            },
            {
                "title": "买入时点：低估值 + 分红方案落地",
                "detail": "格力 2026 三季报营收同比 -8.07%、归母净利同比 -19.51%，"
                          "10 月末市盈率(TTM)约 8.42 倍；雅戈尔恰在三季度新进前十大，"
                          "与中期分红方案和回购公告时间吻合，呈明显的红利/价值配置特征。",
                "tag": "数据推断",
                "source": "华夏时报 2026-10-29 / 格力电器 2026 三季报",
            },
            {
                "title": "公司口径：未披露动机，不可写成事实",
                "detail": "2026 年 10 月 27 日雅戈尔方面对《华夏时报》回应「暂无可提供的信息，可以等三季报披露」，"
                          "格力电器亦未回应是否与雅戈尔后续合作。因此「高股息财务投资」是公开数据与报道支持的推断，"
                          "并非双方正式披露的持股目的。",
                "tag": "已披露",
                "source": "华夏时报 2026-10-29",
            },
        ],
        "evidence": [
            {
                "title": "“裁缝股神”大举“押注”董明珠",
                "source": "中国证券网", "date": "2026-10-26",
                "url": "https://www.cnstock.com/commonDetail/814168",
                "quote": "全资子公司、上半年买入 3.48 亿元、唯一 A 股标的、投资板块 18.15 亿元占 85%、股息率约 8%",
            },
            {
                "title": "格力电器中期分红55.5亿元，雅戈尔新进第六大股东，空调行业承压持续",
                "source": "华夏时报（新浪财经转载）", "date": "2026-10-29",
                "url": "https://finance.sina.com.cn/roll/2026-10-29/doc-iniwwyat8760923.shtml",
                "quote": "雅戈尔回应暂无可提供信息；业内认为持续稳定分红是关键因素；中期 10 派 10",
            },
        ],
        "owner_facts": {
            "投资板块净利润(2026H1)": "18.15 亿元",
            "投资板块占比": "约 85%",
            "服装板块净利润": "2.45 亿元",
            "证券组合唯一A股标的": "格力电器",
        },
        "caveats": [
            "持股动机为公开报道与财务数据推断，双方均未正式披露",
            "股息率按建仓成本与历史分红测算，未来分红存在不确定性",
        ],
    },
}


# ---------------------------------------------------------------- 实体识别
# 常见持股主体的控制链映射。仅收录有公开披露依据的关系，避免臆造。
ENTITY_MAP = {
    "雅戈尔": {
        "listed": "600177", "listed_name": "雅戈尔",
        "relation": "全资子公司",
        "controller": "李如成（实际控制人）",
        "industry": "纺织服饰 / 服装家纺",
        "confidence": "高（上市公司定期报告与媒体报道确认）",
    },
    "格力集团": {
        "listed": "", "listed_name": "",
        "relation": "控股股东（国资）", "controller": "珠海市国资委",
        "industry": "综合 / 产业集团",
        "confidence": "中（基于股东名称与公开资料）",
    },
    "京海互联": {
        "listed": "", "listed_name": "",
        "relation": "经销商联合体", "controller": "——",
        "industry": "家电渠道",
        "confidence": "中（基于公开资料）",
    },
}


def _short_name(name):
    """从公司全称里提取核心字号，用于实体识别与案例匹配。"""
    s = re.sub(r"[（(].*?[)）]", "", str(name or "")).strip()
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "合伙企业", "有限公司-自有资金"):
        s = s.replace(suffix, "")
    s = re.split(r"[-－—]", s)[0]
    s = re.sub(r"^(宁波|珠海|北京|上海|深圳|广州|杭州|中国|香港)", "", s).strip()
    # 去掉常见地名前缀后仍太长时，取前 4 字作为字号
    return s[:8] if s else ""


def _match_entity(name):
    s = str(name or "")
    for token, meta in ENTITY_MAP.items():
        if token in s:
            return token, meta
    # 通用兜底：与本地股票名称表做子串匹配，标注为"疑似关联"
    try:
        _path = os.path.join(BASE_DIR, "data", "stock_names.json")
        with open(_path, "r", encoding="utf-8") as _f:
            names = json.load(_f)
    except Exception:
        names = {}
    for code_, nm in names.items():
        if len(str(nm)) >= 2 and str(nm) in s:
            return str(nm), {
                "listed": str(code_), "listed_name": str(nm), "relation": "疑似关联企业（名称匹配）",
                "controller": "——", "industry": "", "confidence": "低（仅名称匹配，需核实）",
            }
    return _short_name(name), None


# ---------------------------------------------------------------- 本地数据摘要
def _dividend_attraction(code):
    """从本地财报缓存读取标的的股息率/分红情况，作为吸引力的客观依据。"""
    try:
        d = fin_report.fetch_fin8(code)
        dv = d.get("dividend") or {}
        rows = [r for r in (dv.get("rows") or []) if (r.get("dps") or 0) > 0]
        # dividend.rows 按年份倒序（新→旧）
        rows = sorted(rows, key=lambda r: int(r.get("year") or 0), reverse=True)
        if not rows:
            return None
        # 动机判断用「最近一个完整年度」而非只有中期派息的当年，避免低估股息吸引力
        annual = [r for r in rows if any("12-31" in str(p) for p in (r.get("periods") or []))]
        latest = (annual or rows)[0]
        interim = next((r for r in rows if r is not latest), None)
        return {
            "latest_year": latest.get("year"),
            "latest_dps": latest.get("dps"),
            "latest_yield": latest.get("div_yield"),
            "latest_payout": latest.get("payout"),
            "latest_count": latest.get("count", 1),
            "latest_plan": latest.get("plan"),
            "interim_year": interim.get("year") if interim else None,
            "interim_dps": interim.get("dps") if interim else None,
            "stability": (dv.get("summary") or {}).get("stability"),
            "source": "本地分红画像（东财方案 + 本地股价）",
        }
    except Exception:
        return None


def _owner_profile(listed_code):
    """读取股东上市主体的本地财务画像。"""
    if not listed_code:
        return None
    try:
        hist = cw_reader.history_for(str(listed_code), 6)
        if not hist:
            return None
        rows = hist if isinstance(hist, list) else list(hist.values())
        latest = rows[-1] if rows else {}
        if isinstance(latest, dict) and "period" in latest:
            vals = latest.get("data") or latest
        else:
            vals = latest
        out = {"报告期": str(latest.get("date") or "")}
        for k, label in (("np", "归母净利润"), ("rev", "营业收入"), ("roe", "净资产收益率(%)")):
            v = vals.get(k) if isinstance(vals, dict) else None
            if v is not None:
                out[label] = round(float(v), 2)
        return out or None
    except Exception:
        return None


def _holder_history(code, holder_name):
    """该股东在本股的历史持股轨迹（来自本地多期缓存）。"""
    try:
        bundle = hr._read_cache("history", str(code).zfill(6)) or {}
    except Exception:
        return []
    out = []
    for p in (bundle.get("data") or []):
        date = str(p.get("date") or "")
        item = next((x for x in (p.get("top") or []) if str(holder_name) in str(x.get("name") or "")), None)
        if item:
            out.append({
                "date": date,
                "rank": item.get("rank"),
                "shares": item.get("shares"),
                "ratio": item.get("ratio"),
                "change": item.get("change_raw"),
            })
    return out


# ---------------------------------------------------------------- 通用推断
def _generic_reasoning(holder_name, nature, industry, company_industry, attraction):
    """没有核实案例时的规则推断，结论明确降级为"待核实"。"""
    s = str(holder_name)
    reasons = []
    label = "待核实的持股动机"
    if "香港中央结算" in s:
        label = "陆股通通道持股"
        reasons.append({"title": "通道账户性质", "detail": "香港中央结算为陆股通名义持有人，代表多名境外投资者合计持股，"
                        "并非单一决策主体，不能据此判断某一机构的投资意图。", "tag": "已披露", "source": "交易所规则"})
    elif "保险" in s:
        label = "保险资金长期配置"
        reasons.append({"title": "资金属性", "detail": "保险资金负债久期长，偏好稳定现金流与低波动权益资产；"
                        "持股通常兼顾股息收入与长期资产负债表匹配。", "tag": "规则推断", "source": "资金属性推断"})
    elif "基金" in s or "证券投资" in s:
        label = "机构组合配置"
        reasons.append({"title": "组合持仓属性", "detail": "公募/资管产品持仓多为组合配置，权重受指数成分、"
                        "比较基准与申赎影响，individual 持股变化未必反映主动观点。", "tag": "规则推断", "source": "产品属性推断"})
    elif industry and company_industry and industry != company_industry:
        label = "跨界财务投资（待核实）"
        reasons.append({"title": "产业属性不匹配", "detail": f"股东属{industry}，标的属{company_industry}；"
                        "未见公开披露的产业合作安排，优先按财务投资理解，但不能排除未披露的战略意图。",
                        "tag": "规则推断", "source": "行业名称匹配"})
    elif industry and company_industry and industry == company_industry:
        label = "同业产业投资（待核实）"
        reasons.append({"title": "产业协同可能", "detail": f"股东与标的同属{industry}领域，"
                        "需进一步核实是否存在业务合作、渠道整合或产业整合预期。", "tag": "规则推断", "source": "行业名称匹配"})
    else:
        reasons.append({"title": "主体信息有限", "detail": "公开披露仅提供股东名称与持股数量，"
                        "缺少主体经营与资金来源信息，暂无法给出可靠动机判断。", "tag": "规则推断", "source": "披露数据"})

    if attraction and attraction.get("latest_yield"):
        reasons.append({
            "title": "标的的客观吸引力",
            "detail": f"标的 {attraction['latest_year']} 年每股分红 {attraction['latest_dps']} 元，"
                      f"对应股息率 {attraction['latest_yield']}%，"
                      f"派息率 {attraction['latest_payout'] if attraction['latest_payout'] is not None else '—'}%；"
                      f"分红特征：{attraction.get('stability') or '—'}。高股息是产业资本与长期资金常见的配置理由。",
            "tag": "客观数据",
            "source": attraction["source"],
        })
    return label, reasons


# ---------------------------------------------------------------- 主入口
def lookthrough(code, holder_name, refresh=False):
    """股东穿透分析。code 为标的股票，holder_name 为前十大股东名称。"""
    code = str(code).zfill(6)
    holder_name = str(holder_name or "").strip()
    if not holder_name:
        return {"ok": False, "error": "缺少股东名称"}

    cache_path = os.path.join(CACHE_DIR, f"look_{code}_{abs(hash(holder_name)) % 10000000}.json")
    if not refresh and os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < CACHE_TTL:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached["cache"] = "hit"
            return cached
        except Exception:
            pass

    token, meta = _match_entity(holder_name)
    case = CASES.get((token, code))
    nature = hr._classify_nature(holder_name, "")
    industry = hr._industry_of(holder_name)
    company_name = hr._stock_name(code)
    company_industry = hr._industry_of(company_name)
    attraction = _dividend_attraction(code)

    if case:
        verdict = case["verdict"]
        reasons = case["reasons"]
        chain = case["chain"]
        evidence = case["evidence"]
        owner_code = (meta or {}).get("listed", "")
        owner_facts = dict(case.get("owner_facts") or {})
        prof = _owner_profile(owner_code)
        if prof:
            owner_facts.update(prof)
        caveats = list(case.get("caveats") or [])
    else:
        label, reasons = _generic_reasoning(holder_name, nature, industry, company_industry, attraction)
        verdict = {
            "label": label, "confidence": "低（缺少核实的主体资料，仅规则推断）", "level": "unverified",
            "one_liner": "当前仅有股东名称与持股数据，无法确认其资金来源与持股目的；以下为可验证的线索与推断。",
        }
        chain = [{"name": holder_name, "role": "直接持股主体", "note": "来自定期报告前十大股东披露", "source": "定期报告"}]
        if meta:
            chain.append({
                "name": meta.get("listed_name") or token,
                "role": meta.get("relation") or "关联主体",
                "note": "名称/公开资料匹配，需以工商与公告核实",
                "source": "规则匹配",
            })
        evidence = []
        owner_facts = {}
        if attraction:
            owner_facts["标的股息率"] = attraction["latest_yield"]
        caveats = ["未收录该股东的核实案例，结论仅为规则推断", "请以公司公告与股东自身披露为准"]

    # 若本地有股息数据，作为统一补充证据（案例里未覆盖时）
    if attraction and not any(r.get("source", "").startswith("本地分红画像") for r in reasons):
        reasons.append({
            "title": "标的股息与分红特征（本地计算）",
            "detail": f"{attraction['latest_year']} 年 DPS {attraction['latest_dps']} 元、"
                      f"股息率 {attraction['latest_yield']}%，{attraction.get('stability') or ''}。",
            "tag": "客观数据", "source": attraction["source"],
        })

    hist = _holder_history(code, holder_name)
    out = {
        "ok": True,
        "code": code,
        "company": company_name,
        "holder": holder_name,
        "token": token,
        "nature": nature,
        "industry": industry,
        "company_industry": company_industry,
        "entity": {
            "listed_code": (meta or {}).get("listed", ""),
            "listed_name": (meta or {}).get("listed_name", ""),
            "relation": (meta or {}).get("relation", ""),
            "controller": (meta or {}).get("controller", ""),
            "industry": (meta or {}).get("industry", industry),
            "confidence": (meta or {}).get("confidence", "低（未匹配到核实主体）"),
        },
        "verdict": verdict,
        "chain": chain,
        "reasons": reasons,
        "evidence": evidence,
        "owner_facts": owner_facts,
        "attraction": attraction,
        "history": hist,
        "caveats": caveats,
        "cache": "miss",
        "disclaimer": "主体关系与持股动机为公开资料整理与规则推断，非公司正式披露，不构成投资建议。",
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 股东全市场持仓反查
# 核心字号过泛时会误匹配大量无关主体（如"中国""基金"），这些词不用于同集团识别
_GENERIC_TOKENS = {
    "中国", "上海", "北京", "深圳", "广州", "杭州", "宁波", "珠海", "香港", "天津", "重庆",
    "中央", "结算", "证券", "基金", "银行", "保险", "信托", "资管", "资产", "投资", "控股",
    "集团", "股份", "有限", "公司", "合伙", "自有", "资金", "养老", "社保", "证金", "汇金",
    "中央结算", "代理人", "互联互通", "陆股通", "北向", "普通账户", "信用账户",
}


def holder_portfolio(holder, limit=400):
    """回答「这个股东还投资了哪些股票」。

    从全市场十大股东缓存反查，分两层：
      - direct：该股东实体本身的持仓
      - affiliated：核心字号相同的同集团关联主体（如雅戈尔控股/集团/服装控股）的持仓
    只覆盖进入前十大股东的公开披露持仓，更小的持股不可见。
    """
    import controller_reader as cr

    raw = str(holder or "").strip()
    name = re.sub(r"[\s　]", "", raw)
    out = {
        "ok": False, "holder": raw, "rows": [], "summary": {},
        "entity": {}, "note": "仅统计全市场上市公司前十大股东中公开披露的持股，"
                              "未进入前十大的持仓不在本表；同一集团关联主体按核心字号匹配，需自行核实。",
    }
    if not name:
        out["error"] = "股东名称为空"
        return out
    try:
        index = cr.build_holder_index()
    except Exception as e:
        out["error"] = "全市场股东索引读取失败：" + str(e)[:100]
        return out

    def _n(s):
        return re.sub(r"[\s　]", "", str(s or ""))

    # 1) 精确命中；无精确时用双向包含兜底（穿透面板名称与缓存可能有细微差异）
    direct_keys, direct_rows = [], []
    for k, v in index.items():
        if _n(k) == name:
            direct_keys.append(k)
            direct_rows.extend(v)
    if not direct_keys and len(name) >= 4:
        for k, v in index.items():
            nk = _n(k)
            if len(nk) >= 4 and (name in nk or nk in name):
                direct_keys.append(k)
                direct_rows.extend(v)

    # 2) 同集团关联主体：核心字号匹配（ENTITY_MAP 有核实时置信度高，否则仅字号匹配）
    token, meta = _match_entity(raw)
    aff_keys = []
    if token and len(token) >= 2 and token not in _GENERIC_TOKENS:
        for k, v in index.items():
            if k in direct_keys or _n(k) == name:
                continue
            if token in k or token in _n(k):
                aff_keys.append(k)
    out["entity"] = {
        "token": token or "",
        "listed_code": (meta or {}).get("listed", ""),
        "listed_name": (meta or {}).get("listed_name", ""),
        "relation": (meta or {}).get("relation", ""),
        "controller": (meta or {}).get("controller", ""),
        "confidence": (meta or {}).get("confidence", "低（仅核心字号匹配，需核实）"),
        "direct_keys": direct_keys,
        "affiliated_keys": aff_keys[:20],
    }

    rows = []
    for r in direct_rows:
        rows.append({**r, "holder_name": raw, "layer": "direct",
                     "relation": "该股东本身"})
    # 同集团记录用索引 key 作为真实主体名，确保「谁持有」准确
    for k in aff_keys:
        for r in index[k]:
            rows.append({**r, "holder_name": k, "layer": "affiliated",
                         "relation": "同集团主体：" + k})

    seen = set()
    uniq = []
    for r in rows:
        key = (r.get("code"), r.get("holder_name"), r.get("rank"), r.get("ratio"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda x: (-(x.get("ratio") or 0), x.get("code") or ""))
    total = len(uniq)
    out["rows"] = uniq[:limit]
    out["truncated"] = total > limit

    codes = {r["code"] for r in uniq}
    direct_codes = {r["code"] for r in uniq if r["layer"] == "direct"}
    aff_codes = {r["code"] for r in uniq if r["layer"] == "affiliated"}
    shares = sum((r.get("shares") or 0) for r in uniq if r["layer"] == "direct")
    top = uniq[0] if uniq else {}
    dates = sorted({r.get("end_date") or r.get("report_date") or "" for r in uniq if r.get("end_date") or r.get("report_date")})
    out["summary"] = {
        "stock_count": len(codes),
        "direct_count": len(direct_codes),
        "affiliated_count": len(aff_codes),
        "record_count": total,
        "top_stock": top.get("stock", ""),
        "top_code": top.get("code", ""),
        "top_ratio": top.get("ratio"),
        "direct_shares": shares,
        "latest_report": dates[-1] if dates else "",
        "group_confidence": out["entity"]["confidence"],
    }
    out["ok"] = bool(uniq)
    return out
