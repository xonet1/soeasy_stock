# -*- coding: utf-8 -*-
"""
decision_engine.py — 投资决策引擎（可执行信号，非投资建议）

【定位】
把系统已有的全部可量化信息，融合为一个明确的、可执行的、可解释的操作信号：
    买入 / 卖出 / 持有 / 增持 / 减持，以及信号的「增强 / 减弱」。
这是对 synthesis.py（只做决策辅助、不输出买卖）的能力升级。所有结论标注来源、
权重与数据新鲜度，任何一维缺失自动降权而非中断；全程带置信度与风险揭示。

【信息源 → 维度】（共 12 维加权 + 2 个调节器，各自归一化到 0-100，50 为中性）
  1. 技术面 technical      w=0.15  指标投票 + 近期买卖信号 + k线位置（本地日线，实时）
  2. 趋势预测 prediction   w=0.11  次日上涨概率 p_up（历史相似日 kNN + 六维研判）
  3. 量能 volume           w=0.07  量价配合、放量/缩量状态（本地日线）
  4. 基本面 fundamental    w=0.17  近8年财报健康分（ROE/增长/现金流/负债/分红）
  5. 估值 valuation        w=0.10  PE/PB + PEG + 格雷厄姆安全边际（多模型）
  6. 舆情 sentiment        w=0.08  新闻/公告/研报情绪分
  7. 股东 holder           w=0.06  增减持家数、集中度变化、控制权
  8. 国内政策 policy       w=0.04  国内政策快讯主题情绪
  9. 国际形势 intl         w=0.05  全球财经资讯 + 地缘冲突/制裁/汇率/能源/科技管制
 10. 供给关系 supply       w=0.04  产业链上下游景气度（近20日涨幅代理）+ 公司链位置剪刀差
 11. 行业相对强弱 industry w=0.08  个股20日超额收益 vs 所属行业板块（132个行业板块本地日线）
 12. 风险波动 risk         w=0.05  年化波动/ATR/最大回撤/Beta（低波优先，反向压制仓位）
  + 大盘环境 market_env          不占权重，作「总闸门」：弱市向 50 收缩综合分并压低置信度
  + 信号可信度 reliability        不占权重，作乘数调节技术类三项权重与置信度

【缜密计算】
  - 加权：score = Σ(w_i × score_i) / Σw_i（仅对可用维度），避免缺失维度稀释
  - 增强/减弱：对每个有历史序列的维度，比较「近期均值」与「前期均值」的斜率方向，
    汇总为 momentum（动量）：连续改善 → 信号增强，连续恶化 → 信号减弱
  - 共振：≥3 个维度同向且置信度足 → 提升信号等级；维度分歧 → 降级为观望
  - 置信度：由「可用维度权重覆盖度 × 数据新鲜度 × 维度一致性」三者合成

【诚实边界】
  - 全部为公开/本地数据的机械化量化结果，非预测保证，不构成任何投资建议
  - 历史相似条件不代表未来；财务数据有报告期滞后；舆情/政策为词典与规则映射
  - 不设目标价、不做收益承诺；单股信号不替代完整的基本面调研与风险承受能力评估
"""
import math
import os
import time
import json
import traceback

import numpy as np

import indicators
import signals as sig_mod
import volume_analysis
import tdx_reader

# 各模块延迟导入，缺依赖/离线时不影响其它维度
_sentiment_ok = True
_fin_ok = True
_models_ok = True
_holder_ok = True
_predict_ok = True
_external_ok = True
try:
    import sentiment as sent_mod
    import news_reader
except Exception:
    _sentiment_ok = False
try:
    import fin_report
except Exception:
    _fin_ok = False
try:
    import models_reader
except Exception:
    _models_ok = False
try:
    import holder_reader
except Exception:
    _holder_ok = False
try:
    import predict_reader
except Exception:
    _predict_ok = False
try:
    import policy_reader, geo_risk
except Exception:
    _external_ok = False
try:
    import chain_reader
    _chain_ok = True
except Exception:
    _chain_ok = False
try:
    import market_env
    _market_ok = True
except Exception:
    _market_ok = False


# 维度权重（12 维，总和 1.0）
#   policy=国内政策 / intl=国际形势 / supply=供给关系
#   industry=行业相对强弱（新） / risk=风险波动（新）
DIM_WEIGHTS = {
    "technical": 0.15,
    "prediction": 0.11,
    "volume": 0.07,
    "fundamental": 0.17,
    "valuation": 0.10,
    "sentiment": 0.08,
    "holder": 0.06,
    "policy": 0.04,
    "intl": 0.05,
    "supply": 0.04,
    "industry": 0.08,
    "risk": 0.05,
}
DIM_LABELS = {
    "technical": "技术面", "prediction": "趋势预测", "volume": "量能",
    "fundamental": "基本面", "valuation": "估值", "sentiment": "舆情",
    "holder": "股东", "policy": "国内政策", "intl": "国际形势", "supply": "供给关系",
    "industry": "行业相对强弱", "risk": "风险波动",
}
BUY_TYPES = {"macd_golden", "ma_golden", "breakout20", "volume_burst",
             "kdj_oversold", "boll_lower"}
SELL_TYPES = {"macd_death", "ma_death", "kdj_overbought", "boll_upper"}

# 技术类维度：受「信号可信度」因子调节权重（可信度低则降权）
TECH_DIMS = ("technical", "prediction", "volume")
_HIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "decision_history")


def _clamp(v, lo=0.0, hi=100.0):
    try:
        return round(max(lo, min(hi, float(v))), 1)
    except Exception:
        return 50.0


def _f(v, default=None):
    try:
        x = float(v)
        return default if x != x else x
    except Exception:
        return default


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------- 信号可信度因子（第 9 因子） ----------------

def _reliability(code, as_of=None):
    """信号可信度：读历史回放缓存（decision_history.backtest_signal 落盘），
    评估「技术面/预测/量能」这套择时逻辑在该股身上的历史有效性。

    返回 (trust 0-100, label, detail, evidence)。缓存缺失/过期返回 None。
    纯本地读文件，不联网；与 decision_history 解耦，避免循环依赖。
    """
    fp = os.path.join(_HIST_DIR, f"backtest_{str(code).zfill(6)}.json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            cache = json.load(f)
        # 缓存版本校验：日线更新到新交易日后旧回放失效
        ver = str(cache.get("version") or "")
        ver_day = ver.split("_")[0] if ver else ""
        if as_of and ver_day and ver_day != as_of:
            return None
        stats = ((cache.get("payload") or {}).get("stats")) or {}
        acc = _f(stats.get("accuracy"))
        n = int(stats.get("directional_n") or 0)
        buckets = {b.get("label"): b for b in (stats.get("score_buckets") or [])}
        hi = lo = None
        for k, b in buckets.items():
            if k and "偏多" in k:
                hi = _f(b.get("avg_ret"))
            elif k and "偏空" in k:
                lo = _f(b.get("avg_ret"))
        if acc is None and (hi is None or lo is None):
            return None
        # 打分：50 为基准。评分区分度（偏多档-偏空档）权重最高，其次方向命中率
        score = 50.0
        parts = []
        delta = None
        if hi is not None and lo is not None:
            delta = hi - lo
            d_clip = max(-1.0, min(1.0, delta / 1.5))
            score += 30 * d_clip
            parts.append(f"分档差 {delta:+.2f}%")
        if acc is not None:
            a_clip = max(-1.0, min(1.0, (acc - 50) / 15.0))
            score += 20 * a_clip
            parts.append(f"命中率 {acc:.1f}%")
        if n < 30:
            score -= (30 - n) * 0.5
            parts.append(f"样本偏少({n})")
        score = round(max(0.0, min(100.0, score)), 1)
        if score >= 70:
            label = "高"
        elif score >= 55:
            label = "中"
        elif score >= 40:
            label = "偏弱"
        else:
            label = "失效"
        ev = [{"text": f"信号可信度 {score:.0f}/100（{label}）· " + " · ".join(parts),
               "dir": "buy" if score >= 70 else ("sell" if score < 40 else "neutral")}]
        if delta is not None and delta < 0:
            ev.append({"text": "历史评分与收益反向，技术类信号不可靠", "dir": "sell"})
        return score, label, " · ".join(parts), ev
    except Exception:
        return None


def _dim(score, detail, evidence, weight, fresh="real", available=True, trend=None):
    """统一维度结构。score=None 表示该维度不可用。"""
    return {
        "key": "", "label": "", "score": _clamp(score) if score is not None else None,
        "weight": weight, "detail": detail, "evidence": evidence or [],
        "fresh": fresh, "available": bool(available) and score is not None,
        "trend": trend,  # 'improving' / 'weakening' / 'flat' / None
    }


# ---------------- 各维度计算 ----------------

def _technical(reader, code, df=None):
    """技术面：多指标投票 + 近期信号 + 价格位置。返回 (score, detail, evidence, trend)。"""
    try:
        d = df if df is not None else reader.read_recent_cached(code, n=260)
        if d is None or len(d) < 60:
            return None, "本地行情不足", [], None
        e = indicators.enrich(d.copy())
        last = e.iloc[-1]
        votes, ev = [], []
        # 均线排列
        if all(c in e.columns for c in ("ma5", "ma10", "ma20")):
            bull = last["ma5"] > last["ma10"] > last["ma20"]
            votes.append(1 if bull else -1)
            ev.append({"text": "均线多头排列" if bull else "均线空头排列", "dir": "buy" if bull else "sell"})
        # MACD
        if "dif" in e.columns and "dea" in e.columns:
            bull = last["dif"] > last["dea"]
            votes.append(1 if bull else -1)
            ev.append({"text": "MACD 多头(DIF>DEA)" if bull else "MACD 空头(DIF<DEA)", "dir": "buy" if bull else "sell"})
        # 价格 vs MA20
        if "ma20" in e.columns and last["ma20"] == last["ma20"]:
            above = last["close"] > last["ma20"]
            votes.append(1 if above else -1)
            ev.append({"text": "站上 MA20" if above else "跌破 MA20", "dir": "buy" if above else "sell"})
        # RSI
        rsi = _f(last.get("rsi6"))
        if rsi is not None:
            if rsi < 30:
                votes.append(1); ev.append({"text": f"RSI6={rsi:.0f} 超卖", "dir": "buy"})
            elif rsi > 70:
                votes.append(-1); ev.append({"text": f"RSI6={rsi:.0f} 超买", "dir": "sell"})
        # KDJ
        k = _f(last.get("k"))
        if k is not None:
            if k < 20:
                votes.append(1); ev.append({"text": f"KDJ K={k:.0f} 超卖", "dir": "buy"})
            elif k > 80:
                votes.append(-1); ev.append({"text": f"KDJ K={k:.0f} 超买", "dir": "sell"})
        if not votes:
            return None, "指标未就绪", [], None
        base = 50 + (sum(votes) / len(votes)) * 32
        # 近期信号（近 10 日）加权：买入 +、卖出 -，新信号权重更高
        try:
            rec = sig_mod.detect_signals(d, lookback=10, enriched=e)
            net = 0
            for i, s in enumerate(rec or []):
                w = 1.0 if i >= len(rec) / 2 else 0.6
                if s.get("type") in BUY_TYPES:
                    net += w
                elif s.get("type") in SELL_TYPES:
                    net -= w
                if len(ev) < 12:
                    ev.append({"text": s.get("label", s.get("type", "")), "dir": "buy" if s.get("type") in BUY_TYPES else "sell",
                               "date": s.get("date")})
            base += max(-12, min(12, net * 4))
        except Exception:
            pass
        # 趋势动量：近 5 日 vs 前 5 日 MA20 斜率
        trend = None
        try:
            ma = e["ma20"].dropna()
            if len(ma) >= 12:
                recent_slope = (ma.iloc[-1] / ma.iloc[-6] - 1) * 100
                prev_slope = (ma.iloc[-6] / ma.iloc[-11] - 1) * 100
                if recent_slope > prev_slope + 0.15:
                    trend = "improving"
                elif recent_slope < prev_slope - 0.15:
                    trend = "weakening"
                else:
                    trend = "flat"
        except Exception:
            pass
        return _clamp(base), f"{len(votes)}项指标投票，净票数 {sum(votes):+d}", ev[:12], trend
    except Exception as ex:
        return None, f"技术面计算异常：{ex}"[:80], [], None


def _prediction(reader, code):
    """趋势预测：次日上涨概率。"""
    if not _predict_ok:
        return None, "预测模块不可用", [], None
    try:
        p = predict_reader.predict_next_day(code)
        if not p.get("ok"):
            return None, p.get("error", "预测不可用")[:60], [], None
        up = _f(p.get("p_up"))
        score = _clamp(up) if up is not None else None
        ev = [{"text": f"次日上涨概率 {p.get('p_up')}%（{p.get('label')}）", "dir": "buy" if (up or 50) >= 55 else ("sell" if (up or 50) < 45 else "neutral")}]
        st = p.get("stats") or {}
        if st.get("n"):
            ev.append({"text": f"历史相似样本 {st['n']} 天，胜率 {st.get('win_rate')}%", "dir": "neutral"})
        detail = f"{p.get('basis', '')} · 评级{p.get('grade')}"
        if p.get("low_confidence"):
            detail += " · 低置信"
        return score, detail, ev, None
    except Exception as ex:
        return None, f"预测计算异常：{ex}"[:80], [], None


def _volume(reader, code, df=None):
    """量能：六维研判 + 量价配合。"""
    try:
        d = df if df is not None else reader.read_recent_cached(code, n=260)
        if d is None or len(d) < 30:
            return None, "行情不足", [], None
        vd = volume_analysis.daily_verdicts(d, days=5)
        if not vd:
            return None, "量能数据不足", [], None
        scores = [_f(x.get("score")) for x in vd]
        latest = scores[-1] if scores and scores[-1] is not None else None
        trend = None
        if len(scores) >= 3:
            recent = _avg(scores[-3:]); prev = _avg(scores[:-3])
            if recent is not None and prev is not None:
                if recent > prev + 4:
                    trend = "improving"
                elif recent < prev - 4:
                    trend = "weakening"
                else:
                    trend = "flat"
        score = 50 + (latest or 0) * 0.35
        ev = []
        last = vd[-1]
        if last.get("volume_state"):
            ev.append({"text": f"量能状态：{last.get('volume_state')}", "dir": "neutral"})
        label = volume_analysis._verdict_label(latest) if latest is not None else ""
        if label:
            ev.append({"text": f"综合研判：{label}", "dir": "buy" if (latest or 0) > 8 else ("sell" if (latest or 0) < -8 else "neutral")})
        return _clamp(score), f"近5日研判分 {latest}", ev, trend
    except Exception as ex:
        return None, f"量能计算异常：{ex}"[:80], [], None


def _fundamental(code):
    """基本面：直接读8年财报健康分缓存，绝不联网。"""
    if not _fin_ok:
        return None, "财报模块不可用", [], None
    try:
        cache_dir = getattr(fin_report, "CACHE_DIR", None)
        if not cache_dir:
            return None, "财报缓存目录不可用", [], None
        fp = os.path.join(cache_dir, f"fin8_v2_{code}.json")
        if not os.path.exists(fp):
            return None, "财报缓存未采集", [], None
        with open(fp, "r", encoding="utf-8") as f:
            d = json.load(f)
        age_days = (time.time() - os.path.getmtime(fp)) / 86400
        if not d.get("ok"):
            return None, d.get("error") or d.get("message") or "财报不可用", [], None
        sc = _f(d.get("score"))
        if sc is None:
            return None, "健康分缺失", [], None
        ev = [{"text": f"财务健康分 {d.get('score')}/100（{d.get('verdict')}）",
               "dir": "buy" if sc >= 65 else ("sell" if sc < 45 else "neutral")}]
        parts = d.get("parts") or []
        strong = [p for p in parts if p.get("max") and (p.get("score", 0) / p["max"]) >= 0.8]
        weak = [p for p in parts if p.get("max") and (p.get("score", 0) / p["max"]) < 0.35]
        for p in strong[:3]:
            ev.append({"text": f"强项：{p.get('name')}", "dir": "buy"})
        for p in weak[:3]:
            ev.append({"text": f"弱项：{p.get('name')}", "dir": "sell"})
        fresh = "stale" if age_days > 90 else "real"
        return _clamp(sc), f"近8年财报 · {d.get('verdict')} · 数据源 {d.get('source')}", ev, None, fresh
    except Exception as ex:
        return None, f"财报计算异常：{ex}"[:80], [], None


def _valuation(code):
    """估值：直接读 fin/quote 本地缓存 + 纯函数算分，绝不联网。缓存缺失则该维降级。"""
    if not _models_ok:
        return None, "模型模块不可用", [], None
    try:
        cache_dir = getattr(models_reader, "CACHE_DIR", None)
        if not cache_dir:
            return None, "模型缓存目录不可用", [], None
        fp = os.path.join(cache_dir, f"fin_{code}.json")
        qp = os.path.join(cache_dir, f"quote_{code}.json")
        if not (os.path.exists(fp) and os.path.exists(qp)):
            return None, "估值缓存未采集（ fin/quote 缺失）", [], None
        with open(fp, "r", encoding="utf-8") as f:
            fin = json.load(f)
        with open(qp, "r", encoding="utf-8") as f:
            quote = json.load(f)
        if not fin.get("ok"):
            return None, "财务缓存无效", [], None
        # 纯计算函数（无网络）
        fscore = models_reader.calc_fscore(fin)
        zrisk = models_reader.calc_zrisk(fin)
        graham = models_reader.calc_graham(fin, quote)
        peg = models_reader.calc_peg(fin, quote)
        quality = models_reader.calc_quality(fin, quote)
        # 估值合成（与 models_reader.analyze 同口径）
        g_s = None
        if graham:
            mg = graham["margin"] / 100
            g_s = 100 if mg > 0.3 else (70 if mg > 0 else (45 if mg > -0.2 else 20))
        p_s = None
        if peg and peg.get("peg") is not None:
            pv = peg["peg"]
            p_s = 95 if pv < 0.5 else (80 if pv < 1 else (65 if pv < 1.5 else (50 if pv < 2 else (35 if pv < 3 else 15))))
        elif peg and peg.get("growth") is not None and peg["growth"] <= 0:
            p_s = 30
        val_score = models_reader._blend([(g_s, 0.6), (p_s, 0.4)])
        fs_s = fscore["score"] / fscore["of"] * 100 if fscore and fscore.get("of") else None
        z_s = sum(p["score"] for p in zrisk["parts"]) / zrisk["max"] * 100 if zrisk and zrisk.get("max") else None
        total = models_reader._blend([
            (quality["score"] if quality else None, 0.30),
            (fs_s, 0.25),
            (val_score, 0.30),
            (z_s, 0.15),
        ])
        if total is None:
            return None, "估值模型数据不足", [], None
        total = round(total)
        verdict = models_reader.build_verdict(total)
        ev = [{"text": f"估值模型总分 {total} · {verdict}",
               "dir": "buy" if total >= 65 else ("sell" if total < 45 else "neutral")}]
        for k, lab, v in (("quality", "质量", quality["score"] if quality else None),
                          ("valuation", "估值", val_score), ("fscore", "F-Score", fs_s)):
            if v is not None:
                ev.append({"text": f"{lab} {v:.0f}", "dir": "buy" if v >= 65 else ("sell" if v < 45 else "neutral")})
        fresh = "stale" if (time.time() - os.path.getmtime(qp)) > 86400 else "real"
        return _clamp(total), f"本地估值缓存 · {verdict}", ev[:6], None, fresh
    except Exception as ex:
        return None, f"估值计算异常：{ex}"[:80], [], None


def _sentiment(code):
    """舆情：新闻/公告/研报情绪分（读缓存，不强制联网）。"""
    if not _sentiment_ok:
        return None, "舆情模块不可用", [], None
    try:
        pl = {}
        stale = False
        for k in ("news", "notice", "research"):
            b = news_reader._read_cache(k, code) or {}
            pl[k] = b
            stale = stale or bool(b.get("stale"))
        if not any((pl[k].get("items") for k in pl)):
            return None, "舆情缓存为空（未采集）", [], None
        res = sent_mod.analyze(pl)
        sc = _f(res.get("overall_score"))
        if sc is None:
            return None, "情绪分缺失", [], None
        ev = [{"text": f"舆情情绪 {res.get('overall_score')}（{res.get('overall_label')}）· {res.get('total')}条",
               "dir": "buy" if sc >= 62 else ("sell" if sc <= 38 else "neutral")}]
        return _clamp(sc), f"舆情 {res.get('overall_label')} · {res.get('total')}条", ev, None, ("stale" if stale else "real")
    except Exception as ex:
        return None, f"舆情计算异常：{ex}"[:80], [], None, "stale"


def _holder(code):
    """股东：增减持家数、集中度、控制权（读缓存）。"""
    if not _holder_ok:
        return None, "股东模块不可用", [], None
    try:
        deep = holder_reader.fetch_deep(code)
        basic = holder_reader.fetch_holders(code)
        if not deep and not basic:
            return None, "股东数据未采集", [], None
        summary = (deep or {}).get("summary") or {}
        if not summary and basic:
            summary = (basic.get("summary") or {}).get("top", {})
        inc = int(summary.get("increase") or 0)
        dec = int(summary.get("decrease") or 0)
        sc = 50 + (inc - dec) * 5
        ev = [{"text": f"增持 {inc} 家 / 减持 {dec} 家", "dir": "buy" if inc > dec else ("sell" if dec > inc else "neutral")}]
        trend = None
        series = ((deep or {}).get("trend_top") or {}).get("series", [])
        if len(series) >= 2:
            a, b = series[-1].get("cr5"), series[0].get("cr5")
            if a is not None and b is not None:
                delta = a - b
                ev.append({"text": f"CR5 {b:.1f}%→{a:.1f}%", "dir": "buy" if delta > 0 else "sell"})
                sc += max(-8, min(8, delta * 2))
                trend = "improving" if delta > 0.5 else ("weakening" if delta < -0.5 else "flat")
        return _clamp(sc), f"增减持净 {inc-dec:+d} 家", ev[:6], trend
    except Exception as ex:
        return None, f"股东计算异常：{ex}"[:80], [], None


def _policy(reader, code):
    """国内政策：政策快讯主题情绪（只读 policy.json，绝不含全球资讯）。"""
    if not _external_ok:
        return None, "政策模块不可用", [], None
    try:
        pdir = getattr(policy_reader, "CACHE_DIR", None)
        if not pdir:
            return None, "政策缓存目录不可用", [], None
        fp = os.path.join(pdir, "policy.json")
        if not os.path.exists(fp):
            return None, "政策缓存未采集", [], None
        with open(fp, "r", encoding="utf-8") as f:
            b = json.load(f)
        items = b.get("data") or []
        if not items:
            return None, "政策快讯为空", [], None
        try:
            _, industry, profile = policy_reader._company_profile(reader, code)
        except Exception:
            profile, industry = "", ""
        themes = {}
        for theme, kws in policy_reader.THEMES.items():
            hit = [k for k in kws if k in profile]
            if hit:
                themes[theme] = sum(profile.count(k) for k in hit)
        related = [x for x in items if set(x.get("themes", [])) & set(themes.keys())]
        if not related:
            return None, "无相关政策主题匹配", [], None
        avg = round(sum(_f(x.get("score"), 50) for x in related) / len(related), 1)
        ev = [{"text": f"国内政策情绪 {avg}（{len(related)}条相关，主题：{'、'.join(list(themes)[:3]) or '综合'}）",
               "dir": "buy" if avg >= 62 else ("sell" if avg <= 38 else "neutral")}]
        fresh = "stale" if b.get("stale") else "real"
        return _clamp(avg), f"国内政策 {len(related)}条", ev, None, fresh
    except Exception as ex:
        return None, f"国内政策计算异常：{ex}"[:80], [], None


def _intl(reader, code):
    """国际形势：全球财经资讯情绪 + 地缘冲突/制裁/汇率/能源供应/科技管制因子。

    这是原 external 中「地缘 + 全球资讯」部分的独立拆分，与国内政策解耦。
    纯读 geo_events.json / global.json 本地缓存，绝不联网。
    """
    if not _external_ok:
        return None, "国际形势模块不可用", [], None
    parts, ev, stale, hits_total = [], [], False, 0
    # --- A. 全球财经资讯主题情绪（global.json，含海外政策/国际宏观）---
    try:
        pdir = getattr(policy_reader, "CACHE_DIR", None)
        if pdir:
            fp = os.path.join(pdir, "global.json")
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    b = json.load(f)
                items = b.get("data") or []
                stale = stale or bool(b.get("stale"))
                if items:
                    try:
                        _, industry, profile = policy_reader._company_profile(reader, code)
                    except Exception:
                        profile, industry = "", ""
                    themes = {}
                    for theme, kws in policy_reader.THEMES.items():
                        hit = [k for k in kws if k in profile]
                        if hit:
                            themes[theme] = sum(profile.count(k) for k in hit)
                    related = [x for x in items if set(x.get("themes", [])) & set(themes.keys())]
                    if related:
                        avg = round(sum(_f(x.get("score"), 50) for x in related) / len(related), 1)
                        parts.append(avg)
                        ev.append({"text": f"国际资讯情绪 {avg}（{len(related)}条）",
                                   "dir": "buy" if avg >= 62 else ("sell" if avg <= 38 else "neutral")})
    except Exception:
        pass
    # --- B. 地缘事件因子（军事冲突/制裁关税/能源供应/国际金融汇率/科技管制）---
    try:
        gpath = getattr(geo_risk, "_GEO_PATH", None)
        if gpath and os.path.exists(gpath):
            with open(gpath, "r", encoding="utf-8") as f:
                feed = json.load(f)
            events = feed.get("data") or []
            _, industry, profile = geo_risk._company_profile(code)
            text = (industry or "") + " " + (profile or "")
            sens = {}
            for etype, cfg in geo_risk.EVENTS.items():
                ben = [k for k in cfg.get("benefit", []) if k in text]
                harm = [k for k in cfg.get("harm", []) if k in text]
                if ben or harm:
                    s = len(ben) * 2 - len(harm) * 2 + len(ben) - len(harm)
                    sens[etype] = "潜在受益" if s > 0 else ("承压" if s < 0 else "中性")
            if sens:
                delta, hits = 0, 0
                for ev_ in events[:30]:
                    rel = [h for h in ev_.get("events", []) if h.get("type") in sens]
                    for h in rel:
                        stance = sens[h["type"]]
                        if h.get("direction") == "偏空" and stance == "承压":
                            delta -= 6 * h.get("severity", 1) / 3; hits += 1
                        elif h.get("direction") == "偏多" and stance == "潜在受益":
                            delta += 5 * h.get("severity", 1) / 3; hits += 1
                if hits:
                    factor = _clamp(50 + delta)
                    parts.append(factor)
                    hits_total = hits
                    ev.append({"text": f"地缘因子 {factor} · {hits}条相关事件（{'、'.join(list(sens)[:3])}）",
                               "dir": "buy" if factor >= 60 else ("sell" if factor <= 40 else "neutral")})
            stale = stale or bool(feed.get("stale"))
    except Exception:
        pass
    if not parts:
        return None, "国际形势数据未采集", [], None
    fresh = "stale" if stale else "real"
    return _clamp(_avg(parts)), f"{len(parts)}项国际因子（地缘{hits_total}条）", ev[:5], None, fresh


# 产业链位置 → 供给关系方向
_RES_KW = ("煤炭", "石油", "天然气", "原油", "有色", "黄金", "铜", "铝", "锌", "锂", "稀土",
           "矿", "硅料", "多晶硅", "铁矿石", "钢铁", "化工原料", "磷", "钾", "萤石", "石英")
_MFG_KW = ("设备", "零部件", "组件", "代工", "模组", "加工", "制造", "封装", "封测", "材料",
           "元器件", "连接器", "模具", "晶圆", "芯片", "半导体", "面板", "电源", "电池")


def _supply(reader, code):
    """供给关系：产业链上下游景气度 + 公司在链中的位置（剪刀差逻辑）。

    【量化口径】用上游/下游关联公司近 20 日平均涨幅作为「上游成本/景气」与
    「下游需求」的代理指标（本地日线，纯离线）：
      - 上游资源型：上游景气直接受益 → 上游涨幅↑ 得分↑
      - 中游制造型：利润 = 下游需求 − 上游成本 → 剪刀差(下游−上游涨幅)↑ 得分↑
      - 下游应用型：需求驱动 + 成本下降受益
    【诚实边界】关联公司名单来自板块语义映射（规则推断），涨幅是景气代理而非真实
    供需数据（无库存/产能/开工率）；数据缺失时该维降级。
    """
    try:
        cdir = getattr(chain_reader, "CACHE_DIR", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chain_cache")
        rel_fp = os.path.join(cdir, f"related_{code}.json")
        if not os.path.exists(rel_fp):
            return None, "产业链缓存未采集", [], None
        with open(rel_fp, "r", encoding="utf-8") as f:
            rel = json.load(f)
        rdata = rel.get("data") or {}
        up_list = (rdata.get("upstream") or [])[:12]
        down_list = (rdata.get("downstream") or [])[:12]
        if not up_list and not down_list:
            return None, "无上下游关联公司", [], None

        def _avg_ret(lst, n=21):
            rets = []
            for x in lst:
                try:
                    df = reader.read_recent_cached(x.get("code"), n=n)
                    if df is not None and len(df) >= 15:
                        c0, c1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
                        if c0 > 0:
                            rets.append((c1 / c0 - 1) * 100)
                except Exception:
                    pass
            return (sum(rets) / len(rets)) if rets else None, len(rets)

        up_trend, up_n = _avg_ret(up_list)
        down_trend, down_n = _avg_ret(down_list)
        covered = up_n + down_n
        if covered < 4:
            return None, f"关联公司日线覆盖不足({covered}只)", [], None

        # 判断公司在产业链的位置（读主营业务缓存）
        text = ""
        biz_fp = os.path.join(cdir, f"business_{code}.json")
        if os.path.exists(biz_fp):
            try:
                with open(biz_fp, "r", encoding="utf-8") as f:
                    bd = (json.load(f).get("data") or {})
                text = (bd.get("main_business") or "") + " " + " ".join(bd.get("product_types") or [])
            except Exception:
                text = ""
        if any(k in text for k in _RES_KW):
            pos, pos_label = "res", "上游资源型"
        elif any(k in text for k in _MFG_KW):
            pos, pos_label = "mfg", "中游制造型"
        else:
            pos, pos_label = "down", "下游应用型"

        ut = up_trend if up_trend is not None else 0.0
        dt = down_trend if down_trend is not None else 0.0
        score = 50.0
        ev = []
        if pos == "res":
            score += max(-15, min(15, ut * 2.5))
            ev.append({"text": f"上游景气(近20日){ut:+.1f}%，资源型直接受益", "dir": "buy" if ut > 0 else "sell"})
        elif pos == "mfg":
            scissors = dt - ut
            score += max(-15, min(15, scissors * 2.2))
            ev.append({"text": f"利润剪刀差(下游{dt:+.1f}% − 上游{ut:+.1f}%)={scissors:+.1f}%",
                       "dir": "buy" if scissors > 0 else "sell"})
        else:
            score += max(-12, min(12, dt * 2.0)) - max(-6, min(6, ut * 1.0))
            ev.append({"text": f"下游需求(近20日){dt:+.1f}%，上游成本{ut:+.1f}%",
                       "dir": "buy" if dt > 0 else "sell"})
        score = _clamp(score)
        ev.insert(0, {"text": f"产业链位置：{pos_label}", "dir": "neutral"})
        detail = f"{pos_label} · 上游{ut:+.1f}%/下游{dt:+.1f}% · 覆盖{covered}家"
        fresh = "stale" if rel.get("stale") else "real"
        return score, detail, ev[:5], None, fresh
    except Exception as ex:
        return None, f"供给关系计算异常：{ex}"[:80], [], None


# ---------------- 动作与评级 ----------------

def _action(score, resonance_buy, resonance_sell, confidence, coverage=1.0, n_avail=99):
    """综合分 → 可执行动作。共振可升级，分歧会降级。数据不足时强制观望。"""
    if score is None or coverage < 0.35 or n_avail < 3:
        return {"code": "HOLD", "label": "数据不足·观望", "tone": "neutral", "strength": 0,
                "hint": f"仅 {n_avail} 个维度/覆盖 {coverage*100:.0f}%，无法形成可靠信号"}
    # 基础档位
    if score >= 74:
        code, label, tone, strength = "STRONG_BUY", "强烈买入", "buy", 5
    elif score >= 64:
        code, label, tone, strength = "BUY", "买入", "buy", 4
    elif score >= 56:
        code, label, tone, strength = "ADD", "增持/分批建仓", "buy", 3
    elif score >= 46:
        code, label, tone, strength = "HOLD", "持有/观望", "neutral", 2
    elif score >= 38:
        code, label, tone, strength = "REDUCE", "减持", "sell", 3
    elif score >= 28:
        code, label, tone, strength = "SELL", "卖出", "sell", 4
    else:
        code, label, tone, strength = "STRONG_SELL", "强烈卖出", "sell", 5
    hints = {
        "STRONG_BUY": "多维强共振，但仍需分批、设止损，避免一次性重仓",
        "BUY": "信号偏多，可分批介入并设好止损位",
        "ADD": "可小幅增持，等待进一步确认",
        "HOLD": "信号中性，持仓者持有观察，空仓者暂不追",
        "REDUCE": "信号偏空，可先降低仓位",
        "SELL": "信号较弱，建议减仓或退出观望",
        "STRONG_SELL": "多维偏空，注意风险控制",
    }
    hint = hints[code]
    # 分歧降级：多空共振同时出现，或置信度低 → 收敛为观望
    if resonance_buy >= 1 and resonance_sell >= 1 and confidence < 70:
        code, label, tone, strength = "HOLD", "维度分歧·观望", "neutral", 1
        hint = "买卖信号并存，等待方向明朗"
    elif confidence < 45 and tone != "neutral":
        hint += "（数据覆盖不足，置信度偏低，谨慎对待）"
    return {"code": code, "label": label, "tone": tone, "strength": strength, "hint": hint}


def _confidence(dims, coverage):
    """置信度 = 覆盖度 × 新鲜度 × 一致性。"""
    avail = [d for d in dims if d["available"]]
    if not avail:
        return 0, "无可用维度"
    # 新鲜度
    fresh_bonus = {"real": 1.0, "stale": 0.6}.get
    freshness = _avg([fresh_bonus(d.get("fresh", "real"), "real") for d in avail]) or 0
    # 一致性：可用维度分数的标准差越小越一致
    scores = [d["score"] for d in avail if d["score"] is not None]
    if len(scores) >= 2:
        m = sum(scores) / len(scores)
        var = sum((x - m) ** 2 for x in scores) / len(scores)
        std = math.sqrt(var)
        agree = max(0.0, 1 - std / 30.0)
    else:
        agree = 0.5
    conf = round(100 * (0.45 * coverage + 0.25 * freshness + 0.30 * agree))
    conf = max(0, min(100, conf))
    if conf >= 75:
        lv = "高"
    elif conf >= 55:
        lv = "中"
    elif conf >= 35:
        lv = "偏低"
    else:
        lv = "低"
    return conf, f"覆盖{coverage*100:.0f}% · 一致性{agree*100:.0f}% · 新鲜度{freshness*100:.0f}%（{lv}）"


def _industry(reader, code, df=None):
    """行业相对强弱：个股 20 日超额收益 vs 所属行业板块（纯本地，板块指数日线）。"""
    if not _market_ok:
        return None, "行业模块不可用", [], None
    try:
        return market_env.industry_strength(reader, code, df=df)
    except Exception:
        return None, "行业强弱计算失败", [], None


def _risk(reader, code, df=None):
    """风险波动：年化波动率、ATR%、近一年最大回撤、相对沪深300 的 Beta。
    低波动/小回撤得分高（低波因子有长期超额），高波动会同时压低置信度与建议仓位。
    返回五元组 (score, detail, evidence, trend, extra)。"""
    if df is None or len(df) < 40:
        return None, "日线不足，无法评估风险", [], None, None
    try:
        c = df["close"].astype(float).values
        h = df["high"].astype(float).values
        l = df["low"].astype(float).values
        rets = np.diff(c) / c[:-1]
        n = min(20, len(rets))
        vol = float(np.std(rets[-n:], ddof=1)) * math.sqrt(244) * 100
        tr = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = float(np.mean(tr[-n:])) / c[-1] * 100 if c[-1] else None
        seg = c[-250:] if len(c) >= 60 else c
        peak = np.maximum.accumulate(seg)
        mdd = float(np.max((peak - seg) / peak)) * 100 if len(seg) else None

        # Beta：与沪深300 按日期对齐
        beta = None
        try:
            p = os.path.join(getattr(reader, "vipdoc", ""), "sh", "lday", "sh000300.day")
            with open(p, "rb") as f:
                f.seek(-32 * min(len(df), 260), os.SEEK_END)
                arr = np.frombuffer(f.read(), dtype=tdx_reader.DAY_DTYPE)
            idx_close = {str(int(d)): v / 100.0 for d, v in zip(arr["date"], arr["close"])}
            dates = df["date"].astype("int64").values
            pairs = [(float(idx_close[str(int(d))]), float(px))
                     for d, px in zip(dates, c) if str(int(d)) in idx_close]
            if len(pairs) >= 40:
                ic = np.array([x[0] for x in pairs])
                sc_ = np.array([x[1] for x in pairs])
                ir = np.diff(ic) / ic[:-1]
                sr = np.diff(sc_) / sc_[:-1]
                var = float(np.var(ir, ddof=1))
                if var > 0:
                    beta = round(float(np.cov(sr, ir, ddof=1)[0][1] / var), 2)
        except Exception:
            beta = None

        # 打分：低波动加分、大回撤减分
        score = 50.0 + max(-32.0, min(28.0, (35.0 - vol) * 1.05))
        score -= max(0.0, min(18.0, ((mdd or 0) - 18.0) * 1.1))
        score = round(max(0.0, min(100.0, score)), 1)

        ev = []
        if vol >= 45:
            ev.append({"text": f"年化波动 {vol:.0f}%，波动风险偏高", "dir": "sell"})
        elif vol <= 22 and (mdd or 0) <= 20:
            ev.append({"text": f"年化波动 {vol:.0f}%、最大回撤 {mdd:.0f}%，风险可控", "dir": "buy"})
        if (mdd or 0) >= 40:
            ev.append({"text": f"近一年最大回撤 {mdd:.0f}%，回撤风险大", "dir": "sell"})
        if beta is not None and beta >= 1.4:
            ev.append({"text": f"Beta {beta:.2f}，弱市放大跌幅", "dir": "sell"})
        elif beta is not None and beta <= 0.8:
            ev.append({"text": f"Beta {beta:.2f}，弱市相对抗跌", "dir": "buy"})

        trend = "improving" if vol < 25 else ("weakening" if vol > 45 else None)
        desc = f"年化波动{vol:.0f}%，ATR{atr:.1f}%，最大回撤{mdd:.0f}%"
        if beta is not None:
            desc += f"，Beta{beta:.2f}"
        extra = {"vol_annual": round(vol, 1), "atr_pct": round(atr, 1) if atr else None,
                 "max_drawdown": round(mdd, 1) if mdd is not None else None, "beta": beta}
        return score, desc, ev, trend, extra
    except Exception:
        return None, "风险评估失败", [], None, None


def _position(risk_extra, tone, conf, env_score=None):
    """建议仓位：以「组合波动预算 3%」反推单票上限，再用 ATR 给止损位。
    观望/卖出动作不给建仓仓位（只给「若转为买入时的上限」供参考）。"""
    if not risk_extra or not risk_extra.get("vol_annual"):
        return None
    vol = float(risk_extra["vol_annual"])
    if vol <= 0:
        return None
    cap = max(2.0, min(12.0, 3.0 / vol * 100.0))
    atr = risk_extra.get("atr_pct") or (vol / 6.0)
    stop = max(5.0, min(15.0, atr * 2.5))
    if conf is not None and conf < 55:
        cap *= 0.7          # 置信度不足进一步收缩
    if env_score is not None:
        cap *= 0.6 + 0.4 * (env_score / 100.0)   # 弱市压缩仓位
    cap = max(1.0, round(cap, 1))

    if tone in ("sell", "neutral"):
        suggested = 0.0
        note = ("当前动作为卖出/观望，建议仓位 0；"
                f"若后续转为买入，按当前波动率与大盘环境的仓位上限约 {cap:.1f}%。")
    else:
        suggested = cap
        note = "仓位为风险预算折算的机械上限，不含个人风险偏好与组合已有持仓。"
    return {
        "suggested_pct": round(suggested, 1),
        "cap_pct": cap,
        "stop_loss_pct": round(stop, 1),
        "basis": f"波动率{vol:.0f}% → 按3%波动预算折算上限；ATR{atr:.1f}%×2.5 作止损参考",
        "note": note,
    }


def build_decision(reader, code, refresh=False):
    """主入口：融合全部可量化信息，返回可执行投资决策。"""
    code = str(code).zfill(6)
    t0 = time.time()
    try:
        df = reader.read_recent_cached(code, n=260)
    except Exception:
        df = None
    name = ""
    try:
        name = reader.name_of(code)
    except Exception:
        pass

    raw = {}
    raw["technical"] = _technical(reader, code, df)
    raw["prediction"] = _prediction(reader, code)
    raw["volume"] = _volume(reader, code, df)
    raw["fundamental"] = _fundamental(code)
    raw["valuation"] = _valuation(code)
    raw["sentiment"] = _sentiment(code)
    raw["holder"] = _holder(code)
    raw["policy"] = _policy(reader, code)
    raw["intl"] = _intl(reader, code)
    raw["supply"] = _supply(reader, code) if _chain_ok else (None, "产业链模块不可用", [], None)
    raw["industry"] = _industry(reader, code, df)
    risk_val = _risk(reader, code, df)
    raw["risk"] = risk_val[:4]
    risk_extra = risk_val[4] if len(risk_val) > 4 else None

    dims = []
    for key, w in DIM_WEIGHTS.items():
        val = raw.get(key, (None, "未计算", [], None))
        if len(val) == 5:  # _sentiment 五元组（含 fresh）
            sc, detail, ev, trend, fresh = val
        else:
            sc, detail, ev, trend = val
            fresh = "real"
        d = _dim(sc, detail, ev, w, fresh=fresh, trend=trend)
        d["key"], d["label"] = key, DIM_LABELS[key]
        dims.append(d)

    # ---- 第 9 因子：信号可信度，动态调节技术类权重 ----
    as_of = None
    try:
        if df is not None and len(df):
            as_of = str(df["date"].iloc[-1])[:10]
    except Exception:
        pass
    rel = _reliability(code, as_of)
    reliability = None
    rel_factor = 1.0
    if rel:
        t_score, t_label, t_detail, t_ev = rel
        # 可信度 0-100 → 权重系数 0.5-1.0（失效时技术类权重打对折，高可信保持满权）
        rel_factor = 0.5 + 0.5 * (t_score / 100.0)
        for d in dims:
            if d["key"] in TECH_DIMS and d["available"]:
                d["weight"] = round(d["weight"] * rel_factor, 4)
                d["reliability_adjusted"] = True
        reliability = {
            "score": t_score, "label": t_label, "detail": t_detail,
            "factor": round(rel_factor, 3),
            "applies_to": [DIM_LABELS[k] for k in TECH_DIMS],
            "evidence": t_ev,
        }

    avail = [d for d in dims if d["available"]]
    total_w = sum(d["weight"] for d in avail)
    # 覆盖度用「原始权重」衡量数据齐备度，不随之信度调节而假性下降
    coverage = sum(DIM_WEIGHTS[d["key"]] for d in avail)
    if avail:
        composite = sum(d["score"] * d["weight"] for d in avail) / total_w
        composite = round(composite, 1)
    else:
        composite = None
    conf, conf_detail = _confidence(dims, coverage)

    # 信号可信度参与置信度：可信度低 → 整体置信度打折
    if reliability:
        conf = round(conf * rel_factor)
        conf_detail = f"{conf_detail}；信号可信度{reliability['label']}({reliability['score']:.0f})×{rel_factor:.2f}"

    # ---- 第 10 因子：大盘环境，作为「总闸门」而非并列维度 ----
    # 弱市里所有个股信号都该打折：把综合分向 50 收缩，同时压低置信度；强市则基本不干预。
    env = None
    env_factor = 1.0
    if _market_ok:
        try:
            env = market_env.market_environment(reader)
        except Exception:
            env = None
    if env and composite is not None:
        env_factor = round(0.55 + 0.45 * (env["score"] / 100.0), 3)
        composite = round(50 + (composite - 50) * env_factor, 1)
        env_damp = 0.75 + 0.25 * (env["score"] / 100.0)
        conf = max(0, min(100, round(conf * env_damp)))
        conf_detail = f"{conf_detail}；大盘环境{env['label']}({env['score']:.0f})×{env_damp:.2f}"

    # 多空共振统计（score≥62 / ≤38 的维度数）
    rb = [d["label"] for d in avail if d["score"] >= 62]
    rs = [d["label"] for d in avail if d["score"] <= 38]
    action = _action(composite, len(rb), len(rs), conf, coverage, len(avail))
    insufficient = action["code"] == "HOLD" and action["strength"] == 0
    if insufficient:
        composite = None  # 数据不足时不展示误导性分数

    # 建议仓位：只在有明确方向时给，且随波动率/置信度收缩
    position = None
    if not insufficient:
        try:
            position = _position(risk_extra, (action or {}).get("tone"), conf,
                                 env_score=(env or {}).get("score"))
        except Exception:
            position = None

    # 增强/减弱归因：按动量方向汇总维度
    improving = [d["label"] for d in avail if d.get("trend") == "improving"]
    weakening = [d["label"] for d in avail if d.get("trend") == "weakening"]
    # 用综合分相对中性的偏离 + 动量，判定整体信号方向
    if composite is None or insufficient:
        momentum, momentum_label = "flat", "信号待观察"
    elif len(improving) >= len(weakening) + 1 and composite >= 52:
        momentum, momentum_label = "enhancing", "买入信号增强"
    elif len(weakening) >= len(improving) + 1 and composite <= 50:
        momentum, momentum_label = "weakening", "卖出信号增强"
    elif composite >= 62:
        momentum, momentum_label = "enhancing", "偏多信号维持"
    elif composite <= 38:
        momentum, momentum_label = "weakening", "偏空信号维持"
    else:
        momentum, momentum_label = "flat", "信号平稳"

    # 买卖信号清单（从各维度 evidence 汇总，去重）
    buy_sig, sell_sig, seen = [], [], set()
    for d in avail:
        for e in d["evidence"]:
            t = e.get("text")
            if not t or t in seen:
                continue
            seen.add(t)
            row = {"text": t, "source": d["label"]}
            if e.get("dir") == "buy":
                buy_sig.append(row)
            elif e.get("dir") == "sell":
                sell_sig.append(row)

    # 风险提示（基于真实数据）
    risks = []
    if insufficient:
        buy_sig, sell_sig = [], []
        risks.append("可用数据严重不足，未生成任何买卖信号，请勿依据本结果操作")
    if coverage < 0.7:
        risks.append(f"仅 {coverage*100:.0f}% 权重维度可用，结论代表性有限")
    if rs and rb:
        risks.append("多空维度分歧：" + "、".join(rb[:3]) + " vs " + "、".join(rs[:3]))
    if conf < 55:
        risks.append("置信度偏低，建议仅作参考")
    tdim = next((d for d in dims if d["key"] == "technical"), None)
    if tdim and tdim["available"]:
        for e in tdim["evidence"]:
            if "超买" in e.get("text", ""):
                risks.append("短期指标超买，追高风险上升")
                break
    fdim = next((d for d in dims if d["key"] == "fundamental"), None)
    if fdim and fdim["available"] and (fdim["score"] or 50) < 45:
        risks.append("财务健康分偏低，注意基本面风险")
    vdim = next((d for d in dims if d["key"] == "valuation"), None)
    if vdim and vdim["available"] and (vdim["score"] or 50) < 40:
        risks.append("估值模型总分偏低")
    if reliability and reliability["score"] < 55:
        risks.append(f"该股技术类信号历史可信度{reliability['label']}"
                     f"（{reliability['score']:.0f}/100），已自动下调技术面/预测/量能权重，"
                     "请勿单凭技术信号操作")
    if env and env["score"] < 40:
        risks.append(f"大盘环境{env['label']}（{env['score']:.0f}/100），"
                     "系统性风险偏高，个股买入信号已按闸门折扣处理")
    if risk_extra and (risk_extra.get("vol_annual") or 0) >= 45:
        risks.append(f"年化波动{risk_extra['vol_annual']:.0f}%，建议仓位需显著低于常规水平")
    ind_dim = next((d for d in dims if d["key"] == "industry"), None)
    if ind_dim and ind_dim["available"] and (ind_dim["score"] or 50) <= 40:
        risks.append("个股显著弱于所属行业，注意个股自身问题（非行业普跌）")

    recent_trade = None
    try:
        if df is not None and len(df):
            recent_trade = str(df["date"].iloc[-1])[:10]
    except Exception:
        pass

    return {
        "ok": True,
        "code": code,
        "name": name,
        "as_of": recent_trade,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "composite": composite,
        "action": action,
        "momentum": momentum,
        "momentum_label": momentum_label,
        "enhance": improving,
        "weaken": weakening,
        "confidence": conf,
        "confidence_detail": conf_detail,
        "coverage": round(coverage, 3),
        "dimensions": dims,
        "reliability": reliability,
        "market_env": env,
        "position": position,
        "risk_metrics": risk_extra,
        "buy_signals": buy_sig[:12],
        "sell_signals": sell_sig[:12],
        "resonance_buy": rb,
        "resonance_sell": rs,
        "risks": risks,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "disclaimer": (
            "本信号由公开信息与本地行情的机械化量化计算生成，为决策辅助工具，"
            "不构成任何投资建议或收益承诺，不提供目标价。历史相似条件不代表未来，"
            "财务数据存在报告期滞后，舆情与政策为词典/规则映射。请结合公司公告、"
            "行业研究与自身风险承受能力独立决策。市场有风险，投资需谨慎。"
        ),
    }


if __name__ == "__main__":
    import sys
    r = tdx_reader.TdxData()
    for c in (sys.argv[1:] or ["000651", "600519", "300750"]):
        d = build_decision(r, c)
        print(c, d.get("name"), "分", d.get("composite"), "动作", d["action"]["label"],
              "动量", d.get("momentum_label"), "置信", d.get("confidence"),
              "维度", sum(1 for x in d["dimensions"] if x["available"]), "耗时", d.get("elapsed_ms"), "ms")
