# -*- coding: utf-8 -*-
"""
indicators.py — 技术指标计算（纯 pandas/numpy 实现，不依赖 TA-Lib）

提供：
  MA / EMA / MACD / KDJ / RSI / BOLL / 成交量均线 / ATR / 布林带
所有函数输入 DataFrame（含 open/high/low/close/volume），原地增加指标列并返回。
"""
import numpy as np
import pandas as pd


def add_ma(df, periods=(5, 10, 20, 60, 120, 250)):
    for p in periods:
        if len(df) >= p:
            df[f"ma{p}"] = df["close"].rolling(p, min_periods=p).mean()
    return df


def add_ema(df, periods=(12, 26)):
    for p in periods:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2
    return df


def add_kdj(df, n=9, m1=3, m2=3):
    """向量化 KDJ：用 ewm 实现 SMA 递归，等价 K_t=(m1-1)/m1*K_{t-1}+1/m1*RSV。"""
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    df["k"] = k
    df["d"] = d
    df["j"] = 3 * k - 2 * d
    return df


def add_rsi(df, periods=(6, 12, 24)):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    for p in periods:
        avg_gain = gain.ewm(alpha=1 / p, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        df[f"rsi{p}"] = rsi.fillna(50)
    return df


def add_boll(df, n=20, k=2):
    mid = df["close"].rolling(n, min_periods=n).mean()
    # 通达信/主流行情软件的 BOLL 用总体标准差(ddof=0)；pandas 默认 ddof=1 会高估约 2.6%，
    # 导致上下轨与券商软件显示不一致。这里显式 ddof=0 对齐。
    std = df["close"].rolling(n, min_periods=n).std(ddof=0)
    df["boll_mid"] = mid
    df["boll_up"] = mid + k * std
    df["boll_low"] = mid - k * std
    return df


def add_volume_ma(df, periods=(5, 10, 20)):
    for p in periods:
        df[f"vma{p}"] = df["volume"].rolling(p, min_periods=p).mean()
    return df


def add_bias(df, periods=(6, 12, 24)):
    """BIAS 乖离率 = (收盘 - MA(n)) / MA(n) × 100。正=高于均线，负=低于。"""
    for p in periods:
        ma = df["close"].rolling(p, min_periods=p).mean()
        df[f"bias{p}"] = (df["close"] - ma) / ma * 100
    return df


def add_wr(df, n=14):
    """威廉指标 WR(n) = (Hn - 收盘) / (Hn - Ln) × 100。通达信口径：<20 超买，>80 超卖。"""
    hh = df["high"].rolling(n, min_periods=n).max()
    ll = df["low"].rolling(n, min_periods=n).min()
    rng = hh - ll
    df["wr14"] = np.where(rng > 0, (hh - df["close"]) / rng * 100, np.nan)
    df.loc[rng <= 0, "wr14"] = np.nan
    return df


def add_roc(df, n=12):
    """ROC 变动率 = (收盘 - n日前收盘) / n日前收盘 × 100。"""
    prev = df["close"].shift(n)
    df["roc12"] = (df["close"] - prev) / prev * 100
    return df


def add_obv(df):
    """OBV 能量潮：收盘上涨日累计成交量、下跌日减。衡量量能流向。"""
    direction = np.sign(df["close"].diff())
    direction = direction.fillna(0)
    df["obv"] = (direction * df["volume"]).cumsum()
    return df


def add_atr(df, n=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / n, adjust=False).mean()
    return df


def enrich(df, light=False):
    """一次性附加指标。light=True 时只算选股/信号必需的指标，用于全市场扫描提速。"""
    if df.empty:
        return df
    df = df.copy()
    if light:
        add_ma(df, periods=(5, 10, 20))
        add_macd(df)
        add_kdj(df)
        add_rsi(df, periods=(6,))
        add_boll(df)
        add_volume_ma(df, periods=(5,))
    else:
        add_ma(df)
        add_ema(df)
        add_macd(df)
        add_kdj(df)
        add_rsi(df)
        add_boll(df)
        add_volume_ma(df)
        add_atr(df)
        add_bias(df)
        add_wr(df)
        add_roc(df)
        add_obv(df)
    return df


def latest_indicators(df):
    """返回最新一根 K 线的指标快照（可 JSON 序列化）。"""
    if df.empty:
        return {}
    row = df.iloc[-1]
    cols = ["close", "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd",
            "k", "d", "j", "rsi6", "rsi12", "boll_up", "boll_mid", "boll_low",
            "volume", "vma5", "atr",
            "bias6", "bias12", "bias24", "wr14", "roc12", "obv"]
    out = {}
    for c in cols:
        if c in df.columns:
            v = row[c]
            out[c] = None if pd.isna(v) else round(float(v), 3)
    return out
