# -*- coding: utf-8 -*-
"""
daily_runner.py — 每日投资决策自动计算调度

设计目标：
  1. 服务启动后自动拉起一轮决策计算，持仓股排在最前，强制重算，保证每天至少一次。
  2. 服务连续运行多天时不漏算：每 5 分钟巡检，自然日变化 / 日线推进到新交易日 /
     持仓列表变化 / 当日快照缺失，任一成立即重跑一轮。
  3. 后端持有持仓镜像（由前端同步），因此无需浏览器在线也能完成计算。
  4. 全市场 9500+ 只股票首次计算需 5~7 秒/只，不适合启动时跑；默认只算
     「持仓 + 已有账本/显式加入观察列表」的股票。全量任务另行按需触发。

状态文件：data/decision_autorun_status.json（供接口与人工排查）
"""
import os
import json
import time
import threading
import datetime as dt

import decision_history
import secure_store
from us_reader import is_us_code, norm_code

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
WATCH_PATH = os.path.join(DATA_DIR, "decision_watchlist.json")
STATUS_PATH = os.path.join(DATA_DIR, "decision_autorun_status.json")

START_DELAY = 5        # 启动后延迟秒数，等待日线索引构建完成
CHECK_INTERVAL = 300   # 巡检周期（秒）
MAX_WATCH = 300        # 观察列表上限，防止误操作导致启动任务过长

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD = None
_READER = None

_STATUS = {
    "running": False,
    "last_run": None,
    "last_run_date": None,
    "last_as_of": None,
    "holdings": [],
    "holdings_hash": "",
    "results": [],
    "error": None,
    "started": False,
}


# ---------------- 持久化 ----------------
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _norm_code(v):
    c = str(v or "").strip()
    if c.endswith(".SH") or c.endswith(".SZ") or c.endswith(".BJ"):
        c = c[:6]
    # 美股 ticker（字母开头）原样保留并规范化，避免被当作空串丢弃
    if is_us_code(c):
        return norm_code(c)
    c = "".join(ch for ch in c if ch.isdigit())
    return c.zfill(6) if c and len(c) <= 6 else ""


def load_holdings():
    """从加密存储读取持仓代码列表（去重、保持顺序）。解密失败时返回空列表并保留错误状态。"""
    try:
        rows = secure_store.load_portfolio()["holdings"]
    except secure_store.SecureStoreError as e:
        _STATUS["last_error"] = str(e)
        return []
    out, seen = [], set()
    for item in rows:
        code = _norm_code(item.get("code") if isinstance(item, dict) else item)
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def load_portfolio():
    """供接口读取完整持仓与账户名录（已解密）。"""
    return secure_store.load_portfolio()


def sync_holdings(holdings, accounts=None):
    """同步持仓与账户名录到加密文件。返回最终参与自动计算的代码列表。"""
    if not isinstance(holdings, list):
        raise ValueError("holdings 应为列表")
    saved = secure_store.save_portfolio(holdings, accounts)
    codes, seen = [], set()
    for row in saved["holdings"]:
        code = _norm_code(row.get("code"))
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    with _LOCK:
        _STATUS["holdings"] = codes
        _STATUS.pop("last_error", None)
    return codes


def load_watchlist():
    raw = _load_json(WATCH_PATH, None)
    if raw is None:
        # 首次运行：把已有账本的代码作为观察列表，避免从零开始。
        codes = []
        try:
            for fn in os.listdir(decision_history.HISTORY_DIR):
                if fn.startswith("backtest_") or not fn.endswith(".json"):
                    continue
                code = fn[:-5]
                if code.isdigit() and len(code) == 6:
                    codes.append(code)
        except Exception:
            pass
        codes = codes[:MAX_WATCH]
        _save_json(WATCH_PATH, {"codes": codes})
        return codes
    if isinstance(raw, dict):
        raw = raw.get("codes") or []
    out, seen = [], set()
    for item in raw or []:
        code = _norm_code(item)
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out[:MAX_WATCH]


def add_watch(code):
    code = _norm_code(code)
    if not code:
        raise ValueError("代码无效")
    codes = load_watchlist()
    if code not in codes:
        codes.append(code)
    codes = codes[-MAX_WATCH:]
    _save_json(WATCH_PATH, {"codes": codes})
    return codes


def remove_watch(code):
    code = _norm_code(code)
    codes = [c for c in load_watchlist() if c != code]
    _save_json(WATCH_PATH, {"codes": codes})
    return codes


# ---------------- 计算 ----------------
def _compute(reader, code, force):
    t0 = time.time()
    try:
        rec, created = decision_history.ensure_snapshot(reader, code, force=force)
        return {
            "code": code,
            "name": rec.get("name", ""),
            "created": bool(created),
            "score": rec.get("composite"),
            "sec": round(time.time() - t0, 2),
            "ok": True,
        }
    except Exception as e:
        return {"code": code, "ok": False, "error": str(e)[:160],
                "sec": round(time.time() - t0, 2)}


def run_once(reader=None, force_holdings=True):
    """执行一轮：持仓股优先且强制重算，观察列表仅在缺当日快照时补齐。"""
    global _READER
    reader = reader or _READER
    if reader is None:
        return {"ok": False, "error": "行情数据未初始化"}
    with _LOCK:
        if _STATUS["running"]:
            return {"ok": False, "error": "上一轮仍在运行", "status": status()}
        _STATUS["running"] = True
        _STATUS["error"] = None
        _STATUS["results"] = []

    t0 = time.time()
    try:
        hold = load_holdings()
        watch = [c for c in load_watchlist() if c not in hold]
        results = []

        # 第一优先级：持仓股。每次运行强制重算，确保日线更新后当天结果是最新的。
        for code in hold:
            results.append(_compute(reader, code, force=True))

        # 第二优先级：观察列表。ensure_snapshot 按 as_of 判断，已有当日快照会直接复用。
        for code in watch:
            results.append(_compute(reader, code, force=False))

        with _LOCK:
            _STATUS["results"] = results
            _STATUS["holdings"] = hold
            _STATUS["holdings_hash"] = ",".join(hold)
            _STATUS["last_run"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _STATUS["last_run_date"] = dt.date.today().isoformat()
            _STATUS["last_as_of"] = _as_of_of(reader, hold[0]) if hold else None
        out = {"ok": True, "count": len(results), "holdings": hold,
               "elapsed": round(time.time() - t0, 2),
               "fail": sum(1 for r in results if not r.get("ok"))}
    except Exception as e:
        with _LOCK:
            _STATUS["error"] = str(e)[:300]
        out = {"ok": False, "error": str(e)[:300]}
    finally:
        with _LOCK:
            _STATUS["running"] = False
        _save_json(STATUS_PATH, dict(_STATUS))
    return out


def _as_of_of(reader, code):
    try:
        return decision_history._as_of(reader, code)
    except Exception:
        return None


def status():
    """当前自动运行状态（接口输出）。"""
    with _LOCK:
        s = dict(_STATUS)
        s["results"] = list(_STATUS.get("results") or [])
    s["holdings_from_disk"] = load_holdings()
    s["watch_count"] = len(load_watchlist())
    return s


def _need_run(reader):
    today = dt.date.today().isoformat()
    if _STATUS.get("last_run_date") != today:
        return True, "新自然日"
    if _STATUS.get("holdings_hash") != ",".join(load_holdings()):
        return True, "持仓列表变化"
    hold = load_holdings()
    if hold:
        as_of = _as_of_of(reader, hold[0])
        if as_of and as_of != _STATUS.get("last_as_of"):
            return True, "日线推进到新交易日"
        book = decision_history._load_book(hold[0])
        if as_of and as_of not in book.get("records", {}):
            return True, "持仓当日快照缺失"
    return False, ""


def _loop(reader):
    _STOP.wait(START_DELAY)
    while not _STOP.is_set():
        need, why = _need_run(reader)
        if need:
            print(f"[daily_runner] 触发每日决策计算：{why}")
            run_once(reader)
        _STOP.wait(CHECK_INTERVAL)


def start(reader):
    """服务启动时调用，拉起后台守护线程。"""
    global _THREAD, _READER
    _READER = reader
    if reader is None:
        return False
    if _THREAD is not None and _THREAD.is_alive():
        return True
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(reader,), daemon=True,
                               name="daily-decision-runner")
    _THREAD.start()
    with _LOCK:
        _STATUS["started"] = True
    return True


def stop():
    _STOP.set()


def trigger_async():
    """供接口手动触发一轮（异步）。"""
    if _READER is None:
        return False
    th = threading.Thread(target=run_once, args=(_READER,), daemon=True,
                          name="daily-decision-manual")
    th.start()
    return True
