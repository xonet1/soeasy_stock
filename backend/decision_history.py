# -*- coding: utf-8 -*-
"""
decision_history.py — 每日投资决策快照账本 + 准确性回测

【两个能力】
  1. 每日快照：以「数据截止交易日 as_of」为 key 固化决策快照。
     当天首次访问生成、当天复用；日线更新到新交易日后自动生成新快照。
     这样每天一份、可回看、可对比，避免每次刷新结果漂移。

  2. 准确性验证（全程本地日线，不联网）：
     a) 账本验证：对每条每日快照，用其 as_of 之后的实际收盘价回填
        T+1/T+3/T+5/T+10 收益，统计各动作的命中率。随时间自然累积样本。
     b) 历史回放：初期账本样本太少，用本地日线在历史每个截面重放
        「技术面 + 量能」信号（这两维可完整还原历史截面），立刻得到
        上百个可验证样本，快速检验择时逻辑的方向准确率与评分区分度。

【诚实边界】
  - 命中率口径：buy 且未来收益>0 算对，sell 且未来收益<0 算对，
    neutral 不计入方向命中（分子分母都排除）。
  - 主口径为 T+5，同时给出 T+1/T+3/T+10；未到期的样本标 None，不参与统计。
  - 历史回放只还原可量化的技术面/量能，不等同于当日 8 维完整决策，
    仅用于检验择时逻辑，不代表完整信号的真实战绩。
"""
import os
import json
import time
import threading

import pandas as pd

import decision_engine
from us_reader import is_us_code, norm_code

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR = os.path.join(BASE_DIR, "data", "decision_history")
_LOCK = threading.RLock()


def _norm(code):
    """快照账本代码规范化：美股 ticker 原样（AAPL），A 股补足 6 位。"""
    s = str(code or "").strip()
    return norm_code(s) if is_us_code(s) else s.zfill(6)
HORIZONS = (1, 3, 5, 10)
PRIMARY = 5
SCHEMA_VERSION = 6  # 账本格式版本：升版后旧快照自动重算升级（v6=12维+市场环境/仓位字段落快照）


def _ensure_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _book_path(code):
    return os.path.join(HISTORY_DIR, f"{_norm(code)}.json")


def _bt_path(code):
    return os.path.join(HISTORY_DIR, f"backtest_{_norm(code)}.json")


def _load_book(code):
    _ensure_dir()
    fp = _book_path(code)
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                b = json.load(f)
            b.setdefault("records", {})
            b.setdefault("name", "")
            b.setdefault("schema", 1)  # 旧账本无版本号，视为 1
            return b
        except Exception:
            pass
    return {"code": _norm(code), "name": "", "records": {}, "schema": SCHEMA_VERSION}


def _save_book(code, book):
    _ensure_dir()
    tmp = _book_path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _book_path(code))


def _as_of(reader, code):
    """当前数据截止交易日（本地日线最新一根）。"""
    try:
        df = reader.read_recent_cached(code, n=5)
        if df is not None and len(df):
            return str(df["date"].iloc[-1])[:10]
    except Exception:
        pass
    return None


def _compact(dec):
    """精简快照：保留回看所需字段，去掉冗长 disclaimer/完整证据。"""
    keep = ("ok", "code", "name", "composite", "action", "momentum", "momentum_label",
            "enhance", "weaken", "confidence", "confidence_detail", "coverage",
            "resonance_buy", "resonance_sell", "risks", "reliability",
            "market_env", "position", "risk_metrics")
    out = {k: dec.get(k) for k in keep}
    out["buy_n"] = len(dec.get("buy_signals") or [])
    out["sell_n"] = len(dec.get("sell_signals") or [])
    out["buy_signals"] = dec.get("buy_signals") or []
    out["sell_signals"] = dec.get("sell_signals") or []
    # 维度保留渲染所需字段（label/score/weight/trend/fresh/detail），
    # detail 是前端维度条下方的说明文字，不能丢；available 供统计用。
    out["dimensions"] = [
        {"label": d.get("label"), "key": d.get("key"), "score": d.get("score"),
         "weight": d.get("weight"), "trend": d.get("trend"), "fresh": d.get("fresh"),
         "detail": d.get("detail"), "available": d.get("available")}
        for d in (dec.get("dimensions") or [])
    ]
    return out


def ensure_snapshot(reader, code, force=False):
    """确保当日快照存在。返回 (快照dict, 是否新建)。"""
    code = _norm(code)
    with _LOCK:
        book = _load_book(code)
        day = _as_of(reader, code)
        rec = book["records"].get(day) if day else None
        # 旧版账本格式（无 ok / 维度缺 detail）→ 自动重算升级
        need_upgrade = bool(rec) and (book.get("schema", 1) < SCHEMA_VERSION
                                      or not rec.get("ok")
                                      or any("detail" not in d for d in (rec.get("dimensions") or [])))
        if rec and not force and not need_upgrade:
            out = dict(rec)
            out["ok"] = True  # 兼容旧账本（早期精简快照未存 ok 字段）
            out["from_snapshot"] = True
            out["snapshot_date"] = day
            return out, False
        # 生成新快照前，确保历史回放缓存存在（可信度因子依赖它）
        try:
            ensure_backtest(reader, code, cur_day=day)
        except Exception:
            pass
        dec = decision_engine.build_decision(reader, code)
        dec["as_of"] = day or dec.get("as_of")
        dec["snapshot_generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if day:
            book["records"][day] = _compact(dec)
        if dec.get("name"):
            book["name"] = dec["name"]
        book["schema"] = SCHEMA_VERSION
        _save_book(code, book)
        out = dict(dec)
        out["from_snapshot"] = False
        out["snapshot_date"] = day
        return out, True


def list_snapshots(code):
    """按日期倒序列出所有每日快照（精简）。"""
    book = _load_book(code)
    rows = []
    for day, rec in sorted(book["records"].items(), reverse=True):
        row = dict(rec)
        row["ok"] = True
        row["as_of"] = day
        rows.append(row)
    return {"ok": True, "code": _norm(code), "name": book.get("name", ""),
            "count": len(rows), "snapshots": rows}


# ---------------- 收益回填与统计 ----------------

def _forward_returns(df, idx):
    if idx is None or df is None or idx < 0 or idx >= len(df):
        return {f"d{n}": None for n in HORIZONS}
    closes = df["close"].values
    out = {}
    for n in HORIZONS:
        j = idx + n
        out[f"d{n}"] = round((float(closes[j]) / float(closes[idx]) - 1) * 100, 2) if j < len(closes) else None
    return out


def _tone_of(rec):
    if "tone" in rec and rec.get("tone"):
        return rec["tone"]
    a = rec.get("action") or {}
    return a.get("tone")


def _stats(records, primary=PRIMARY):
    """通用准确率统计。records: [{tone, forward:{d1..d10}, composite?}]"""
    pk = f"d{primary}"
    by_tone = {}
    directional = []
    valid_scores = []
    for r in records:
        tone = _tone_of(r) or "neutral"
        fw = r.get("forward") or {}
        g = by_tone.setdefault(tone, {"n": 0, "hit": 0, "sum": 0.0, "cnt": 0,
                                      "sum1": 0.0, "cnt1": 0, "sum10": 0.0, "cnt10": 0})
        g["n"] += 1
        ret = fw.get(pk)
        if ret is not None:
            hit = (ret > 0) if tone == "buy" else ((ret < 0) if tone == "sell" else None)
            if hit is not None:
                g["hit"] += 1 if hit else 0
                directional.append(1 if hit else 0)
            g["sum"] += ret; g["cnt"] += 1
            sc = r.get("composite")
            if sc is not None:
                valid_scores.append((float(sc), ret, tone))
        r1 = fw.get("d1")
        if r1 is not None:
            g["sum1"] += r1; g["cnt1"] += 1
        r10 = fw.get("d10")
        if r10 is not None:
            g["sum10"] += r10; g["cnt10"] += 1

    groups = {}
    for tone, g in by_tone.items():
        matured = g["cnt"]  # T+primary 已到期的样本数
        is_dir = tone in ("buy", "sell")
        groups[tone] = {
            "n": g["n"],
            "hit_rate": round(100.0 * g["hit"] / matured, 1) if (is_dir and matured) else None,
            "avg_ret": round(g["sum"] / matured, 2) if matured else None,
            "avg_ret_d1": round(g["sum1"] / g["cnt1"], 2) if g["cnt1"] else None,
            "avg_ret_d10": round(g["sum10"] / g["cnt10"], 2) if g["cnt10"] else None,
            "matured": matured,
        }

    # 评分区分度：按综合分分档看 T+5 平均收益
    buckets = []
    if valid_scores:
        for lo, hi, label in ((0, 42, "偏空(<42)"), (42, 58, "中性(42-58)"), (58, 101, "偏多(≥58)")):
            vals = [ret for sc, ret, _t in valid_scores if lo <= sc < hi]
            if vals:
                buckets.append({"label": label, "n": len(vals),
                                "avg_ret": round(sum(vals) / len(vals), 2),
                                "win_rate": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1)})

    return {
        "primary": pk,
        "total": len(records),
        "directional_n": len(directional),
        "accuracy": round(100.0 * sum(directional) / len(directional), 1) if directional else None,
        "groups": groups,
        "score_buckets": buckets,
    }


def verify_snapshots(reader, code):
    """用真实后续走势验证每日快照，返回 (带收益的快照列表, 统计)。"""
    code = _norm(code)
    book = _load_book(code)
    try:
        df = reader.read_day(code)
    except Exception:
        df = pd.DataFrame()
    dates = df["date"].dt.strftime("%Y-%m-%d").tolist() if len(df) else []
    pos = {d: i for i, d in enumerate(dates)}
    rows = []
    for day, rec in sorted(book["records"].items(), reverse=True):
        row = dict(rec)
        row["as_of"] = day
        row["forward"] = _forward_returns(df, pos.get(day))
        rows.append(row)
    return rows, _stats(rows)


# ---------------- 历史信号回放（快速检验择时逻辑） ----------------

def _version(df):
    if df is None or len(df) == 0:
        return "empty"
    return f"{str(df['date'].iloc[-1])[:10]}_{len(df)}"


def backtest_signal(reader, code, step=5, max_points=160, force=False):
    """在历史每个截面重放技术面+量能信号，验证方向准确率。带文件缓存。"""
    code = _norm(code)
    try:
        df = reader.read_day(code)
    except Exception:
        df = pd.DataFrame()
    if df is None or len(df) < 90:
        return {"ok": False, "error": "本地日线不足（需≥90根）", "records": [], "stats": None}

    ver = _version(df)
    fp = _bt_path(code)
    if not force and os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("version") == ver and cache.get("step") == step:
                payload = cache["payload"]
                # 只缓存耗时的「样本记录」，stats 每次按当前口径现算：
                # 否则统计逻辑修好后会被旧缓存一直挡住（曾导致中性分档缺失）。
                payload["stats"] = _stats(payload.get("records") or [])
                return {"ok": True, "cached": True, **payload}
        except Exception:
            pass

    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    points = list(range(60, len(df) - 11, step))[-max_points:]
    records = []
    for i in points:
        sub = df.iloc[:i + 1]
        try:
            t = decision_engine._technical(reader, code, df=sub)
            v = decision_engine._volume(reader, code, df=sub)
        except Exception:
            continue
        ts, vs = t[0], v[0]
        if ts is None and vs is None:
            continue
        if ts is not None and vs is not None:
            combined = ts * 0.7 + vs * 0.3
        else:
            combined = ts if ts is not None else vs
        combined = round(float(combined), 1)
        tone = "buy" if combined >= 58 else ("sell" if combined <= 42 else "neutral")
        records.append({
            "as_of": dates[i],
            "composite": combined,
            "tech": ts, "volume": vs,
            "tone": tone,
            "forward": _forward_returns(df, i),
        })

    stats = _stats(records)
    payload = {"records": records, "stats": stats, "step": step, "points": len(points)}
    _ensure_dir()
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": ver, "step": step, "payload": payload}, f, ensure_ascii=False)
    os.replace(tmp, fp)
    return {"ok": True, "cached": False, **payload}


def ensure_backtest(reader, code, force=False, cur_day=None):
    """确保回放缓存可用。缓存有效时只做轻量校验（读 n=5 日线 + json），
    日线更新到新交易日或首次访问才真正重放。返回是否本次重新生成。"""
    code = _norm(code)
    if force:
        backtest_signal(reader, code, force=True)
        return True
    cur = cur_day or _as_of(reader, code)
    if cur and os.path.exists(_bt_path(code)):
        try:
            with open(_bt_path(code), "r", encoding="utf-8") as f:
                c = json.load(f)
            ver_day = str(c.get("version") or "").split("_")[0]
            if ver_day == cur:
                return False
        except Exception:
            pass
    backtest_signal(reader, code, force=True)
    return True


def summary(reader, code):
    """一次性返回：每日快照验证 + 历史回放验证，供前端总览。"""
    code = _norm(code)
    snaps, snap_stats = verify_snapshots(reader, code)
    bt = backtest_signal(reader, code)
    return {
        "ok": True,
        "code": code,
        "snapshot_stats": snap_stats,
        "snapshots": snaps[:60],
        "backtest": {"stats": bt.get("stats"), "records": (bt.get("records") or [])[-80:],
                     "cached": bt.get("cached", False), "error": bt.get("error")},
    }
