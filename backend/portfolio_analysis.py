# -*- coding: utf-8 -*-
"""
portfolio_analysis.py — 多账户持仓组合分析

相对旧版 /api/portfolio 的严谨性修正：
  1. account 维度：同一 code 可属于多个账户，按 (account, code) 合并重复录入行；
     组合、账户、个股三层分别给出市值/盈亏/权重，跨账户重叠单独列出。
  2. 成本口径：只有全部持仓都填了有效成本，才用总成本锚定净值；
     部分缺失时退化为区间起点归一化并明确警告（旧版会把部分成本当全部成本，净值虚高）。
  3. 时间对齐：以所有持仓都有行情的共同起始日为净值起点，避免晚上市股票
     在早期缺席导致组合市值与净值失真（旧版直接 ffill 后从最早日开始）。
  4. 风险指标：Beta / Alpha / 跟踪误差 / 信息比率 / Sortino / 历史 VaR95 / CVaR95 /
     日胜率 / 最差单日；并给出账户间收益相关性，检验"多账户"是否真的分散。
  5. 集中度：合并同一股票跨账户市值后算 HHI、有效持仓数、Top1/3/5 权重。
  6. 数据质量：代码名称不符、成本缺失、行情缺失、重复录入、时间未对齐全部显式提示。

全部为本地日线计算，不联网。收益指标基于收盘价，不含现金、分红、交易费用与税费。
"""
import math
import numpy as np
import pandas as pd

DEFAULT_ACCOUNT = "默认账户"


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _r(v, n=2):
    try:
        return round(float(v), n)
    except Exception:
        return None


def normalize_holdings(holdings):
    """按 (account, code) 合并重复行；加权平均成本。返回 (rows, warnings)。"""
    rows, warns = [], []
    bucket = {}
    for h in holdings or []:
        if not isinstance(h, dict):
            warns.append("存在格式无效的持仓行，已忽略。")
            continue
        code = str(h.get("code", "") or "").strip()
        code = "".join(ch for ch in code if ch.isdigit())[:6].zfill(6) if code else ""
        if not code or code == "000000":
            warns.append(f"持仓代码无效：{h.get('code')!r}，已忽略。")
            continue
        account = str(h.get("account", "") or "").strip() or DEFAULT_ACCOUNT
        cost = max(0.0, _f(h.get("cost"), 0.0))
        shares = _f(h.get("shares"), 0.0)
        if shares <= 0:
            warns.append(f"{account} / {code} 股数为 0 或负数，已忽略。")
            continue
        key = (account, code)
        name = str(h.get("name", "") or "").strip()
        if key not in bucket:
            bucket[key] = {"account": account, "code": code, "name": name,
                           "input_names": [], "shares": 0.0, "cost_value": 0.0, "dup": 0}
        b = bucket[key]
        b["shares"] += shares
        b["cost_value"] += cost * shares
        b["dup"] += 1
        if name:
            b["name"] = name
            b["input_names"].append(name)
    for b in bucket.values():
        b["cost"] = b["cost_value"] / b["shares"] if b["shares"] else 0.0
        if b["dup"] > 1:
            warns.append(f"{b['account']} / {b['code']} 重复录入 {b['dup']} 行，已合并股数并按加权平均成本计算。")
        rows.append(b)
    return rows, warns


def _metrics(nav, bench=None):
    """基于净值序列计算收益/风险指标。"""
    nav = nav.dropna()
    if nav.empty:
        return {}
    ret = nav.pct_change().dropna()
    n = len(nav)
    total = float(nav.iloc[-1] / nav.iloc[0] - 1) * 100
    years = max(n - 1, 1) / 252.0
    annual = ((float(nav.iloc[-1] / nav.iloc[0])) ** (1 / years) - 1) * 100 if nav.iloc[0] > 0 else 0.0
    vol = float(ret.std() * math.sqrt(252) * 100) if len(ret) > 1 else 0.0
    dd = (nav / nav.cummax() - 1) * 100
    downside = float(ret[ret < 0].std() * math.sqrt(252) * 100) if (ret < 0).any() else 0.0
    q05 = float(np.percentile(ret, 5)) if len(ret) > 5 else None
    out = {
        "interval_return": _r(total), "annual_return": _r(annual),
        "annual_vol": _r(vol), "max_drawdown": _r(dd.min()),
        "sharpe": _r(float(ret.mean() * 252 / (ret.std() * math.sqrt(252))), 3)
                  if len(ret) > 1 and ret.std() > 0 else None,
        "sortino": _r(annual / downside, 3) if downside > 0 else None,
        "var95_daily": _r(-q05 * 100) if q05 is not None else None,
        "cvar95_daily": _r(-float(ret[ret <= q05].mean()) * 100) if q05 is not None else None,
        "win_rate": _r(float((ret > 0).mean() * 100)) if len(ret) else None,
        "best_day": _r(float(ret.max()) * 100) if len(ret) else None,
        "worst_day": _r(float(ret.min()) * 100) if len(ret) else None,
        "observations": n,
        "_ret": ret, "_dd": dd,
    }
    if bench is not None:
        br = bench.pct_change().reindex(ret.index).dropna()
        common = ret.index.intersection(br.index)
        if len(common) > 5:
            pr, brr = ret.loc[common], br.loc[common]
            var = float(brr.var())
            beta = float(pr.cov(brr) / var) if var > 0 else None
            active = pr - brr
            te = float(active.std() * math.sqrt(252) * 100)
            out["beta"] = _r(beta, 3) if beta is not None else None
            out["alpha_annual"] = _r((float(pr.mean()) - beta * float(brr.mean())) * 252 * 100) if beta is not None else None
            out["tracking_error"] = _r(te)
            out["information_ratio"] = _r(float(active.mean() * 252 / (active.std() * math.sqrt(252))), 3)
            out["benchmark_return"] = _r((float(bench.iloc[-1] / bench.iloc[0]) - 1) * 100)
    return out


def analyze(reader, holdings, days=250, benchmark="999999"):
    rows, warns = normalize_holdings(holdings)
    if not rows:
        return {"error": "没有有效持仓"}

    # ---- 行情读取与时间对齐 ----
    series, skip = {}, []
    for b in rows:
        code = b["code"]
        if code in series:
            continue
        try:
            df = reader.read_day(code, recent=int(days))
        except Exception:
            df = None
        if df is None or df.empty:
            skip.append(code)
            continue
        series[code] = df.set_index("date")["close"].astype(float)
    if skip:
        warns.append(f"以下代码无有效行情，未纳入分析：{', '.join(skip)}。")
    if not series:
        return {"error": "持仓股票均无有效数据", "warnings": warns}

    # 共同起始日：所有持仓都有报价的第一天
    firsts = [s.first_valid_index() for s in series.values() if s.first_valid_index() is not None]
    common_start = max(firsts) if firsts else None
    prices = pd.DataFrame(series).dropna(how="all")
    if common_start is not None and common_start in prices.index:
        prices = prices.loc[common_start:]
    prices = prices.ffill()
    if prices.empty or len(prices) < 2:
        return {"error": "共同可交易日不足，无法计算组合指标", "warnings": warns}
    late = [code for code, s in series.items() if s.first_valid_index() is not None and s.first_valid_index() > prices.index[0]]
    if late:
        warns.append(f"以下持仓在回看起点尚未上市/无报价，净值统一从共同起始日 {prices.index[0].date()} 开始计算：{', '.join(sorted(late))}。")

    # ---- 名称核对与成本完整性 ----
    for b in rows:
        if b["code"] not in prices.columns:
            continue
        real = reader.name_of(b["code"]) or ""
        inp = " / ".join(b["input_names"])
        if inp and real and inp != real and inp not in real and real not in inp:
            warns.append(f"{b['account']} / {b['code']} 实际为「{real}」，你填写的是「{inp}」，请核对代码。")
            b["name_mismatch"] = True
        else:
            b["name_mismatch"] = False
        b["real_name"] = real or inp
    valid = [b for b in rows if b["code"] in prices.columns]
    missing_cost = [f"{b['account']}/{b['code']}" for b in valid if b["cost"] <= 0]
    has_full_cost = not missing_cost
    if missing_cost:
        warns.append(f"以下持仓未填成本价（{', '.join(missing_cost)}），组合净值改为按回看区间起点归一化；盈亏金额仅统计已填成本部分，请勿把区间收益当作累计盈亏。")

    # ---- 组合与分账户市值序列 ----
    def value_of(items):
        s = pd.Series(0.0, index=prices.index)
        for b in items:
            if b["code"] in prices.columns:
                s = s + prices[b["code"]] * b["shares"]
        return s

    port_value = value_of(valid)
    accounts = []
    for acc in sorted({b["account"] for b in valid}):
        items = [b for b in valid if b["account"] == acc]
        av = value_of(items)
        cost_sum = sum(b["cost"] * b["shares"] for b in items)
        acc_full_cost = all(b["cost"] > 0 for b in items)
        anav = av / cost_sum if acc_full_cost and cost_sum > 0 else av / av.iloc[0]
        m = _metrics(anav)
        accounts.append({
            "name": acc, "position_count": len(items),
            "total_value": _r(float(av.iloc[-1])), "total_cost": _r(cost_sum) if acc_full_cost else None,
            "total_pnl": _r(float(av.iloc[-1] - cost_sum)) if acc_full_cost else None,
            "total_pnl_pct": _r(float(av.iloc[-1] / cost_sum - 1) * 100) if acc_full_cost and cost_sum > 0 else None,
            "weight_pct": _r(float(av.iloc[-1] / port_value.iloc[-1]) * 100) if port_value.iloc[-1] > 0 else None,
            "nav_base": "cost" if acc_full_cost and cost_sum > 0 else "start",
            "interval_return": m.get("interval_return"), "annual_return": m.get("annual_return"),
            "annual_vol": m.get("annual_vol"), "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe"), "_nav": anav,
        })

    total_value = float(port_value.iloc[-1])
    total_cost = sum(b["cost"] * b["shares"] for b in valid)
    nav = port_value / total_cost if has_full_cost and total_cost > 0 else port_value / port_value.iloc[0]
    nav = nav.replace([np.inf, -np.inf], np.nan).dropna()
    if nav.empty:
        return {"error": "净值序列为空", "warnings": warns}

    # ---- 基准 ----
    bench_curve, bench_ret = None, None
    try:
        bd = reader.read_day(str(benchmark).zfill(6), recent=int(days))
        if bd is not None and not bd.empty:
            b = bd.set_index("date")["close"].astype(float)
            b_norm = (1 + b.pct_change().fillna(0)).cumprod()
            b_norm = b_norm.reindex(nav.index).ffill().bfill()
            bench_curve = (b_norm / b_norm.iloc[0]) * float(nav.iloc[0])
            bench_ret = (float(b_norm.iloc[-1] / b_norm.iloc[0]) - 1) * 100
    except Exception:
        pass
    port_m = _metrics(nav, bench_curve)

    # ---- 个股明细（分账户 + 合并视图） ----
    merged = {}
    for b in valid:
        merged.setdefault(b["code"], {"shares": 0.0, "cost_value": 0.0, "accounts": []})
        g = merged[b["code"]]
        g["shares"] += b["shares"]
        g["cost_value"] += b["cost"] * b["shares"]
        g["accounts"].append(b["account"])
    details = []
    for b in valid:
        code = b["code"]
        last = float(prices[code].iloc[-1])
        mv = last * b["shares"]
        cv = b["cost"] * b["shares"]
        g = merged[code]
        details.append({
            "account": b["account"], "code": code, "name": b["real_name"],
            "name_mismatch": bool(b["name_mismatch"]),
            "cost": _r(b["cost"], 3), "shares": _r(b["shares"]),
            "last": _r(last, 3), "market_value": _r(mv),
            "cost_value": _r(cv), "pnl": _r(mv - cv) if b["cost"] > 0 else None,
            "pnl_pct": _r((last / b["cost"] - 1) * 100) if b["cost"] > 0 else None,
            "weight_pct": _r(mv / total_value * 100) if total_value > 0 else None,
            "merged_shares": _r(g["shares"]),
            "merged_value": _r(last * g["shares"]),
            "merged_weight_pct": _r(last * g["shares"] / total_value * 100) if total_value > 0 else None,
            "account_count": len(set(g["accounts"])),
        })
    details.sort(key=lambda x: (x["account"], -x["market_value"]))

    # ---- 跨账户重叠 ----
    overlap_items = []
    for code, g in merged.items():
        if len(set(g["accounts"])) < 2:
            continue
        last = float(prices[code].iloc[-1])
        per = [{"account": d["account"], "shares": d["shares"], "value": d["market_value"]}
               for d in details if d["code"] == code]
        overlap_items.append({
            "code": code, "name": valid[[b["code"] for b in valid].index(code)]["real_name"],
            "account_count": len(set(g["accounts"])),
            "total_shares": _r(g["shares"]),
            "total_value": _r(last * g["shares"]),
            "weight_pct": _r(last * g["shares"] / total_value * 100) if total_value > 0 else None,
            "accounts": per,
        })
    overlap_items.sort(key=lambda x: -(x["total_value"] or 0))
    overlap_value = sum(x["total_value"] or 0 for x in overlap_items)
    overlap = {"position_count": len(overlap_items), "total_value": _r(overlap_value),
               "weight_pct": _r(overlap_value / total_value * 100) if total_value > 0 else None,
               "items": overlap_items}

    # ---- 集中度（按 code 合并跨账户市值） ----
    code_values = {code: float(prices[code].iloc[-1]) * g["shares"] for code, g in merged.items()}
    weights = {c: v / total_value for c, v in code_values.items()} if total_value > 0 else {}
    hhi = sum(w * w for w in weights.values())
    ranked = sorted(weights.values(), reverse=True)
    concentration = {
        "hhi": _r(hhi, 4), "effective_n": _r(1 / hhi, 2) if hhi > 0 else None,
        "stock_count": len(code_values),
        "top1_pct": _r(ranked[0] * 100) if ranked else None,
        "top3_pct": _r(sum(ranked[:3]) * 100) if ranked else None,
        "top5_pct": _r(sum(ranked[:5]) * 100) if ranked else None,
        "max_weight_pct": _r(ranked[0] * 100) if ranked else None,
    }
    if hhi > 0.25:
        warns.append("持仓集中度很高（HHI>0.25），单一个股风险显著，建议核查是否因多账户重复持有同一股票放大了敞口。")
    if overlap["weight_pct"] and overlap["weight_pct"] > 30:
        warns.append(f"跨账户重复持有的股票占组合市值 {overlap['weight_pct']}%，多账户并未有效分散个股风险。")

    # ---- 相关性：个股 + 账户 ----
    def corr_payload(frame):
        c = frame.pct_change().fillna(0).corr()
        return {"labels": [str(x) for x in c.columns],
                "values": [[_r(float(c.iloc[i, j]), 3) for j in range(len(c))] for i in range(len(c))]}

    acc_navs = {a["name"]: a.pop("_nav") for a in accounts}
    corr = corr_payload(prices)
    accounts_corr = corr_payload(pd.DataFrame(acc_navs)) if len(acc_navs) > 1 else None

    # ---- 曲线输出 ----
    step = max(1, len(nav) // 250)
    curve = []
    for i, (d, v) in enumerate(nav.items()):
        if i % step == 0 or i == len(nav) - 1:
            item = {"date": d.strftime("%Y-%m-%d"), "value": _r(v, 4)}
            if bench_curve is not None and d in bench_curve.index:
                item["benchmark"] = _r(bench_curve[d], 4)
            curve.append(item)

    summary = {
        "account_count": len(accounts), "stock_count": len(code_values),
        "total_value": _r(total_value), "total_cost": _r(total_cost) if has_full_cost else None,
        "total_pnl": _r(total_value - total_cost) if has_full_cost else None,
        "total_pnl_pct": _r((total_value / total_cost - 1) * 100) if has_full_cost and total_cost > 0 else None,
        "nav_base": "cost" if has_full_cost and total_cost > 0 else "start",
        "benchmark": str(benchmark).zfill(6),
        "benchmark_name": reader.name_of(str(benchmark).zfill(6)) or str(benchmark),
        "benchmark_return": _r(bench_ret) if bench_ret is not None else None,
        "start_date": prices.index[0].strftime("%Y-%m-%d"),
        "end_date": prices.index[-1].strftime("%Y-%m-%d"),
    }
    summary.update({k: v for k, v in port_m.items() if not k.startswith("_")})

    return {
        "summary": summary, "details": details, "accounts": accounts,
        "overlap": overlap, "concentration": concentration,
        "accounts_corr": accounts_corr, "correlation": corr,
        "warnings": warns,
        "note": ("净值以全部持仓总成本为基准（1.0=成本），终点即累计盈亏；基准线按同一起点归一化。"
                 if has_full_cost and total_cost > 0 else
                 "部分持仓未填成本，净值按回看区间起点归一化（起点=1.0）；区间收益不等于累计盈亏。"),
        "risk_note": "指标基于收盘价日收益，不含现金、分红、交易费用与税费；Beta/Alpha 相对所选基准，样本区间越短越不稳定。",
        "curve": curve,
        "drawdown": [_r(v) for v in port_m["_dd"].tolist()[::step]],
    }
