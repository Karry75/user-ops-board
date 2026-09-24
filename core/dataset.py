"""数据集层：换电窗口聚合、最后换电记录、电池异常事件、网点坐标。

所有重查询集中在 TTL 内存缓存中，供各业务模块复用。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from core import db
from core.common import MS_DAY, now_ms

_cache: Dict[str, Tuple[float, Any]] = {}


def _cached(key: str, ttl: float, builder):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = builder()
    _cache[key] = (time.time(), val)
    return val


def _win_rows(uids=None) -> list:
    now = now_ms()
    d = {n: now - n * MS_DAY for n in (15, 30, 45, 60, 90, 180)}
    flt = ""
    args: list = []
    if uids:
        clean = [int(x) for x in uids]
        flt = " AND take_user_id IN (" + ",".join(["%s"] * len(clean)) + ")"
        args = clean
    sql = f"""
    SELECT take_user_id AS uid,
           SUM(CASE WHEN create_time >= {d[15]} THEN 1 ELSE 0 END) AS n15,
           SUM(CASE WHEN create_time >= {d[30]} THEN 1 ELSE 0 END) AS n30,
           SUM(CASE WHEN create_time >= {d[45]} THEN 1 ELSE 0 END) AS n45,
           SUM(CASE WHEN create_time >= {d[60]} THEN 1 ELSE 0 END) AS n60,
           SUM(CASE WHEN create_time >= {d[90]} THEN 1 ELSE 0 END) AS n90,
           COUNT(*) AS n180,
           SUM(CASE WHEN create_time >= {d[60]} AND back_battery_sn <> '' THEN 1 ELSE 0 END) AS back60,
           SUM(CASE WHEN create_time >= {d[90]} AND back_battery_sn <> '' THEN 1 ELSE 0 END) AS back90,
           SUM(CASE WHEN create_time >= {d[30]} THEN expend_power_fee ELSE 0 END) AS fee30,
           SUM(CASE WHEN create_time >= {d[90]} THEN expend_power_fee ELSE 0 END) AS fee90,
           SUM(expend_power_fee) AS fee180,
           MAX(create_time) AS last_order_time
    FROM {db.biz('t_exchange_order')}
    WHERE is_del=0 AND create_time >= {d[180]}{flt}
    GROUP BY take_user_id
    """
    return db.query(sql, args, ttl=0)


def _win_map(rows) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        out[int(r[0])] = {
            "n15": int(r[1] or 0), "n30": int(r[2] or 0), "n45": int(r[3] or 0),
            "n60": int(r[4] or 0), "n90": int(r[5] or 0), "n180": int(r[6] or 0),
            "back60": int(r[7] or 0), "back90": int(r[8] or 0),
            "fee30": int(r[9] or 0), "fee90": int(r[10] or 0), "fee180": int(r[11] or 0),
            "last_order_time": int(r[12] or 0),
        }
    return out


def order_windows(ttl: float = 300) -> Dict[int, Dict[str, Any]]:
    """近 180 天按用户聚合的多窗口换电次数 / 归还次数 / 消费金额（全量，TTL 缓存）。

    次数口径：t_exchange_order.take_user_id，is_del=0。
    归还次数口径：同表 back_battery_sn 非空。
    金额口径：expend_power_fee（分）。
    """
    return _cached("order_windows", ttl, lambda: _win_map(_win_rows()))


def order_windows_for(uids, ttl: float = 180) -> Dict[int, Dict[str, Any]]:
    """仅按指定用户集合取窗口统计（分页浏览用，避免全量聚合）。"""
    clean = sorted({int(x) for x in (uids or []) if x})
    if not clean:
        return {}
    key = "win_scoped:" + ",".join(str(x) for x in clean)
    return _cached(key, ttl, lambda: _win_map(_win_rows(clean)))


def _last_rows(uids=None) -> list:
    now = now_ms()
    flt = ""
    args: list = []
    if uids:
        clean = [int(x) for x in uids]
        flt = " AND take_user_id IN (" + ",".join(["%s"] * len(clean)) + ")"
        args = clean
    sql = f"""
    SELECT uid, take_battery_sn, site_name, take_exchange_sn, take_battery_time, site_city
    FROM (
        SELECT take_user_id AS uid, take_battery_sn, site_name, take_exchange_sn, site_city,
               COALESCE(NULLIF(take_battery_time,0), create_time) AS take_battery_time,
               ROW_NUMBER() OVER (PARTITION BY take_user_id ORDER BY COALESCE(NULLIF(take_battery_time,0), create_time) DESC) AS rn
        FROM {db.biz('t_exchange_order')}
        WHERE is_del=0 AND create_time >= {now - 180 * MS_DAY} AND take_battery_sn <> ''{flt}
    ) t WHERE rn=1
    """
    return db.query(sql, args, ttl=0)


def _last_map(rows) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for uid, sn, site, exsn, tt, city in rows:
        out[int(uid)] = {
            "battery_sn": sn or "", "site_name": site or "", "exchange_sn": exsn or "",
            "take_time": int(tt or 0), "site_city": city or "",
        }
    return out


def last_exchange(ttl: float = 300) -> Dict[int, Dict[str, Any]]:
    """近 180 天每位用户最后一次取电记录（电池 SN / 网点 / 柜机 / 时间，全量 TTL 缓存）。"""
    return _cached("last_exchange", ttl, lambda: _last_map(_last_rows()))


def last_exchange_for(uids, ttl: float = 180) -> Dict[int, Dict[str, Any]]:
    """仅按指定用户集合取最后一次取电记录（分页浏览用）。"""
    clean = sorted({int(x) for x in (uids or []) if x})
    if not clean:
        return {}
    key = "last_scoped:" + ",".join(str(x) for x in clean)
    return _cached(key, ttl, lambda: _last_map(_last_rows(clean)))


def battery_events(days: int = 30, ttl: float = 300) -> Dict[str, Dict[str, Any]]:
    """近 N 天电池异常事件（按电池 SN 汇总事件天数，用于流通异常与超范围预警）。"""
    def build():
        now = now_ms()
        sql = f"""
        SELECT b.battery_sn, e.name,
               COUNT(*) AS ev_cnt,
               COUNT(DISTINCT DATE(FROM_UNIXTIME(b.create_time/1000))) AS ev_days,
               MAX(b.create_time) AS last_time,
               MAX(b.city) AS city,
               MAX(b.address) AS address
        FROM {db.biz('t_monitor_ex_event_battery')} b
        JOIN {db.biz('t_monitor_ex_event')} e ON e.id = b.ex_event_id
        WHERE b.is_del=0 AND b.create_time >= {now - days * MS_DAY} AND b.battery_sn <> ''
        GROUP BY b.battery_sn, e.name
        """
        rows = db.query(sql, ttl=0)
        out: Dict[str, Dict[str, Any]] = {}
        for sn, name, cnt, days_, last, city, addr in rows:
            rec = out.setdefault(sn, {"events": {}, "out_of_range_days": 0, "offline_days": 0,
                                      "lost_flag": False, "unrecognized_flag": False,
                                      "last_time": 0, "city": "", "address": ""})
            rec["events"][name] = int(cnt or 0)
            if "超出运营区域" in (name or ""):
                rec["out_of_range_days"] = max(rec["out_of_range_days"], int(days_ or 0))
            if "长时间离线" in (name or ""):
                rec["offline_days"] = max(rec["offline_days"], int(days_ or 0))
            if "丢失" in (name or ""):
                rec["lost_flag"] = True
            if "未识别" in (name or ""):
                rec["unrecognized_flag"] = True
            if int(last or 0) > rec["last_time"]:
                rec["last_time"] = int(last or 0)
                rec["city"] = city or rec["city"]
                rec["address"] = addr or rec["address"]
        return out
    return _cached(f"battery_events_{days}", ttl, build)


def city_short_names(ttl: float = 3600) -> Dict[str, str]:
    """sys_city: 城市全名 -> 短名（如 广东省深圳市 -> 深圳市）。"""
    def build():
        rows = db.query(
            f"SELECT name, city FROM {db.base('sys_city')} WHERE is_del=0", ttl=0
        )
        return {r[0]: (r[1] or r[0]) for r in rows if r[0]}
    return _cached("city_short", ttl, build)


def site_points(ttl: float = 3600) -> Dict[str, List[Tuple[float, float]]]:
    """按城市缓存网点坐标（网点名 -> 坐标），用于距离判定。"""
    def build():
        rows = db.query(
            f"""SELECT city, longitude, latitude FROM {db.biz('t_site')}
                WHERE site_status='on' AND longitude > 0 AND latitude > 0""",
            ttl=0,
        )
        out: Dict[str, List[Tuple[float, float]]] = {}
        for city, lng, lat in rows:
            try:
                out.setdefault(city or "", []).append((float(lng), float(lat)))
            except Exception:
                continue
        return out
    return _cached("site_points", ttl, build)


def haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_near_cache: Dict[Any, Optional[float]] = {}


def prewarm() -> None:
    """并发预热四张基础数据集（全量），避免首屏串行等待。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(order_windows), ex.submit(last_exchange),
                ex.submit(battery_events, 30), ex.submit(city_short_names)]
        for fu in futs:
            try:
                fu.result()
            except Exception:
                pass


def nearest_site_km(city_full: str, lng: Optional[float], lat: Optional[float]) -> Optional[float]:
    if not lng or not lat:
        return None
    try:
        ck = (city_full or "", round(float(lng), 2), round(float(lat), 2))
    except Exception:
        return None
    if ck in _near_cache:
        return _near_cache[ck]
    pts = site_points()
    short = city_short_names().get(city_full, city_full)
    cands = pts.get(short) or pts.get(city_full)
    if not cands:
        for key, val in pts.items():
            if key and key in (city_full or ""):
                cands = val
                break
    if not cands:
        return None
    val = round(min(haversine_km(float(lng), float(lat), x, y) for x, y in cands), 2)
    if len(_near_cache) > 300000:
        _near_cache.clear()
    _near_cache[ck] = val
    return val
