"""只读数据访问层（AnalyticDB MySQL / pymysql）。

- 双库：业务库 DB_NAME、基础库 DB_BASE，统一用 `库名.表名` 显式限定。
- 强制只读：语句必须以 select / with 开头，命中写关键字直接拒绝。
- 带 TTL 内存缓存，避免看板重复拉取大表。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymysql

from config import settings

_ALLOW_RE = re.compile(r"^\s*(select|with)\b", re.I)
_DENY_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|rename|call|set)\b", re.I
)

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Any]] = {}
DEFAULT_TTL = 300.0


class DBError(RuntimeError):
    pass


def _connect():
    if not settings.db_configured():
        raise DBError("数据库连接参数缺失（未找到 .env 中的 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME）")
    return pymysql.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=settings.QUERY_TIMEOUT,
        cursorclass=pymysql.cursors.Cursor,
    )


def query(sql: str, args: Optional[Sequence[Any]] = None, ttl: float = DEFAULT_TTL) -> List[Tuple]:
    """执行只读查询，返回行元组列表。"""
    if not _ALLOW_RE.match(sql):
        raise DBError("只允许 select / with 查询")
    stripped = _DENY_RE.sub(" ", sql, count=0)
    if _DENY_RE.search(sql) and not _ALLOW_RE.match(sql):
        raise DBError("包含疑似写操作关键字，已拒绝")
    key = sql + "|" + repr(tuple(args or ()))
    now = time.time()
    if ttl > 0:
        with _lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if ttl > 0:
        with _lock:
            _cache[key] = (now, rows)
            if len(_cache) > 256:
                for k in sorted(_cache, key=lambda x: _cache[x][0])[:64]:
                    _cache.pop(k, None)
    return rows


def query_dicts(sql: str, args: Optional[Sequence[Any]] = None, ttl: float = DEFAULT_TTL) -> List[Dict[str, Any]]:
    if not _ALLOW_RE.match(sql):
        raise DBError("只允许 select / with 查询")
    key = "D|" + sql + "|" + repr(tuple(args or ()))
    now = time.time()
    if ttl > 0:
        with _lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, args or ())
            rows = list(cur.fetchall())
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if ttl > 0:
        with _lock:
            _cache[key] = (now, rows)
            if len(_cache) > 256:
                for k in sorted(_cache, key=lambda x: _cache[x][0])[:64]:
                    _cache.pop(k, None)
    return rows


def one(sql: str, args: Optional[Sequence[Any]] = None, default: Any = 0, ttl: float = DEFAULT_TTL) -> Any:
    rows = query(sql, args, ttl=ttl)
    if not rows:
        return default
    val = rows[0][0]
    return default if val is None else val


def biz(table: str) -> str:
    """业务库全限定表名。"""
    return f"`{settings.DB_NAME}`.`{table}`"


def base(table: str) -> str:
    """基础库全限定表名。"""
    return f"`{settings.DB_BASE}`.`{table}`"


def clear_cache() -> None:
    with _lock:
        _cache.clear()
