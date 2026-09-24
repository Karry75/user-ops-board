"""通用工具：时间换算、筛选条件、字典与批量补全。"""
from __future__ import annotations

import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core import db

MS_DAY = 86400000


def _batches(items: Sequence[Any], step: int) -> List[List[Any]]:
    items = list(items)
    return [items[i:i + step] for i in range(0, len(items), step)]


def _pmap(fn, items: Sequence[Any], workers: int = 8) -> List[Any]:
    """批量查询并行化：每次查询使用独立连接（只读、无副作用），显著降低多批往返耗时。"""
    items = list(items)
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_str(ms: Any) -> str:
    if not ms:
        return ""
    try:
        v = int(ms)
    except Exception:
        return ""
    if v <= 0:
        return ""
    if v > 1e12:
        v = v / 1000
    try:
        return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def ms_to_date(ms: Any) -> str:
    s = ms_to_str(ms)
    return s.split(" ")[0] if s else ""


def days_left(ms: Any, ref_ms: Optional[int] = None) -> Optional[int]:
    """距今天剩余天数（向上取整，可为负）。"""
    if not ms:
        return None
    ref = ref_ms or now_ms()
    try:
        return int(-((ref - int(ms)) // -MS_DAY))
    except Exception:
        return None


class Filters:
    """统一筛选器：城市 × 电池产品 × OEM × 时间窗 × 押金划扣预警时间。"""

    def __init__(self, city: str = "", product: str = "", oem: str = "",
                 start_ms: int = 0, end_ms: int = 0, warn_stage: str = ""):
        self.city = (city or "").strip()
        self.product = (product or "").strip()
        self.oem = (oem or "").strip()
        self.start_ms = int(start_ms or 0)
        self.end_ms = int(end_ms or 0)
        self.warn_stage = (warn_stage or "").strip()

    @property
    def active(self) -> bool:
        return bool(self.city or self.product or self.oem or self.warn_stage)

    def deposit_warn_cond(self, alias: str = "a") -> Tuple[str, list]:
        """押金划扣预警时间筛选（T-30 / T-7 / T-3 / T-1 / 已逾期）。

        押金到期 = 押金绑定时间(deposit_bind_time) + 1 年，无绑定时间时回退租期到期时间。
        仅统计押金在账且押金金额 > 0 的协议。
        """
        if not self.warn_stage:
            return "", []
        from core import cn
        expire = (f"(COALESCE(NULLIF({alias}.deposit_bind_time, 0), {alias}.rent_expire_time)"
                  f" + {cn.YEAR_MS})")
        now = now_ms()
        base = [f"{alias}.deposit_status='on'", f"{alias}.deposit_fee > 0",
                f"({alias}.deposit_bind_time > 0 OR {alias}.rent_expire_time > 0)"]
        if self.warn_stage == "overdue":
            return " AND ".join(base + [f"{expire} <= %s"]), [now]
        days = cn.WARN_STAGE_DAYS.get(self.warn_stage)
        if not days:
            return "", []
        return (" AND ".join(base + [f"{expire} <= %s", f"{expire} > %s"]),
                [now + days * MS_DAY, now])

    def agreement_where(self, alias: str = "a", time_field: str = "create_time") -> Tuple[str, list]:
        """返回协议表过滤 SQL 片段与参数（不含 WHERE 关键字）。"""
        parts = [f"{alias}.is_del=0"]
        args: List[Any] = []
        if self.city:
            parts.append(f"{alias}.sys_city_name = %s")
            args.append(self.city)
        if self.product:
            parts.append(f"{alias}.battery_product_id = %s")
            args.append(self.product)
        if self.oem:
            parts.append(f"{alias}.oem_id = %s")
            args.append(self.oem)
        if self.start_ms:
            parts.append(f"{alias}.{time_field} >= %s")
            args.append(self.start_ms)
        if self.end_ms:
            parts.append(f"{alias}.{time_field} <= %s")
            args.append(self.end_ms)
        return " AND ".join(parts), args

    def describe(self) -> Dict[str, Any]:
        from core import cn
        stage_label = dict((o["value"], o["label"]) for o in cn.WARN_STAGE_OPTIONS).get(
            self.warn_stage, self.warn_stage or "全部预警时间")
        return {
            "city": self.city or "全部城市",
            "product": self.product or "全部电池产品",
            "oem": self.oem or "全部 OEM",
            "warn_stage": stage_label,
            "start": ms_to_date(self.start_ms) or "不限",
            "end": ms_to_date(self.end_ms) or "不限",
        }


_memo_store: Dict[str, Tuple[float, Any]] = {}


def memo(key: str, ttl: float, builder):
    """进程内 TTL 缓存：同一筛选条件下的重查询只执行一次。"""
    hit = _memo_store.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = builder()
    _memo_store[key] = (time.time(), val)
    if len(_memo_store) > 64:
        for k in sorted(_memo_store, key=lambda x: _memo_store[x][0])[:16]:
            _memo_store.pop(k, None)
    return val


def filter_key(f: "Filters") -> str:
    return f"{f.city}|{f.product}|{f.oem}|{f.start_ms}|{f.end_ms}|{f.warn_stage}"


def user_phone_map(user_ids: Sequence[int]) -> Dict[int, str]:
    """用户 id → 当前手机号（t_user.phone），用于「用户当前手机号」列。"""
    clean = [int(x) for x in {int(i) for i in user_ids if i}]
    if not clean:
        return {}
    out: Dict[int, str] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query(
            f"SELECT id, phone FROM {db.biz('t_user')} WHERE id IN ({ph})",
            batch, ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for uid, phone in rows:
            out[int(uid)] = phone or ""
    return out


def rent_info_map(rent_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """租期卡 id → 套餐信息（套餐名称 / 卡状态 / 卡到期时间）。"""
    clean = [int(x) for x in {int(i) for i in rent_ids if i}]
    if not clean:
        return {}
    out: Dict[int, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query_dicts(
            f"""SELECT ur.id, ur.card_status, ur.expire_time, rp.name AS package_name,
                       rp.valid_type, rp.category
                FROM {db.biz('t_user_exchange_rent')} ur
                LEFT JOIN {db.biz('t_exchange_rent_package')} rp ON rp.id = ur.package_id
                WHERE ur.id IN ({ph})""",
            batch, ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for r in rows:
            out[int(r["id"])] = r
    return out


def product_name_map() -> Dict[int, str]:
    """电池产品 id → 名称（复用 product_options 缓存，避免大表 JOIN）。"""
    return {int(p["id"]): p["name"] for p in product_options()}


def deposit_orders_map(agreement_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """协议 → 押金订单列表（t_user_exchange_deposit_package_order），用于划扣结果判定与失败归因。"""
    clean = [int(x) for x in {int(i) for i in agreement_ids if i}]
    if not clean:
        return {}
    out: Dict[int, List[Dict[str, Any]]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query_dicts(
            f"""SELECT id, exchange_agreement_id, deposit_fee, real_fee, is_pay, pay_time,
                       order_status, pay_way, trade_no, create_time
                FROM {db.biz('t_user_exchange_deposit_package_order')}
                WHERE is_del=0 AND exchange_agreement_id IN ({ph})""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for r in rows:
            out.setdefault(int(r["exchange_agreement_id"]), []).append(r)
    return out


def pay_status_map(order_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """押金订单 id → 支付流水（latest，pay_status 含 PAYERROR，用于失败归因佐证）。"""
    clean = [int(x) for x in {int(i) for i in order_ids if i}]
    if not clean:
        return {}
    out: Dict[int, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query_dicts(
            f"""SELECT business_id, pay_status, pay_way, fee, pay_fee, create_time
                FROM {db.biz('t_pay_wechat_log')}
                WHERE business_type='exchangeDepositOrder' AND business_id IN ({ph})""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for r in rows:
            bid = int(r["business_id"])
            prev = out.get(bid)
            if prev is None or int(r.get("create_time") or 0) > int(prev.get("create_time") or 0):
                out[bid] = r
    return out


_city_cache: Dict[str, Any] = {"ts": 0, "rows": []}
_product_cache: Dict[str, Any] = {"ts": 0, "rows": []}


def city_options() -> List[Dict[str, Any]]:
    if time.time() - _city_cache["ts"] < 600 and _city_cache["rows"]:
        return _city_cache["rows"]
    rows = db.query(
        f"""SELECT a.sys_city_name, COUNT(*) c
            FROM {db.biz('t_exchange_agreement')} a
            WHERE a.is_del=0 AND a.sys_city_name <> ''
            GROUP BY a.sys_city_name ORDER BY c DESC""",
        ttl=600,
    )
    data = [{"name": r[0], "count": int(r[1] or 0)} for r in rows]
    _city_cache.update(ts=time.time(), rows=data)
    return data


def product_options() -> List[Dict[str, Any]]:
    if time.time() - _product_cache["ts"] < 600 and _product_cache["rows"]:
        return _product_cache["rows"]
    rows = db.query(
        f"""SELECT a.battery_product_id, MAX(p.name) nm, COUNT(*) c
            FROM {db.biz('t_exchange_agreement')} a
            LEFT JOIN {db.biz('t_battery_product')} p ON p.id = a.battery_product_id
            WHERE a.is_del=0 AND a.battery_product_id > 0
            GROUP BY a.battery_product_id ORDER BY c DESC""",
        ttl=600,
    )
    data = [
        {"id": int(r[0]), "name": r[1] or f"产品{r[0]}", "count": int(r[2] or 0)}
        for r in rows
    ]
    _product_cache.update(ts=time.time(), rows=data)
    return data


def oem_options() -> List[Dict[str, Any]]:
    rows = db.query(
        f"""SELECT a.oem_id, COUNT(*) c FROM {db.biz('t_exchange_agreement')} a
            WHERE a.is_del=0 AND a.oem_id > 0 GROUP BY a.oem_id ORDER BY c DESC LIMIT 30""",
        ttl=600,
    )
    return [{"id": int(r[0]), "count": int(r[1] or 0)} for r in rows]


def battery_map(sns: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """按电池 SN 批量取电池档案（最后定位/在线状态/是否归还柜机/电量）。"""
    clean = [s for s in {str(x) for x in sns if x}]
    if not clean:
        return {}
    out: Dict[str, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        rows = db.query_dicts(
            f"""SELECT b.device_sn, b.battery_status, b.online_status, b.lat, b.lng,
                       b.last_location_address, b.last_location_time, b.last_back_exchange_sn,
                       b.last_back_time, b.last_take_exchange_sn, b.last_take_time,
                       b.last_upload_exchange_sn, b.last_online_time, b.oem_device_status
                FROM {db.biz('t_battery')} b
                WHERE b.is_del=0 AND b.device_sn IN ({ph})""",
            batch,
            ttl=180,
        )
        rows2 = db.query_dicts(
            f"""SELECT s.device_sn, s.power, s.using, s.charging, s.voltage_out, s.cycle,
                       s.last_power_update_time
                FROM {db.biz('t_battery_status')} s
                WHERE s.device_sn IN ({ph})""",
            batch,
            ttl=180,
        )
        return rows, rows2

    for rows, rows2 in _pmap(_fetch, _batches(clean, 1000)):
        for r in rows:
            out[r["device_sn"]] = r
        for r in rows2:
            out.setdefault(r["device_sn"], {})["power"] = r.get("power")
            out[r["device_sn"]].update(
                power_using=r.get("using"), charging=r.get("charging"),
                voltage_out=r.get("voltage_out"), cycle=r.get("cycle"),
                power_update_time=r.get("last_power_update_time"),
            )
    return out


def user_tags(user_ids: Sequence[int]) -> Dict[int, List[str]]:
    clean = [int(x) for x in {int(i) for i in user_ids if i}]
    if not clean:
        return {}
    out: Dict[int, List[str]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query(
            f"""SELECT r.result_id, t.tag_name
                FROM {db.biz('t_tag_result')} r
                JOIN {db.biz('t_tag')} t ON t.id = r.tag_id
                WHERE r.result_type=1 AND r.result_id IN ({ph})""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for rid, name in rows:
            out.setdefault(int(rid), []).append(name)
    return out


def feedback_scores(user_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """用户评价得分：t_feedback.business_type='exchange' 按 user_id 聚合（1不满意/2一般/3非常好）。"""
    clean = [int(x) for x in {int(i) for i in user_ids if i}]
    if not clean:
        return {}
    out: Dict[int, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query(
            f"""SELECT f.user_id, AVG(f.score), COUNT(*), MAX(f.create_time)
                FROM {db.biz('t_feedback')} f
                WHERE f.business_type='exchange' AND f.user_id IN ({ph})
                GROUP BY f.user_id""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for uid, avg_score, cnt, last in rows:
            out[int(uid)] = {"avg_score": round(float(avg_score or 0), 2), "cnt": int(cnt or 0),
                             "last_time": ms_to_str(last)}
    return out


def reception_notes(agreement_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    clean = [int(x) for x in {int(i) for i in agreement_ids if i}]
    if not clean:
        return {}
    out: Dict[int, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query(
            f"""SELECT r.agreement_id, r.detail, r.create_time, r.type
                FROM {db.biz('t_reception_log')} r
                JOIN (
                    SELECT agreement_id, MAX(create_time) mt
                    FROM {db.biz('t_reception_log')}
                    WHERE is_del=0 AND agreement_id IN ({ph})
                    GROUP BY agreement_id
                ) m ON m.agreement_id = r.agreement_id AND m.mt = r.create_time
                WHERE r.is_del=0""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for aid, detail, ctime, rtype in rows:
            out[int(aid)] = {"detail": detail, "time": ms_to_str(ctime), "type": rtype}
    return out


def cyclepay_map(agreement_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    clean = [int(x) for x in {int(i) for i in agreement_ids if i}]
    if not clean:
        return {}
    out: Dict[int, Dict[str, Any]] = {}

    def _fetch(batch):
        ph = ",".join(["%s"] * len(batch))
        return db.query_dicts(
            f"""SELECT exchange_agreement_id, status, sign_status, single_amount,
                       last_deduct_time, next_deduct_time, unsign_reason, sign_time
                FROM {db.biz('t_user_cyclepay_sign_log')}
                WHERE exchange_agreement_id IN ({ph})""",
            batch,
            ttl=300,
        )

    for rows in _pmap(_fetch, _batches(clean, 1000)):
        for r in rows:
            out[int(r["exchange_agreement_id"])] = r
    return out


def paginate(rows: List[Any], page: int, size: int, cap: int = 500) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 50), int(cap or 500)))
    total = len(rows)
    start = (page - 1) * size
    return {"total": total, "page": page, "size": size, "rows": rows[start:start + size]}
