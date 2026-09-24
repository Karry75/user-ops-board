"""user_ops_board 配置加载器。

安全约定（与 board / electric 保持一致）：
1. 数据库口令仅从 .env 读取并驻留内存，禁止写入日志、禁止在接口/前端输出。
2. 本工程**不复制**任何凭据明文，默认复用同级 board / electric 目录下的 .env（只读引用）；
   若本工程目录下存在自己的 .env，则优先使用（可用于覆盖）。
3. 数据库访问层强制只读（select / with 白名单），严禁写库。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BASE_DIR)  # D:\Marvis K\janus
CONFIG_DIR = os.path.join(BASE_DIR, "config")

# .env 查找顺序：本工程 .env -> board/.env -> electric/.env（均只读引用，不复制明文）
ENV_CANDIDATES = [
    os.path.join(BASE_DIR, ".env"),
    os.path.join(PROJECT_ROOT, "board", ".env"),
    os.path.join(PROJECT_ROOT, "electric", ".env"),
]


def _load_dotenv(path: str) -> None:
    """轻量 .env 解析：KEY=VALUE，注入 os.environ（不覆盖已存在的值）。"""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # 凭据读取失败不抛出、不打印内容
        pass


for _p in ENV_CANDIDATES:
    _load_dotenv(_p)

DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", "3306") or 3306)
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "sharing-citybike-pro")          # 业务库
DB_BASE = os.environ.get("DB_BASE", "sharing-system-base-pro")      # 基础库

BOARD_HOST = os.environ.get("USER_OPS_BOARD_HOST", "0.0.0.0")
BOARD_PORT = int(os.environ.get("USER_OPS_BOARD_PORT", "8095"))

DEFAULT_LIMIT = 500
QUERY_TIMEOUT = 120


def db_configured() -> bool:
    return bool(DB_HOST and DB_USER and DB_PASSWORD and DB_NAME)


_th_cache: Dict[str, Any] = {"ts": 0.0, "mtime": -1.0, "data": {}}


def load_thresholds() -> Dict[str, Any]:
    """加载口径阈值配置（可在界面展示、可修改）。

    进程内按文件 mtime + 30 秒 TTL 缓存：阈值读取处于高频循环中，避免重复解析 YAML。
    """
    path = os.path.join(CONFIG_DIR, "thresholds.yaml")
    if yaml is None or not os.path.isfile(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = -1.0
    cache = _th_cache
    if cache["data"] and (time.time() - cache["ts"]) < 30 and cache["mtime"] == mtime:
        return cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return cache["data"] or {}
    cache.update(ts=time.time(), mtime=mtime, data=data)
    return data
