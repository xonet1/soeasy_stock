# -*- coding: utf-8 -*-
"""
market_env.py
市场环境（大盘择时/宽度）与行业相对强弱。**纯本地、零联网**。

数据来源：
  T0002/hq_cache/tdxzs.cfg   板块定义（名称|板块代码|…|T行业码），132 个行业板块
  T0002/hq_cache/tdxhy.cfg   个股 → 通达信行业码（覆盖 98.6% 个股）
  vipdoc/sh|sz/lday/*.day    指数/板块指数日线（880xxx 行业、881xxx 概念均有本地日线）

两个能力：
  1) market_environment()  大盘环境分：4 大指数趋势 + 板块宽度（站上 MA20 比例）
                            → 作为决策引擎的「总闸门」，弱市自动压缩多空偏离与置信度
  2) industry_strength()   个股 20 日收益 − 所属行业板块 20 日收益 = 超额强弱
                            → 区分「个股自身强」还是「只是行业在涨」

设计约束：不联网、不依赖 pandas（用 numpy 直读二进制），结果按交易日缓存。
"""
import os
import glob
import json
import time

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "market_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 与 tdx_reader 保持一致的日线结构（32 字节/条）
_DAY_DTYPE = np.dtype([
    ("date", "<i4"), ("open", "<i4"), ("high", "<i4"), ("low", "<i4"),
    ("close", "<i4"), ("amount", "<f4"), ("volume", "<i4"), ("reserved", "<i4"),
])

# 大盘基准：上证、深成、创业板、沪深300
_INDEXES = (
    ("sh", "000001", "上证指数"),
    ("sz", "399001", "深证成指"),
    ("sz", "399006", "创业板指"),
    ("sh", "000300", "沪深300"),
)

_T_IND = None        # {T行业码: (行业名, 板块代码)}
_CODE_IND = None     # {个股代码: T行业码}
_MAP_TS = 0.0
_MAP_TTL = 3600      # 映射表 1 小时刷新一次（客户端行业调整不频繁）


# ---------------- 映射表解析 ----------------
def _root(reader):
    return getattr(reader, "root", None) or "C:/zd_gjzq"


def load_maps(reader, force=False):
    """解析板块与行业映射。返回 (T行业码→(名称,板块代码), 个股→T行业码)。"""
    global _T_IND, _CODE_IND, _MAP_TS
    if not force and _T_IND is not None and time.time() - _MAP_TS < _MAP_TTL:
        return _T_IND, _CODE_IND
    t_map, c_map = {}, {}
    hq = os.path.join(_root(reader), "T0002", "hq_cache")
    try:
        with open(os.path.join(hq, "tdxzs.cfg"), "rb") as f:
            for line in f.read().decode("gbk", "ignore").splitlines():
                p = line.split("|")
                if len(p) >= 6:
                    tcode = p[-1].strip()
                    if len(tcode) >= 5 and tcode[0] == "T" and tcode[1:].isdigit():
                        t_map[tcode] = (p[0].strip(), p[1].strip())
    except Exception:
        pass
    try:
        with open(os.path.join(hq, "tdxhy.cfg"), "rb") as f:
            for line in f.read().decode("gbk", "ignore").splitlines():
                p = line.split("|")
                if len(p) >= 3:
                    code, tcode = p[1].strip(), p[2].strip()
                    if len(code) == 6 and code.isdigit() and tcode.startswith("T"):
                        c_map[code] = tcode
    except Exception:
        pass
    _T_IND, _CODE_IND, _MAP_TS = t_map, c_map, time.time()
    return t_map, c_map


# ---------------- 日线读取（numpy 直读，带端读优化） ----------------
def _read_tail(path, n=40):
    """只读文件尾部 n 条记录，返回 (dates, closes, highs, lows)。"""
    try:
        size = os.path.getsize(path)
        if size < 32 * 5:
            return None
        n = min(n, size // 32)
        with open(path, "rb") as f:
            f.seek(-32 * n, os.SEEK_END)
            buf = f.read(32 * n)
        arr = np.frombuffer(buf, dtype=_DAY_DTYPE)
        if arr.size < 5:
            return None
        return (arr["date"].astype("int64"),
                arr["close"].astype("float64") / 100.0,
                arr["high"].astype("float64") / 100.0,
                arr["low"].astype("float64") / 100.0)
    except Exception:
        return None


def _idx_path(reader, market, code):
    return os.path.join(_root(reader), "vipdoc", market, "lday", f"{market}{code}.day")


def _board_path(reader, board_code):
    """板块指数文件：先找沪市，再找深市。"""
    for m in ("sh", "sz"):
        p = _idx_path(reader, m, board_code)
        if os.path.exists(p):
            return p
    return None


def _pct(a, b):
    try:
        if not b:
            return None
        return (a / b - 1.0) * 100.0
    except Exception:
        return None


# ---------------- 能力 1：大盘环境 ----------------
def _index_score(dates, closes):
    """单指数打分：站上 MA20 + MA20 斜率 + 20 日涨幅。50 为中性。"""
    if closes is None or len(closes) < 25:
        return None
    ma20 = float(np.mean(closes[-20:]))
    ma20_prev = float(np.mean(closes[-25:-5]))
    last = float(closes[-1])
    above = last >= ma20
    slope = (ma20 / ma20_prev - 1.0) * 100.0 if ma20_prev else 0.0
    ret20 = (last / float(closes[-21]) - 1.0) * 100.0 if len(closes) > 21 else 0.0
    sc = 50.0 + (10.0 if above else -10.0)
    sc += max(-12.0, min(12.0, slope * 4.0))
    sc += max(-8.0, min(8.0, ret20 * 0.8))
    return {
        "score": round(max(0.0, min(100.0, sc)), 1),
        "above_ma20": bool(above),
        "ret20": round(ret20, 2),
        "slope5": round(slope, 2),
    }


def _breadth(reader):
    """板块宽度：多少行业/概念板块站上 MA20、当日多少板块上涨。
    用 880/881 板块指数代理全市场宽度——全市场个股尾读约 16 秒，板块 1119 个约 1 秒。"""
    paths = []
    for m in ("sh", "sz"):
        paths += glob.glob(os.path.join(_root(reader), "vipdoc", m, "lday", f"{m}88*.day"))
    total = above = up = 0
    for p in paths:
        t = _read_tail(p, 22)
        if not t:
            continue
        _, c, _, _ = t
        if len(c) < 21:
            continue
        total += 1
        if c[-1] >= float(np.mean(c[-20:])):
            above += 1
        if c[-1] > c[-2]:
            up += 1
    if not total:
        return None
    above_r, up_r = above / total, up / total
    # 站上 MA20 比例是中期宽度，当日上涨比例是短期情绪，7:3 合成
    sc = (above_r * 0.7 + up_r * 0.3) * 100.0
    return {
        "score": round(sc, 1),
        "boards": total,
        "above_ma20": above,
        "up_today": up,
        "above_ratio": round(above_r * 100, 1),
        "up_ratio": round(up_r * 100, 1),
    }


def _env_cache_path(day):
    return os.path.join(CACHE_DIR, f"env_{day}.json")


def market_environment(reader, force=False):
    """大盘环境分（0-100）。按指数最新交易日缓存，一天只算一次。"""
    try:
        t = _read_tail(_idx_path(reader, "sh", "000001"), 30)
        day = str(int(t[0][-1])) if t else time.strftime("%Y%m%d")
    except Exception:
        day = time.strftime("%Y%m%d")
    day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
    fp = _env_cache_path(day)
    if not force and os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    idx = []
    for mkt, code, label in _INDEXES:
        t = _read_tail(_idx_path(reader, mkt, code), 40)
        if not t:
            continue
        s = _index_score(t[0], t[1])
        if s:
            s["name"], s["code"] = label, code
            idx.append(s)
    br = _breadth(reader)

    parts, ev = [], []
    if idx:
        idx_avg = sum(x["score"] for x in idx) / len(idx)
        parts.append((idx_avg, 0.6))
        upn = sum(1 for x in idx if x["above_ma20"])
        ev.append({"text": f"指数趋势：{upn}/{len(idx)} 个指数站上 MA20",
                   "dir": "buy" if upn >= 3 else ("sell" if upn == 0 else "neutral")})
    if br:
        parts.append((br["score"], 0.4))
        ev.append({"text": f"板块宽度：{br['above_ratio']:.0f}% 板块站上 MA20，"
                           f"当日 {br['up_ratio']:.0f}% 上涨",
                   "dir": "buy" if br["above_ratio"] >= 60 else ("sell" if br["above_ratio"] <= 35 else "neutral")})
    if not parts:
        return None

    tw = sum(w for _, w in parts)
    score = round(sum(s * w for s, w in parts) / tw, 1)
    if score >= 65:
        label, tone = "强势", "buy"
    elif score >= 55:
        label, tone = "偏强", "buy"
    elif score >= 45:
        label, tone = "中性", "neutral"
    elif score >= 35:
        label, tone = "偏弱", "sell"
    else:
        label, tone = "弱势", "sell"

    out = {
        "score": score, "label": label, "tone": tone, "as_of": day,
        "indices": idx, "breadth": br, "evidence": ev,
        "detail": f"大盘环境{label}（{score}/100）：{upn if idx else 0}/{len(idx)} 指数在 MA20 上方"
                  + (f"，{br['above_ratio']:.0f}% 板块走强" if br else ""),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception:
        pass
    return out


# ---------------- 能力 2：行业相对强弱 ----------------
def industry_strength(reader, code, df=None):
    """个股 vs 所属行业板块的 20 日超额收益。

    返回 (score, detail, evidence, trend) 或 (None, 原因, [], None)
    """
    code = str(code).zfill(6)
    t_map, c_map = load_maps(reader)
    tcode = c_map.get(code)
    if not tcode:
        return None, "无行业分类", [], None
    # 优先用最近一级有板块的行业（个股可能挂在二级行业）
    cand = [tcode] + [tcode[:i] for i in (8, 6, 5) if len(tcode) > i]
    name = board = None
    for tc in cand:
        if tc in t_map:
            name, board = t_map[tc]
            break
    if not board:
        return None, f"行业{tcode}无对应板块指数", [], None
    bp = _board_path(reader, board)
    if not bp:
        return None, f"行业板块{board}无本地日线", [], None
    t = _read_tail(bp, 40)
    if not t or len(t[1]) < 22:
        return None, f"行业板块{board}日线不足", [], None
    ind_closes = t[1]
    ind_ret20 = _pct(ind_closes[-1], ind_closes[-21])
    ind_ret5 = _pct(ind_closes[-1], ind_closes[-6])

    if df is None:
        try:
            df = reader.read_recent_cached(code, n=60)
        except Exception:
            df = None
    if df is None or len(df) < 22:
        return None, "个股日线不足", [], None
    closes = df["close"].astype(float).values
    st_ret20 = _pct(closes[-1], closes[-21])
    st_ret5 = _pct(closes[-1], closes[-6])
    if ind_ret20 is None or st_ret20 is None:
        return None, "收益计算失败", [], None

    excess = st_ret20 - ind_ret20
    excess5 = (st_ret5 or 0) - (ind_ret5 or 0)
    # 超额是核心（占大头），行业绝对动量作为辅助（顺风/逆风）
    score = 50.0 + max(-18.0, min(18.0, excess * 2.5)) + max(-8.0, min(8.0, ind_ret20 * 1.2))
    score = round(max(0.0, min(100.0, score)), 1)

    if excess >= 6:
        rel = "显著强于行业"
    elif excess >= 2:
        rel = "略强于行业"
    elif excess > -2:
        rel = "与行业同步"
    elif excess > -6:
        rel = "略弱于行业"
    else:
        rel = "显著弱于行业"

    ev = []
    if excess >= 2:
        ev.append({"text": f"个股20日超额 {excess:+.1f}%（{rel}）", "dir": "buy"})
    elif excess <= -2:
        ev.append({"text": f"个股20日超额 {excess:+.1f}%（{rel}）", "dir": "sell"})
    if ind_ret20 >= 5:
        ev.append({"text": f"行业{name}20日 {ind_ret20:+.1f}%，处于顺风", "dir": "buy"})
    elif ind_ret20 <= -5:
        ev.append({"text": f"行业{name}20日 {ind_ret20:+.1f}%，处于逆风", "dir": "sell"})

    trend = None
    if excess5 > 1 and ind_ret5 is not None and ind_ret5 > 0:
        trend = "improving"
    elif excess5 < -1 and ind_ret5 is not None and ind_ret5 < 0:
        trend = "weakening"

    detail = (f"行业{name}：板块20日{ind_ret20:+.1f}%，个股{st_ret20:+.1f}%，"
              f"超额{excess:+.1f}%（{rel}）")
    return score, detail, ev, trend


def status(reader):
    """自检：映射与板块日线覆盖情况。"""
    t_map, c_map = load_maps(reader, force=True)
    covered = sum(1 for c in c_map if c_map[c] in t_map)
    try:
        env = market_environment(reader)
    except Exception:
        env = None
    return {
        "industries": len(t_map), "mapped_stocks": len(c_map),
        "mapped_with_board": covered, "market_env": env,
    }


if __name__ == "__main__":
    from tdx_reader import TdxData
    r = TdxData()
    t0 = time.time()
    print("status:", json.dumps({k: v for k, v in status(r).items() if k != "market_env"},
                                ensure_ascii=False))
    env = market_environment(r, force=True)
    print(f"环境计算耗时 {time.time()-t0:.2f}s")
    print("env:", env and {k: env[k] for k in ("score", "label", "detail")})
    for c in ("000651", "603019", "000858"):
        print(c, r.name_of(c), industry_strength(r, c)[:2])
