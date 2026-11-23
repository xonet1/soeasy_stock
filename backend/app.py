# -*- coding: utf-8 -*-
"""
app.py — 股票分析系统 Flask API 服务

接口：
  GET  /api/status                     数据状态
  GET  /api/search?q=                  股票搜索
  GET  /api/stock/<code>?days=         个股历史+指标
  POST /api/signals                    扫描买卖信号
  POST /api/screen                     条件选股
  POST /api/portfolio                  持仓组合分析
  POST /api/sentiment                  个股舆情分析（异步任务，返回 job_id）
  GET  /api/chain/<code>?refresh=      个股上下游产业链穿透
  GET  /api/holders/<code>?refresh=    个股前十大股东 + 集中度分析
  POST /api/holders/deep               股东深度分析（异步任务，返回 job_id）
  GET  /api/holders/status/<code>      股东采集合规状态
  GET  /api/relation/<code>            个股关联分析（本地行情相关性+相对强弱）
  GET  /api/policy/<code>              个股政策分析（政策快讯规则映射）
  POST /api/synthesis                  多维综合决策辅助（异步，汇总六维度）
  GET  /api/health                     健康检查

仅供个人自用分析。
"""
import os
import sys
import io
import json
import uuid
import threading
import traceback
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory, Response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tdx_reader import TdxData
from indicators import enrich, latest_indicators
from signals import scan_market, SIGNAL_LABELS, FILTER_LABELS
import news_reader
from sentiment import analyze as analyze_sentiment
import chain_reader
import holder_reader
import holder_sentiment
import holder_lookthrough
import controller_reader
import cninfo_reader
import relation_reader
import policy_reader
import synthesis
import map_reader
import models_reader
import predict_reader
import fin_report
import screener
import decision_engine
import decision_history
import daily_runner
import portfolio_analysis
import secure_store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = Flask(__name__, static_folder=None)
try:
    from flask_cors import CORS
    CORS(app)
except Exception:
    pass

reader = None
init_error = None
try:
    reader = TdxData()
except Exception as e:
    init_error = str(e)

# 后台扫描任务状态
JOBS = {}
JOBS_LOCK = threading.RLock()


def _run_scan_job(job_id, mode, filters, signal_types, limit):
    def progress(done, total):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
    try:
        results, meta = scan_market(
            reader, mode=mode, filters=filters,
            signal_types=signal_types, limit=limit, progress_cb=progress)
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": JOBS[job_id]["total"],
                "result": {"items": results, "meta": meta},
            })
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})
            traceback.print_exc()


def _start_scan_job(mode, filters, signal_types, limit):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": 0,
            "result": None, "error": None, "mode": mode,
        }
        # 清理超过 30 个的旧任务
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    th = threading.Thread(
        target=_run_scan_job,
        args=(job_id, mode, filters, signal_types, limit), daemon=True)
    th.start()
    return job_id


def _run_sentiment_job(job_id, code, refresh, parts):
    """舆情异步任务：依次拉取新闻/公告/研报（带缓存），再做情绪分析。"""
    def progress(done, total, msg=""):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
                if msg:
                    JOBS[job_id]["message"] = msg
    try:
        total = len(parts)
        payload = {}
        for i, kind in enumerate(parts):
            progress(i, total, f"正在获取{'新闻' if kind=='news' else ('公告' if kind=='notice' else '研报')}…")
            payload[kind] = news_reader.fetch(kind, code, refresh=refresh)
            progress(i + 1, total)
        progress(total, total, "正在分析情绪…")
        result = analyze_sentiment(payload)
        name = reader.name_of(code) if reader else ""
        name = name or holder_reader._stock_name(code)
        result["code"] = code
        result["name"] = name
        # 股东变动事件 × 舆情：把「谁新进/增持」与相关报道关联解读（失败不影响舆情主结果）
        try:
            hold = holder_reader.fetch_holders(code)
            evs = holder_reader.notable_events(
                hold.get("top") or [], holder_reader._industry_of(name))
            linked = holder_sentiment.link_events(evs, payload, name)
            linked["report_date"] = hold.get("report_date", "")
            result["holder_events"] = linked
        except Exception as e:
            result["holder_events"] = {"events": [], "note": f"股东事件联动暂不可用：{str(e)[:120]}"}
        result["cache"] = news_reader.cache_status(code)
        result["as_of"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "done", "progress": total,
                                 "result": result, "message": "完成"})
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})
            traceback.print_exc()


def _start_sentiment_job(code, refresh, parts):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": len(parts),
            "result": None, "error": None, "mode": "sentiment", "message": "排队中…",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    th = threading.Thread(
        target=_run_sentiment_job, args=(job_id, code, refresh, parts), daemon=True)
    th.start()
    return job_id


def _run_holder_deep_job(job_id, code, refresh, periods):
    """股东深度分析异步任务：多期历史采集（限速合规）+ 控制权/趋势/洞察。"""
    def progress(done, total, msg=""):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
                if msg:
                    JOBS[job_id]["message"] = msg
    try:
        progress(0, 0, "开始采集历史股东数据（限速合规）…")
        result = holder_reader.fetch_deep(
            code, refresh=refresh, periods=periods, progress_cb=progress)
        result["name"] = reader.name_of(code) if reader else ""
        result["compliance"] = holder_reader.fetch_status(code)
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": 1, "total": 1,
                "result": result, "message": "完成",
            })
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})
        traceback.print_exc()


def _start_holder_deep_job(code, refresh, periods):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": 0,
            "result": None, "error": None, "mode": "holder_deep", "message": "排队中…",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    th = threading.Thread(
        target=_run_holder_deep_job, args=(job_id, code, refresh, periods), daemon=True)
    th.start()
    return job_id


def _run_synthesis_job(job_id, code, parts):
    """多维综合分析异步任务：按需汇总各维度（各有缓存，避免重复采集）。"""
    def progress(done, total, msg=""):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
                if msg:
                    JOBS[job_id]["message"] = msg
    try:
        steps = [p for p in parts]
        total = len(steps) + 1
        data = {}
        for i, part in enumerate(steps):
            progress(i, total, f"分析{part}…")
            try:
                if part == "holder":
                    data["basic"] = holder_reader.fetch_holders(code)
                    data["deep"] = holder_reader.fetch_deep(code)
                elif part == "relation":
                    data["relation"] = relation_reader.analyze_relation(reader, code)
                elif part == "policy":
                    data["policy"] = policy_reader.analyze_policy(reader, code)
                elif part == "sentiment":
                    # 舆情需联网爬取，复用 news_reader+sentiment 同步取缓存/采集
                    import news_reader as nr
                    pl = {k: nr.fetch(k, code) for k in ("news", "notice", "research")}
                    data["sentiment_data"] = analyze_sentiment(pl)
            except Exception as pe:
                data[part + "_error"] = str(pe)[:150]
        progress(len(steps), total, "汇总多维信号…")
        result = synthesis.synthesize(
            reader, code,
            sentiment_data=data.get("sentiment_data"),
            deep=data.get("deep"), basic=data.get("basic"),
            relation=data.get("relation"), policy=data.get("policy"))
        result["name"] = reader.name_of(code)
        result["dimension_sources"] = {
            "技术面": "本地通达信日线",
            "舆情面": "东方财富公开资讯",
            "股东面": "东方财富F10公开披露",
            "关联面": "本地行情相关性",
            "政策面": "东方财富政策快讯（规则映射）",
            "产业链": "公开资料知识库",
        }
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": total, "total": total,
                "result": result, "message": "完成",
            })
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})
        traceback.print_exc()


def _clean_json(obj):
    """递归清理 NaN/Inf/timestamp 等非法 JSON 值，避免前端 JSON.parse 失败。"""
    import math
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is pd.NaT:
        return None
    return obj


def ok(data, **extra):
    out = {"ok": True, "data": _clean_json(data)}
    out.update(_clean_json(extra))
    return jsonify(out)


def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def _to_int(v, default):
    """安全 int 转换；非法返回 None（调用方决定 400 或默认值）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@app.errorhandler(Exception)
def _unhandled_error(e):
    """全局兜底：未捕获异常与 HTTP 异常一律返回 JSON（避免 500 HTML 崩溃页打断前端解析）。"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return err(e.description or e.name, e.code)
    traceback.print_exc()
    return err(f"服务器内部错误：{e}", 500)


def df_to_records(df, cols=None, limit=None):
    if df.empty:
        return []
    d = df.copy()
    d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    if cols:
        cols = [c for c in cols if c in d.columns]
        d = d[cols]
    if limit:
        d = d.tail(limit)
    # astype(object) 后再替换，避免 pandas 对 float 列把 None 回填为 NaN
    return d.astype(object).where(pd.notna(d), None).to_dict("records")


@app.route("/api/health")
def health():
    return ok({"status": "up", "init_error": init_error})


@app.route("/api/status")
def status():
    if not reader:
        return err(init_error or "数据未初始化", 500)
    s = reader.status()
    s["version"] = reader.data_version()
    return ok(s)


@app.route("/api/watchlist")
def watchlist():
    """动态读取客户端自选股。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    return ok({"items": reader.read_watchlist(), "version": reader.data_version()})


@app.route("/api/data-version")
def data_version():
    """数据版本指纹，供前端检测客户端是否更新了数据。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    return ok({"version": reader.data_version()})


@app.route("/api/search")
def search():
    if not reader:
        return err(init_error or "数据未初始化", 500)
    q = request.args.get("q", "").strip()
    if not q:
        return ok([])
    names = reader.load_names()
    codes = reader.build_index()
    res = []
    for code, name in names.items():
        if q in code or q.lower() in name.lower():
            res.append({"code": code, "name": name, "has_data": code in codes})
            if len(res) >= 30:
                break
    return ok(res)


def _stock_stats(d):
    """从日线数据计算行情统计（全部纯日线可复算，不含换手率——本地无流通股本数据）。"""
    import math
    c = d["close"]
    n = len(d)
    if n < 2:
        return {}
    last = d.iloc[-1]
    prev_close = float(c.iloc[-2])

    def _r(v, nd=2):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)

    # 振幅：(高-低)/昨收
    amps = []
    for i in range(max(1, n - 5), n):
        pc = float(c.iloc[i - 1])
        if pc > 0:
            amps.append((float(d.iloc[i]["high"]) - float(d.iloc[i]["low"])) / pc * 100)
    amp_today = amps[-1] if amps else None
    amp5 = sum(amps) / len(amps) if amps else None

    # 量比：当日量 / 前5日均量
    v5 = float(d["volume"].iloc[-6:-1].mean()) if n >= 6 else None
    vr = float(last["volume"]) / v5 if v5 and v5 > 0 else None

    # 52周（250日窗口）
    w = d.tail(250)
    w52h, w52l = float(w["high"].max()), float(w["low"].min())
    close = float(last["close"])
    dd_high = (close - w52h) / w52h * 100 if w52h > 0 else None
    from_low = (close - w52l) / w52l * 100 if w52l > 0 else None
    # 价格在近250日的分位（0-100）
    pctile = float((w["close"] < close).sum()) / len(w) * 100 if len(w) else None

    # 区间涨跌幅
    def _chg(k):
        if n > k and float(c.iloc[-1 - k]) > 0:
            return (close / float(c.iloc[-1 - k]) - 1) * 100
        return None

    # 年初至今：基点=上一年最后一根收盘；无则用今年首根开盘
    dates_str = d["date"].astype(str)
    cur_year = str(dates_str.iloc[-1])[:4]
    prev_bars = d[dates_str.str[:4] < cur_year]
    if len(prev_bars):
        ytd_base = float(prev_bars.iloc[-1]["close"])
    elif n >= 2:
        ytd_base = float(d.iloc[0]["open"])
    else:
        ytd_base = None
    ytd = (close / ytd_base - 1) * 100 if ytd_base and ytd_base > 0 else None

    # 近60日上涨天数占比
    diffs = c.diff().tail(60).dropna()
    up_ratio = float((diffs > 0).sum()) / len(diffs) * 100 if len(diffs) else None

    # 连涨/连跌天数（以最近一根的方向）
    streak = 0
    streak_dir = 0
    dd_all = c.diff().dropna()
    if len(dd_all):
        streak_dir = 1 if dd_all.iloc[-1] > 0 else (-1 if dd_all.iloc[-1] < 0 else 0)
        for v in reversed(dd_all.tolist()):
            if (v > 0 and streak_dir == 1) or (v < 0 and streak_dir == -1):
                streak += 1
            else:
                break

    # 未回补跳空缺口（近20日，阈值0.5%）：向上 low>昨high，向下 high<昨low
    gaps = []
    look = d.tail(20).reset_index(drop=True)
    for i in range(1, len(look)):
        hi_prev, lo_prev = float(look.iloc[i - 1]["high"]), float(look.iloc[i - 1]["low"])
        hi, lo = float(look.iloc[i]["high"]), float(look.iloc[i]["low"])
        dt = str(look.iloc[i]["date"])[:10]
        if hi_prev > 0 and lo > hi_prev * 1.005:
            # 向上缺口：后续最低价未跌回缺口下沿则视为未回补
            filled = float(look.iloc[i + 1:]["low"].min() if len(look) > i + 1 else hi_prev) <= hi_prev
            gaps.append({"date": dt, "type": "up", "gap_pct": round((lo / hi_prev - 1) * 100, 2), "filled": filled})
        elif lo_prev > 0 and hi < lo_prev * 0.995:
            filled = float(look.iloc[i + 1:]["high"].max() if len(look) > i + 1 else lo_prev) >= lo_prev
            gaps.append({"date": dt, "type": "down", "gap_pct": round((hi / lo_prev - 1) * 100, 2), "filled": filled})
    gaps = [g for g in gaps if not g["filled"]][-3:]

    # 年化波动率（近60日日收益标准差×√244，样本口径 ddof=1）
    rets = c.pct_change().tail(60).dropna()
    vol_ann = float(rets.std() * math.sqrt(244) * 100) if len(rets) >= 10 else None

    # 成交额
    amount_today = float(last["amount"]) if "amount" in d.columns else None
    amount5 = float(d["amount"].tail(5).mean()) if "amount" in d.columns and n >= 1 else None

    return {
        "amplitude": _r(amp_today), "amplitude5": _r(amp5),
        "volume_ratio": _r(vr, 2),
        "amount_today": _r(amount_today, 0), "amount5": _r(amount5, 0),
        "w52_high": _r(w52h, 3), "w52_low": _r(w52l, 3),
        "drawdown_from_high": _r(dd_high), "gain_from_low": _r(from_low),
        "price_percentile": _r(pctile, 1),
        "chg5": _r(_chg(5)), "chg20": _r(_chg(20)), "chg60": _r(_chg(60)), "chg250": _r(_chg(250)),
        "ytd": _r(ytd),
        "up_ratio_60": _r(up_ratio, 1), "streak": streak, "streak_dir": streak_dir,
        "gaps": gaps,
        "volatility_annual": _r(vol_ann),
    }


@app.route("/api/stock/<code>")
def stock(code):
    if not reader:
        return err(init_error or "数据未初始化", 500)
    try:
        days = min(int(request.args.get("days", 500)), 2000)
    except Exception:
        days = 500
    # 统计窗口独立于展示窗口：至少取 300 根，保证 52 周/250 日等统计始终可算
    win = max(days, 300)
    df = reader.read_day(code, recent=win)
    if df.empty:
        return err(f"未找到 {code} 的数据", 404)
    d = enrich(df)
    bars_df = d.tail(days)
    cols = ["date", "open", "high", "low", "close", "volume", "amount",
            "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd",
            "k", "d", "j", "rsi6", "boll_up", "boll_mid", "boll_low", "vma5",
            "bias6", "bias12", "bias24", "wr14", "roc12", "obv"]
    from signals import detect_signals
    from volume_analysis import volume_records, daily_verdicts
    return ok({
        "code": str(code).zfill(6),
        "name": reader.name_of(code),
        "bars": df_to_records(bars_df, cols),
        "indicators": latest_indicators(d),
        "stats": _stock_stats(d),
        "signals": detect_signals(df, lookback=10),
        "volume_daily": {"records": volume_records(d, 20), "verdicts": daily_verdicts(d, 10)},
    })


@app.route("/api/signals", methods=["POST"])
def signals_api():
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    types = body.get("types") or None
    limit = _to_int(body.get("limit", 60), 60)
    if limit is None:
        return err("limit 参数无效", 400)
    limit = min(limit, 300)
    job_id = _start_scan_job("signals", None, types, limit)
    return ok({"job_id": job_id, "labels": SIGNAL_LABELS})


@app.route("/api/screen", methods=["POST"])
def screen_api():
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    filters = body.get("filters") or []
    limit = _to_int(body.get("limit", 60), 60)
    if limit is None:
        return err("limit 参数无效", 400)
    limit = min(limit, 300)
    job_id = _start_scan_job("filter", filters, None, limit)
    return ok({"job_id": job_id, "labels": FILTER_LABELS})


@app.route("/api/job/<job_id>")
def job_api(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return err("任务不存在", 404)
        snapshot = dict(job)
    out = {
        "status": snapshot["status"],
        "progress": snapshot["progress"],
        "total": snapshot["total"],
        "error": snapshot.get("error"),
    }
    if snapshot.get("message"):
        out["message"] = snapshot["message"]
    if snapshot["status"] == "done" and snapshot.get("result"):
        out["result"] = snapshot["result"]
        if snapshot["mode"] == "signals":
            out["labels"] = SIGNAL_LABELS
        elif snapshot["mode"] == "filter":
            out["labels"] = FILTER_LABELS
    return ok(out)


@app.route("/api/portfolio", methods=["POST"])
def portfolio_api():
    """多账户持仓组合分析。
    body: {holdings:[{code,cost,shares,name,account?}], days:250, benchmark:'999999'}
    account 缺省为「默认账户」；同一股票可分属不同账户，按 (账户,代码) 合并重复录入行。
    """
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    holdings = body.get("holdings", [])
    if not isinstance(holdings, list):
        return err("holdings 应为列表", 400)
    if not holdings:
        return err("持仓列表为空", 400)
    days = _to_int(body.get("days", 250), 250)
    if days is None:
        return err("days 参数无效", 400)
    days = min(max(days, 20), 1000)
    benchmark = str(body.get("benchmark", "999999") or "999999").zfill(6)
    try:
        result = portfolio_analysis.analyze(reader, holdings, days=days, benchmark=benchmark)
    except Exception as e:
        traceback.print_exc()
        return err(f"组合分析失败：{e}", 500)
    if result.get("error"):
        return err(result["error"], 404)
    return ok(result)



@app.route("/api/sentiment", methods=["POST"])
def sentiment_api():
    """个股舆情分析：body {code, parts:['news','notice','research'], refresh:bool}
    返回 job_id，通过 /api/job/<id> 轮询进度与结果。
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    code_raw = str(body.get("code", "")).strip()
    if not code_raw or not code_raw.isdigit() or len(code_raw) > 6:
        return err("请提供有效的股票代码", 400)   # 先校验再 zfill，避免空串变 000000 绕过
    code = code_raw.zfill(6)
    raw_parts = body.get("parts")
    parts = ["news", "notice", "research"] if raw_parts is None else raw_parts
    parts = [p for p in parts if p in ("news", "notice", "research")]
    if not parts:
        return err("请至少选择一类舆情数据", 400)
    refresh = bool(body.get("refresh", False))
    job_id = _start_sentiment_job(code, refresh, parts)
    return ok({"job_id": job_id})


@app.route("/api/chain/<code>")
def chain_api(code):
    """个股上下游产业链穿透：真实数据（公司资料/主营构成）+ AI 推断图层，逐条标注来源。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("请提供有效的股票代码", 400)
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        data = chain_reader.fetch_chain(code, refresh=refresh)
        data["name"] = reader.name_of(code)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"产业链数据获取失败：{e}", 500)


@app.route("/api/finreport/<code>")
def finreport_api(code):
    """近 8 年年报分析：营收/净利/ROE/负债率/分红逐年 + CAGR/现金流含金量 + 长期健康分。"""
    try:
        data = fin_report.fetch_fin8(
            str(code).zfill(6),
            refresh=str(request.args.get("refresh", "")).lower() in ("1", "true", "yes"),
        )
        if data.get("ok") and not data.get("name"):
            try:
                data["name"] = reader.name_of(str(code).zfill(6))
            except Exception:
                pass
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"财报分析失败：{e}", 500)


@app.route("/api/predict/<code>")
def predict_api(code):
    """下一交易日买卖概率评级：历史相似条件统计(kNN) + 六维研判评分 Logistic 融合。"""
    try:
        return ok(predict_reader.predict_next_day(str(code).zfill(6)))
    except Exception as e:
        traceback.print_exc()
        return err(f"概率评级计算失败：{e}", 500)


@app.route("/api/decision/<code>")
def decision_api(code):
    """投资决策引擎：每日快照（当天复用、新交易日自动重算）。
    ?refresh=1 可强制重算当日快照。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    try:
        force = request.args.get("refresh") in ("1", "true", "True")
        dec, created = decision_history.ensure_snapshot(reader, str(code).zfill(6), force=force)
        return ok(dec)
    except Exception as e:
        traceback.print_exc()
        return err(f"投资决策计算失败：{e}", 500)


@app.route("/api/decision/autorun/status")
def decision_autorun_status_api():
    """每日自动决策任务状态（持仓优先、每日一次）。"""
    return ok(daily_runner.status())


@app.route("/api/decision/autorun/now", methods=["POST"])
def decision_autorun_now_api():
    """手动触发一轮每日决策计算（异步），随后查 status 获取进度。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    if not daily_runner.trigger_async():
        return err("自动任务未就绪", 503)
    return ok({"started": True})


@app.route("/api/holdings", methods=["GET"])
def holdings_get_api():
    """读取加密存储的持仓与账户名录（前端不再使用浏览器本地存储）。"""
    try:
        data = daily_runner.load_portfolio()
        return ok(data)
    except Exception as e:
        return err(f"读取加密持仓失败：{e}", 500)


@app.route("/api/holdings/restore", methods=["POST"])
def holdings_restore_api():
    """用上一版加密备份恢复持仓（误覆盖后的兜底）。"""
    try:
        data = secure_store.restore_backup()
        return ok(data)
    except Exception as e:
        return err(f"恢复失败：{e}", 500)


@app.route("/api/holdings/sync", methods=["POST"])
def holdings_sync_api():
    """保存持仓与账户名录到加密文件，供每日自动决策优先计算。"""
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings") if isinstance(body, dict) else None
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(holdings, list):
        return err("holdings 应为列表", 400)
    if accounts is not None and not isinstance(accounts, list):
        return err("accounts 应为列表", 400)
    try:
        old = set(daily_runner.load_holdings())
        codes = daily_runner.sync_holdings(holdings, accounts)
        # 持仓集合变化时立即补算一轮，保证新持仓当天就有快照；
        # 若当前轮仍在运行，5 分钟巡检会依据 holdings_hash 差异兜底。
        if set(codes) != old:
            daily_runner.trigger_async()
        return ok({"codes": codes, "count": len(codes),
                   "accounts": secure_store.load_portfolio()["accounts"]})
    except Exception as e:
        return err(f"持仓保存失败：{e}", 500)


@app.route("/api/decision/watchlist", methods=["GET", "POST"])
def decision_watchlist_api():
    """每日自动计算的观察列表（持仓之外）。POST body: {code, action:'add'|'remove'}"""
    if request.method == "GET":
        return ok({"codes": daily_runner.load_watchlist()})
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "add")).lower()
    try:
        if action == "remove":
            codes = daily_runner.remove_watch(body.get("code", ""))
        else:
            codes = daily_runner.add_watch(body.get("code", ""))
        return ok({"codes": codes})
    except Exception as e:
        return err(str(e), 400)


@app.route("/api/decision/history/<code>")
def decision_history_api(code):
    """每日快照账本：列出所有历史快照，供回看与对比。"""
    try:
        return ok(decision_history.list_snapshots(str(code).zfill(6)))
    except Exception as e:
        traceback.print_exc()
        return err(f"快照账本读取失败：{e}", 500)


@app.route("/api/decision/accuracy/<code>")
def decision_accuracy_api(code):
    """准确性验证：每日快照的真实走势验证 + 历史信号回放。?force=1 跳过回放缓存。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    try:
        force = request.args.get("force") in ("1", "true", "True")
        data = decision_history.verify_snapshots(reader, str(code).zfill(6))
        bt = decision_history.backtest_signal(reader, str(code).zfill(6), force=force)
        return ok({
            "code": str(code).zfill(6),
            "snapshot_stats": data[1],
            "snapshots": data[0][:60],
            "backtest": {"stats": bt.get("stats"), "records": (bt.get("records") or [])[-80:],
                         "cached": bt.get("cached", False), "error": bt.get("error")},
        })
    except Exception as e:
        traceback.print_exc()
        return err(f"准确性验证失败：{e}", 500)


@app.route("/api/picks", methods=["POST"])
def picks_api():
    """量化选股推荐：多因子打分 + 约束过滤 + Top N（异步任务，返回 job_id）。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    top_n = _to_int(body.get("top_n", 20), 20)
    if top_n is None:
        return err("top_n 参数无效", 400)
    top_n = min(max(top_n, 5), 50)
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": 0,
            "result": None, "error": None, "mode": "picks",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)

    def _progress(done, total):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total

    def _work():
        try:
            res = screener.run_picks(reader, top_n=top_n, progress_cb=_progress)
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "done",
                    "progress": JOBS[job_id]["total"] or res.get("valid", 0) or 1,
                    "total": JOBS[job_id]["total"] or res.get("valid", 0) or 1,
                    "result": {"picks": res},
                })
        except Exception as e:
            with JOBS_LOCK:
                JOBS[job_id].update({"status": "error", "error": str(e)})
            traceback.print_exc()

    threading.Thread(target=_work, daemon=True).start()
    return ok({"job_id": job_id})


@app.route("/api/picks/history")
def picks_history_api():
    """历期选股清单跟踪：回填各期从生成日至最近交易日的实际涨跌。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    try:
        return ok(screener.picks_history(reader))
    except Exception as e:
        traceback.print_exc()
        return err(f"选股历史读取失败：{e}", 500)


@app.route("/api/models/<code>")
def models_api(code):
    """多模型购买参考：F-Score/Z风险/格雷厄姆/PEG/质量五维 + 海龟/格兰维尔/动量/波动，综合评分。"""
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        data = models_reader.analyze(str(code).zfill(6), refresh=refresh)
        if data.get("ok") and data.get("name") is None:
            try:
                data["name"] = reader.name_of(str(code).zfill(6))
            except Exception:
                pass
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"模型分析失败：{e}", 500)


@app.route("/api/map/stocks")
def map_stocks_api():
    """全国个股分布：全 A 快照 + 省份/行业/估值多维属性（前端本地聚合）。"""
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        return ok(map_reader.get_map_stocks(refresh=refresh))
    except Exception as e:
        traceback.print_exc()
        return err(f"地图数据加载失败：{e}", 502)


@app.route("/api/map/geo")
def map_geo_api():
    """中国省界 GeoJSON（阿里 DataV 公共边界，本地缓存）。直接返回 GeoJSON。"""
    try:
        data = map_reader.get_geo()
        if not data:
            return err("省界数据获取失败", 502)
        return app.response_class(
            response=json.dumps(data, ensure_ascii=False),
            status=200, mimetype="application/json")
    except Exception as e:
        return err(f"省界数据获取失败：{e}", 502)


def _run_controller_scan_job(job_id, refresh, limit, workers):
    """全市场实控人采集 + 图谱重建（异步任务，进度可轮询）。"""
    def progress(done, total, msg=""):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
                if msg:
                    JOBS[job_id]["message"] = msg
    try:
        codes = controller_reader.list_a_share_codes(reader.build_index() if reader else [])
        progress(0, len(codes), "开始全市场实控人采集（限速合规，可断点续采）…")
        stats = controller_reader.scan_all(
            codes, refresh=bool(refresh), limit=limit, workers=workers, progress_cb=progress)
        progress(stats["total"], stats["total"], "构建投资人关联图谱…")
        # 必须强制重建：graph_summary() 内部走 1 小时缓存，直接调用会读到采集前的旧图谱
        controller_reader.build_graph(refresh=True)
        # 同步刷新股东持仓反查索引（供「这个股东还投资了哪些股票」使用）
        try:
            controller_reader.build_holder_index(refresh=True)
        except Exception:
            pass
        summary = controller_reader.graph_summary()
        stats["graph"] = summary
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": stats["total"], "total": stats["total"],
                "result": stats, "message": "全市场穿透图谱已更新",
            })
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})


def _start_controller_scan_job(refresh=False, limit=None, workers=6):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": 0,
            "result": None, "error": None, "mode": "controller_scan", "message": "排队中…",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    threading.Thread(
        target=_run_controller_scan_job,
        args=(job_id, refresh, limit, workers), daemon=True).start()
    return job_id


def _run_cninfo_scan_job(job_id, refresh, limit, workers, with_pdf):
    """巨潮公告证据层批量补采（异步，进度可轮询）。完成后重建图谱注入证据。"""
    def progress(done, total, msg=""):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = done
                JOBS[job_id]["total"] = total
                if msg:
                    JOBS[job_id]["message"] = msg
    try:
        codes = controller_reader.list_a_share_codes(reader.build_index() if reader else [])
        progress(0, len(codes), "开始巨潮公告证据补采（限速合规，可断点续采）…")
        stats = cninfo_reader.scan_all(
            codes, workers=workers, limit=limit, progress_cb=progress, with_pdf=with_pdf)
        # 证据落盘后强制重建图谱，让 controller 详情带上公告出处
        try:
            controller_reader.build_graph(refresh=True)
        except Exception:
            pass
        stats["cninfo_status"] = cninfo_reader.status()
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": stats["total"], "total": stats["total"],
                "result": stats, "message": "巨潮公告证据层已更新",
            })
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})


def _start_cninfo_scan_job(refresh=False, limit=None, workers=4, with_pdf=True):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": 0,
            "result": None, "error": None, "mode": "cninfo_scan", "message": "排队中…",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    threading.Thread(
        target=_run_cninfo_scan_job,
        args=(job_id, refresh, limit, workers, with_pdf), daemon=True).start()
    return job_id


@app.route("/api/controllers/status")
def controllers_status_api():
    """全市场实控人图谱采集状态。"""
    try:
        return ok(controller_reader.status())
    except Exception as e:
        return err(f"图谱状态读取失败：{e}", 500)


@app.route("/api/controllers/scan", methods=["POST"])
def controllers_scan_api():
    """启动/续采全市场实控人扫描（异步）。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体必须是 JSON 对象", 400)
    refresh = body.get("refresh") in (True, 1, "1", "true", "True")
    workers = body.get("workers", 6)
    limit = body.get("limit")
    try:
        workers = max(1, min(12, int(workers)))
        limit = int(limit) if limit else None
    except Exception:
        return err("workers/limit 参数无效", 400)
    return ok({"job_id": _start_controller_scan_job(refresh, limit, workers)})


@app.route("/api/controllers/list")
def controllers_list_api():
    """实控人榜单摘要。"""
    try:
        return ok({"items": controller_reader.controller_list(request.args.get("limit", 100))})
    except Exception as e:
        return err(f"榜单读取失败：{e}", 500)


@app.route("/api/controllers/detail")
def controllers_detail_api():
    """单个实控人的穿透图谱（ECharts nodes/links）。name 可为实控人或股票代码。"""
    name = str(request.args.get("name", "")).strip()
    if not name:
        return err("缺少 name 参数", 400)
    try:
        d = controller_reader.controller_detail(name)
        if not d:
            return err("未找到该实控人或股票代码", 404)
        return ok(d)
    except Exception as e:
        return err(f"图谱读取失败：{e}", 500)


@app.route("/api/controllers/search")
def controllers_search_api():
    """按实控人或公司名称搜索。"""
    return ok({"items": controller_reader.search(request.args.get("q", ""))})


@app.route("/api/controllers/export")
def controllers_export_api():
    """导出完整穿透图谱 JSON（全部实控人、公司、关联链条）。"""
    try:
        g = controller_reader.build_graph()
        body = json.dumps(g, ensure_ascii=False, indent=1)
        return Response(body, mimetype="application/json; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=controller_graph.json"})
    except Exception as e:
        return err(f"导出失败：{e}", 500)


@app.route("/api/controllers/company/<code>")
def controllers_company_api(code):
    """单只股票的实控人与控制链。"""
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("无效股票代码", 400)
    rec = controller_reader.fetch_one(code)
    if not rec:
        return err("该股票实控人数据采集失败", 502)
    d = controller_reader.controller_detail(rec["controller"])
    return ok({"record": rec, "graph": d})


@app.route("/api/cninfo/status")
def cninfo_status_api():
    """巨潮公告证据层缓存状态。"""
    try:
        return ok(cninfo_reader.status())
    except Exception as e:
        return err(f"证据层状态读取失败：{e}", 500)


@app.route("/api/cninfo/<code>")
def cninfo_one_api(code):
    """单只股票的巨潮公告证据：年报链接、实控人原文、前十大股东、权益变动。"""
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("无效股票代码", 400)
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    with_pdf = request.args.get("pdf", "1") not in ("0", "false")
    try:
        rec = cninfo_reader.fetch_one(code, refresh=refresh, with_pdf=with_pdf)
        return ok(rec or {"code": code, "available": False, "note": "采集失败"})
    except Exception as e:
        return err(f"公告证据采集失败：{e}", 502)


@app.route("/api/cninfo/scan", methods=["POST"])
def cninfo_scan_api():
    """启动巨潮公告证据层批量补采（异步）。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体必须是 JSON 对象", 400)
    workers = body.get("workers", 4)
    limit = body.get("limit")
    with_pdf = body.get("with_pdf", True) not in (False, 0, "0", "false")
    try:
        workers = max(1, min(8, int(workers)))
        limit = int(limit) if limit else None
    except Exception:
        return err("workers/limit 参数无效", 400)
    return ok({"job_id": _start_cninfo_scan_job(limit=limit, workers=workers, with_pdf=with_pdf)})


@app.route("/api/holder/lookthrough")
def holder_lookthrough_api():
    """股东穿透：主体/控制链 → 主体经营结构 → 标的吸引力 → 公开证据 → 动机结论。"""
    code = str(request.args.get("code", "")).strip().zfill(6)
    holder = str(request.args.get("holder", "")).strip()
    if not code.isdigit() or not holder:
        return err("需要有效的 code 与 holder 参数", 400)
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        return ok(holder_lookthrough.lookthrough(code, holder, refresh=refresh))
    except Exception as e:
        traceback.print_exc()
        return err(f"股东穿透分析失败：{e}", 500)


@app.route("/api/holder/portfolio")
def holder_portfolio_api():
    """股东全市场持仓反查：该股东（及同集团主体）还持有哪些上市公司。"""
    holder = str(request.args.get("holder", "")).strip()
    if not holder:
        return err("需要 holder 参数", 400)
    try:
        limit = int(request.args.get("limit", "400"))
    except (TypeError, ValueError):
        return err("limit 必须为整数", 400)
    limit = max(1, min(1000, limit))
    try:
        return ok(holder_lookthrough.holder_portfolio(holder, limit=limit))
    except Exception as e:
        traceback.print_exc()
        return err(f"股东持仓反查失败：{e}", 500)


@app.route("/api/holders/<code>")
def holders_api(code):
    """个股前十大股东：十大股东/十大流通股东 + 集中度/增减持汇总。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("请提供有效的股票代码", 400)
    if code not in reader.build_index():
        return err("代码不存在于本地行情库，无法采集股东数据", 404)  # 无效纯数字代码快速失败，避免联网挂起
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        data = holder_reader.fetch_holders(code, refresh=refresh)
        data["name"] = reader.name_of(code)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"股东数据获取失败：{e}", 500)


@app.route("/api/holders/deep", methods=["POST"])
def holders_deep_api():
    """股东深度分析：多期趋势/控制权/分类/增减持/洞察（异步任务，合规限速）。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    code_raw = str(body.get("code", "")).strip()
    if not code_raw or not code_raw.isdigit() or len(code_raw) > 6:
        return err("请提供有效的股票代码", 400)   # 先校验再 zfill
    code = code_raw.zfill(6)
    refresh = bool(body.get("refresh", False))
    periods = body.get("periods", 8)
    try:
        periods = max(4, min(16, int(periods)))
    except Exception:
        periods = 8
    job_id = _start_holder_deep_job(code, refresh, periods)
    return ok({"job_id": job_id})


@app.route("/api/holders/status/<code>")
def holders_status_api(code):
    """股东采集合规状态（缓存时点、限速参数、审计日志）。"""
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("请提供有效的股票代码", 400)
    return ok(holder_reader.fetch_status(code))


@app.route("/api/relation/<code>")
def relation_api(code):
    """个股关联分析：本地行情相关性 + 相对沪深300强弱。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("请提供有效的股票代码", 400)
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        pool_raw = request.args.get("pool", "")
        pool = [x for x in pool_raw.split(",") if x.strip()] if pool_raw else None
        data = relation_reader.analyze_relation(reader, code, pool=pool, refresh=refresh)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"关联分析失败：{e}", 500)


@app.route("/api/policy/<code>")
def policy_api(code):
    """个股政策分析：政策快讯规则映射到行业/个股，情绪打分。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    code = str(code).strip().zfill(6)
    if not code.isdigit():
        return err("请提供有效的股票代码", 400)
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    try:
        data = policy_reader.analyze_policy(reader, code, refresh=refresh)
        return ok(data)
    except Exception as e:
        traceback.print_exc()
        return err(f"政策分析失败：{e}", 500)


@app.route("/api/synthesis", methods=["POST"])
def synthesis_api():
    """多维综合决策辅助（异步任务，返回 job_id）。定位辅助决策，非荐股。"""
    if not reader:
        return err(init_error or "数据未初始化", 500)
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return err("请求体格式无效", 400)
    code_raw = str(body.get("code", "")).strip()
    if not code_raw or not code_raw.isdigit() or len(code_raw) > 6:
        return err("请提供有效的股票代码", 400)   # 先校验再 zfill
    code = code_raw.zfill(6)
    parts = body.get("parts") or ["holder", "relation", "policy", "sentiment"]
    parts = [p for p in parts if p in ("holder", "relation", "policy", "sentiment")]
    if not parts:
        return err("请至少选择一个分析维度", 400)
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "progress": 0, "total": len(parts) + 1,
            "result": None, "error": None, "mode": "synthesis", "message": "排队中…",
        }
        if len(JOBS) > 30:
            for k in list(JOBS.keys())[:-30]:
                if JOBS[k].get("status") in ("done", "error"):
                    JOBS.pop(k, None)
    th = threading.Thread(
        target=_run_synthesis_job, args=(job_id, code, parts), daemon=True)
    th.start()
    return ok({"job_id": job_id})


def _no_cache(resp):
    """本地开发工具：禁用浏览器缓存，确保前端改动刷新即生效。"""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return _no_cache(send_from_directory(FRONTEND_DIR, "index.html"))


@app.route("/<path:path>")
def static_files(path):
    full = os.path.join(FRONTEND_DIR, path)
    if os.path.isfile(full):
        return _no_cache(send_from_directory(FRONTEND_DIR, path))
    return err("Not found", 404)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Stock Analyzer starting at http://{args.host}:{args.port}")
    if init_error:
        print(f"[WARN] {init_error}")
    else:
        daily_runner.start(reader)   # 启动后自动拉起每日决策计算（持仓优先）
        print("[daily_runner] 每日决策自动计算已启用（5 秒后首轮，持仓优先；其后每 5 分钟巡检）")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
