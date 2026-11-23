# -*- coding: utf-8 -*-
"""
secure_store.py — 持仓与账户名录的加密本地存储

设计目标：
  1. 持仓、账户等敏感数据不再存浏览器 localStorage，也不以明文落盘。
  2. Fernet 主密钥由 os.urandom(32) 随机生成，不依赖机器码/口令/时间等任何因子；
     Windows 上落盘前再用 DPAPI（当前用户凭据）加密，拷走密钥+密文到别的账户/机器也解不开。
     密钥文件默认在用户目录（Windows: %APPDATA%/StockAnalyzer/secret.key），
     可用 STOCK_KEY_FILE 指定其他位置，或用 STOCK_SECRET 直接提供 base64 密钥（不走文件）。
  3. 旧版明文 secret.key 会被自动识别并用 DPAPI 重新加密写回，平滑迁移；
     旧的明文镜像 data/portfolio_holdings.json 首次运行时加密后删除。
  4. 密钥丢失或密文损坏时抛出 SecureStoreError，绝不静默返回空数据，避免覆盖原有持仓。

依赖：cryptography（Fernet = AES-128-CBC + HMAC-SHA256）、Windows DPAPI（crypt32.dll）
"""
import os
import json
import threading

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECURE_DIR = os.path.join(BASE_DIR, "data", "secure")
CIPHER_PATH = os.path.join(SECURE_DIR, "portfolio.enc")
LEGACY_PATH = os.path.join(BASE_DIR, "data", "portfolio_holdings.json")
DEFAULT_ACCOUNT = "默认账户"

_LOCK = threading.RLock()


class SecureStoreError(RuntimeError):
    pass


# ---- Windows DPAPI：把随机生成的 Fernet 主密钥绑定到当前 Windows 用户 ----
_DPAPI_MAGIC = b"STKDPAPI1"
_DPAPI_ENTROPY = b"stock-analyzer-portfolio-key-v1"   # 应用固定熵，隔离同用户下其他程序
_CRYPTPROTECT_UI_FORBIDDEN = 0x01                      # 禁止弹出 UI，适合服务进程

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _dpapi_available():
        return True

    def _to_blob(data):
        buf = ctypes.create_string_buffer(data)
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf

    def _dpapi_protect(data):
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        blob_in, keep_in = _to_blob(data)
        ent_in, keep_ent = _to_blob(_DPAPI_ENTROPY)
        blob_out = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), u"StockAnalyzer portfolio key",
            ctypes.byref(ent_in), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise SecureStoreError("DPAPI 加密失败，错误码 %d" % ctypes.get_last_error())
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    def _dpapi_unprotect(data):
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        blob_in, keep_in = _to_blob(data)
        ent_in, keep_ent = _to_blob(_DPAPI_ENTROPY)
        blob_out = _DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None,
            ctypes.byref(ent_in), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise SecureStoreError("DPAPI 解密失败，错误码 %d" % ctypes.get_last_error())
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
else:
    def _dpapi_available():
        return False

    def _dpapi_protect(data):
        return data

    def _dpapi_unprotect(data):
        return data


def _read_key_bytes(path):
    """读取密钥文件：新格式走 DPAPI，旧明文格式自动迁移为 DPAPI 格式。"""
    with open(path, "rb") as f:
        raw = f.read().strip()
    if not raw:
        raise SecureStoreError("密钥文件为空：%s" % path)
    if raw.startswith(_DPAPI_MAGIC):
        if not _dpapi_available():
            raise SecureStoreError("该密钥文件受 Windows DPAPI 保护，无法在当前平台解密：%s" % path)
        try:
            return _dpapi_unprotect(raw[len(_DPAPI_MAGIC):])
        except SecureStoreError:
            raise
        except Exception as e:
            raise SecureStoreError("DPAPI 解密密钥失败：%s" % e)
    # 旧格式：44 字节 base64 明文 Fernet key
    try:
        Fernet(raw)
    except Exception as e:
        raise SecureStoreError("密钥文件格式无法识别（%s）：%s" % (path, e))
    if _dpapi_available():
        try:
            with open(path, "wb") as f:
                f.write(_DPAPI_MAGIC + _dpapi_protect(raw))
            os.chmod(path, 0o600)
        except Exception:
            pass  # 迁移失败不影响本次读取，下次启动会重试
    return raw


def _write_key_bytes(path, key):
    """写入密钥文件：Windows 上先经 DPAPI 加密，确保文件本身不是明文密钥。"""
    data = _DPAPI_MAGIC + _dpapi_protect(key) if _dpapi_available() else key
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def key_path():
    """密钥文件位置：默认在用户目录，不在项目目录内。"""
    custom = os.environ.get("STOCK_KEY_FILE", "").strip()
    if custom:
        return custom
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "StockAnalyzer", "secret.key")
    return os.path.join(os.path.expanduser("~"), ".config", "stock-analyzer", "secret.key")


def _fernet(create=True):
    secret = os.environ.get("STOCK_SECRET", "").strip()
    if secret:
        try:
            return Fernet(secret.encode("ascii"))
        except Exception as e:
            raise SecureStoreError(f"STOCK_SECRET 不是有效的 Fernet 密钥：{e}")
    path = key_path()
    if os.path.isfile(path):
        try:
            return Fernet(_read_key_bytes(path))
        except SecureStoreError:
            raise
        except Exception as e:
            raise SecureStoreError(f"密钥文件无效（{path}）：{e}")
    if not create:
        raise SecureStoreError(f"密钥文件不存在：{path}。请恢复备份或设置 STOCK_SECRET。")
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o700)
    except OSError:
        pass
    key = Fernet.generate_key()
    try:
        _write_key_bytes(path, key)
    except SecureStoreError:
        raise
    except OSError as e:
        raise SecureStoreError(f"无法写入密钥文件 {path}：{e}")
    return Fernet(key)


def _norm_code(v):
    c = str(v or "").strip()
    if c.endswith((".SH", ".SZ", ".BJ")):
        c = c[:6]
    c = "".join(ch for ch in c if ch.isdigit())
    return c.zfill(6) if c and len(c) <= 6 else ""


def _normalize(holdings, accounts=None):
    rows, seen = [], set()
    for item in holdings or []:
        if not isinstance(item, dict):
            continue
        code = _norm_code(item.get("code"))
        if not code:
            continue
        account = str(item.get("account") or "").strip() or DEFAULT_ACCOUNT
        key = (account, code)
        if key in seen:
            continue
        seen.add(key)
        row = {"code": code, "account": account}
        for k in ("name", "cost", "shares"):
            if k in item:
                row[k] = item[k]
        rows.append(row)
    names = [DEFAULT_ACCOUNT]
    for a in (accounts or []):
        a = str(a or "").strip()
        if a and a not in names:
            names.append(a)
    for r in rows:
        if r["account"] not in names:
            names.append(r["account"])
    return rows, names


def save_portfolio(holdings, accounts=None):
    """加密写入持仓与账户名录，原子替换。"""
    rows, names = _normalize(holdings, accounts)
    payload = {"version": 1, "holdings": rows, "accounts": names}
    token = _fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    with _LOCK:
        os.makedirs(SECURE_DIR, exist_ok=True)
        tmp = CIPHER_PATH + ".tmp"
        try:
            # 写入前保留上一版密文，误覆盖时可用 restore_backup() 恢复。
            if os.path.isfile(CIPHER_PATH):
                with open(CIPHER_PATH, "rb") as src, open(CIPHER_PATH + ".bak", "wb") as dst:
                    dst.write(src.read())
            with open(tmp, "wb") as f:
                f.write(token)
            os.replace(tmp, CIPHER_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return {"count": len(rows), "holdings": rows, "accounts": names}


def load_portfolio():
    """读取并解密。首次运行迁移旧的明文镜像。异常时不返回空数据。"""
    with _LOCK:
        if os.path.isfile(CIPHER_PATH):
            try:
                with open(CIPHER_PATH, "rb") as f:
                    raw = _fernet(create=False).decrypt(f.read().strip())
                payload = json.loads(raw.decode("utf-8"))
            except InvalidToken:
                raise SecureStoreError("加密持仓文件无法解密，密钥可能已变更。请恢复密钥，切勿覆盖该文件。")
            except SecureStoreError:
                raise
            except Exception as e:
                raise SecureStoreError(f"读取加密持仓失败：{e}")
            rows, names = _normalize(payload.get("holdings") or [], payload.get("accounts") or [])
            return {"holdings": rows, "accounts": names}

        if os.path.isfile(LEGACY_PATH):
            try:
                with open(LEGACY_PATH, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
            except Exception as e:
                raise SecureStoreError(f"旧持仓文件读取失败：{e}")
            data = save_portfolio(legacy if isinstance(legacy, list) else legacy.get("holdings") or [],
                                  legacy.get("accounts") if isinstance(legacy, dict) else None)
            try:
                os.remove(LEGACY_PATH)
            except OSError:
                pass
            return {"holdings": _normalize(legacy, data["accounts"])[0], "accounts": data["accounts"]}

    return {"holdings": [], "accounts": [DEFAULT_ACCOUNT]}


def restore_backup():
    """用上一版密文恢复当前文件（备份本身也是加密的）。"""
    bak = CIPHER_PATH + ".bak"
    if not os.path.isfile(bak):
        raise SecureStoreError("没有可用的加密备份")
    with open(bak, "rb") as f:
        raw = f.read()
    _fernet(create=False).decrypt(raw.strip())  # 先验证可解密，再覆盖
    with _LOCK:
        tmp = CIPHER_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, CIPHER_PATH)
    return load_portfolio()


def status():
    """供排查：不返回明文，只返回文件状态。"""
    path = key_path()
    protected = False
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                protected = f.read(len(_DPAPI_MAGIC)) == _DPAPI_MAGIC
        except OSError:
            pass
    return {
        "cipher_path": CIPHER_PATH,
        "key_path": path,
        "encrypted": os.path.isfile(CIPHER_PATH),
        "legacy_plaintext_exists": os.path.isfile(LEGACY_PATH),
        "key_protected_by_dpapi": protected,
        "dpapi_available": _dpapi_available(),
    }
