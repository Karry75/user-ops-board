"""沉默低频 · 电池寻找板块：分层、流通异常剔除、动作队列、线索明细。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import settings
from core import cn, common, dataset, db, deposit
from core.common import MS_DAY, Filters, ms_to_str

ACTIVE_STATUS = ("working", "owe_rent", "paused")


def _cfg() -> Dict[str, Any]:
    return settings.load_thresholds().get("silent_low_freq", {}) or {}


def base_rows(f: Filters) -> List[Dict[str, Any]]:
    """生效中协议 + 换电窗口统计 + 最后换电记录 + 电池档案（带 TTL 缓存，同筛选条件只构建一次）。"""
    return common.memo("silent_base:" + common.filter_key(f), 300, lambda: _build_base(f))


def _build_base(f: Filters) -> List[Dict[str, Any]]:
    where, args = f.agreement_where()
    wcond, wargs = f.deposit_warn_cond()
    if wcond:
        where = where + " AND " + wcond
        args = list(args) + list(wargs)
    sql = f"""
    SELECT a.id, a.user_id, a.user_name, a.user_phone, a.sys_city_name, a.battery_product_id,
           a.type, a.status, a.activation_time, a.rent_expire_time, a.deposit_status,
           a.deposit_fee, a.deposit_payway, a.deposit_bind_time, a.deposit_unbind_time,
           a.battery_take_status, a.user_rent_id,
           pr.name AS product_name,
           ur.card_status AS rent_card_status, ur.expire_time AS rent_card_expire,
           rp.name AS package_name,
           CASE WHEN a.rent_expire_time > 0 AND a.rent_expire_time < UNIX_TIMESTAMP()*1000 THEN 1 ELSE 0 END AS rent_overdue
    FROM {db.biz('t_exchange_agreement')} a
    LEFT JOIN {db.biz('t_battery_product')} pr ON pr.id = a.battery_product_id
    LEFT JOIN {db.biz('t_user_exchange_rent')} ur ON ur.id = a.user_rent_id
    LEFT JOIN {db.biz('t_exchange_rent_package')} rp ON rp.id = ur.package_id
    WHERE {where} AND a.status IN {ACTIVE_STATUS}
    """
    rows = db.query_dicts(sql, list(args), ttl=180)
    dataset.prewarm()
    windows = dataset.order_windows()
    lasts = dataset.last_exchange()
    events = dataset.battery_events(30)
    short = dataset.city_short_names()
    now = common.now_ms()
    out: List[Dict[str, Any]] = []
    for r in rows:
        uid = int(r.get("user_id") or 0)
        st = windows.get(uid, {})
        last = lasts.get(uid, {})
        sn = last.get("battery_sn") or ""
        ev = events.get(sn, {}) if sn else {}
        act = int(r.get("activation_time") or 0)
        active_days = int((now - act) / MS_DAY) if act else 0
        back60 = int(st.get("back60") or 0)
        monthly = round(back60 / 2.0, 2)
        rec = dict(r)
        bind = int(r.get("deposit_bind_time") or 0)
        dep_expire = cn.deposit_expire_ms(bind, r.get("rent_expire_time"))
        dep_left = common.days_left(dep_expire)
        rec.update({
            "active_days": active_days,
            "n15": int(st.get("n15") or 0), "n30": int(st.get("n30") or 0),
            "n45": int(st.get("n45") or 0), "n60": int(st.get("n60") or 0),
            "n90": int(st.get("n90") or 0), "n180": int(st.get("n180") or 0),
            "back60": back60, "back90": int(st.get("back90") or 0),
            "fee30": int(st.get("fee30") or 0), "fee90": int(st.get("fee90") or 0),
            "monthly_freq": monthly,
            "battery_sn": sn,
            "last_site": last.get("site_name") or "",
            "last_exchange_sn": last.get("exchange_sn") or "",
            "last_take_time": last.get("take_time") or 0,
            "last_take_time_str": ms_to_str(last.get("take_time") or 0),
            "days_since_last_exchange": (
                int((now - int(last.get("take_time") or 0)) / MS_DAY)
                if int(last.get("take_time") or 0) else active_days
            ),
            "rent_expire_str": ms_to_str(r.get("rent_expire_time")),
            "activation_str": ms_to_str(act),
            "agreement_type_cn": cn.cn("agreement_type", r.get("type")),
            "agreement_status_cn": cn.cn("agreement_status", r.get("status")),
            "deposit_status_cn": cn.cn("deposit_status", r.get("deposit_status")),
            "deposit_payway_cn": cn.cn("deposit_payway", r.get("deposit_payway")),
            "deposit_fee_yuan": round(int(r.get("deposit_fee") or 0) / 100.0, 2),
            "deposit_bind_str": ms_to_str(bind),
            "deposit_expire_ms": dep_expire,
            "deposit_expire_str": ms_to_str(dep_expire),
            "deposit_days_left": dep_left,
            "deposit_stage": cn.stage_of(dep_left),
            "deposit_stage_cn": cn.stage_cn(cn.stage_of(dep_left)),
            "deposit_warn_text": (f"剩 {dep_left} 天押金到期" if dep_left is not None and dep_left >= 0
                                  else (f"押金已到期（逾期 {abs(dep_left)} 天）" if dep_left is not None
                                        else "无押金到期时间")),
            "package_name": r.get("package_name") or "",
            "rent_card_status_cn": cn.cn("card_status", r.get("rent_card_status")),
            "rent_card_expire_str": ms_to_str(r.get("rent_card_expire")),
            "package_text": (f"{r.get('package_name') or '套餐未命名'}（"
                             f"{cn.cn('card_status', r.get('rent_card_status'))}"
                             f"{'，到期 ' + ms_to_str(r.get('rent_card_expire')) if ms_to_str(r.get('rent_card_expire')) else ''}）"
                             if r.get("package_name") or r.get("rent_card_status")
                             else "无租期套餐记录"),
            "abnormal_flow": False,
            "abnormal_reason": "",
            "out_of_range": False,
            "out_of_range_days": int(ev.get("out_of_range_days") or 0),
            "offline_days": int(ev.get("offline_days") or 0),
            "battery_events": ev.get("events") or {},
        })
        rec["_short_city"] = short.get(r.get("sys_city_name") or "", r.get("sys_city_name") or "")
        out.append(rec)
    # 电池档案补全（最后定位 / 在线状态 / 电量 / 是否已归还柜机）
    sns = [r["battery_sn"] for r in out if r["battery_sn"]]
    bmap = common.battery_map(sns)
    for r in out:
        b = bmap.get(r["battery_sn"], {}) if r["battery_sn"] else {}
        r["battery_status"] = b.get("battery_status") or ""
        r["online_status"] = b.get("online_status") or ""
        r["battery_status_cn"] = cn.cn("battery_status", r["battery_status"])
        r["online_status_cn"] = cn.cn("online_status", r["online_status"])
        r["power"] = b.get("power")
        r["last_location_address"] = b.get("last_location_address") or ""
        r["lat"] = b.get("lat")
        r["lng"] = b.get("lng")
        r["last_back_exchange_sn"] = b.get("last_back_exchange_sn") or ""
        r["last_back_time_str"] = ms_to_str(b.get("last_back_time") or 0)
        r["last_take_exchange_sn"] = b.get("last_take_exchange_sn") or ""
        r["cycle"] = b.get("cycle")
    return out


def _flow_abnormal(r: Dict[str, Any]) -> str:
    """流通异常判定：电池已归还柜机 / 不在用户手 / 命中异常事件。"""
    cfg = _cfg().get("abnormal_flow", {}) or {}
    reasons = []
    if (r.get("last_back_exchange_sn") or ""):
        reasons.append("电池已归还柜机")
    if (r.get("battery_status") or "") == "none" and r.get("last_take_time"):
        reasons.append("电池不在使用状态")
    ev = r.get("battery_events") or {}
    for name in cfg.get("battery_events", []) or []:
        if name in ev:
            reasons.append(f"异常事件-{name}({ev[name]})")
    return "；".join(reasons)


def silent_users(f: Filters) -> List[Dict[str, Any]]:
    cfg = _cfg().get("silent", {}) or {}
    window = int(cfg.get("window_days", 90))
    need_overdue = bool(cfg.get("require_overdue_rent", True))
    out = []
    for r in base_rows(f):
        n90 = r["n90"]
        if window == 90:
            cnt = n90
        else:
            cnt = r.get(f"n{window}") or 0
        if cnt > 0:
            continue
        if need_overdue and not (r.get("rent_overdue") or (r.get("status") == "owe_rent")):
            continue
        reason = _flow_abnormal(r)
        r["abnormal_flow"] = bool(reason)
        r["abnormal_reason"] = reason
        if reason:
            continue
        r["group"] = "silent"
        out.append(r)
    return out


def low_freq_level(r: Dict[str, Any]) -> Optional[str]:
    """低频 L1-L5 分层，返回层级名或 None。"""
    cfg = _cfg().get("low_freq", {}) or {}
    new_excl = int(cfg.get("new_user_exclude_days", 15))
    days = int(r.get("active_days") or 0)
    power = r.get("power")
    l5 = cfg.get("L5", {}) or {}
    if days > new_excl:
        for key in ("L1", "L2", "L3"):
            spec = cfg.get(key, {}) or {}
            lo, hi, mx = int(spec.get("min_days", 0)), int(spec.get("max_days", 0)), int(spec.get("max_count", 0))
            if lo < days <= hi:
                cnt = r.get(f"n{hi}") or 0
                if cnt < mx:
                    return key
                return None
        spec = cfg.get("L4", {}) or {}
        if days > int(spec.get("min_days", 60)) and int(r.get("n60") or 0) < int(spec.get("last60_max_count", 4)):
            return "L4"
        return None
    # 新用户（生效 <= 15 天）：仅 L5
    if days <= int(l5.get("new_user_days", 15)) and power is not None and float(power or 0) < float(l5.get("power_lt", 25)):
        return "L5"
    if days > int(l5.get("new_user_days", 15)) and int(r.get("n15") or 0) == 0 \
            and power is not None and float(power or 0) < float(l5.get("power_lt", 25)):
        return "L5"
    return None


def low_freq_users(f: Filters) -> List[Dict[str, Any]]:
    out = []
    for r in base_rows(f):
        reason = _flow_abnormal(r)
        r["abnormal_flow"] = bool(reason)
        r["abnormal_reason"] = reason
        if reason:
            continue
        lvl = low_freq_level(r)
        if not lvl:
            continue
        r["group"] = "low_freq"
        r["level"] = lvl
        out.append(r)
    return out


def action_queue(f: Filters) -> Dict[str, Any]:
    cfg = _cfg().get("action_queue", {}) or {}
    persuade_days = int(cfg.get("persuade_days", 60))
    out_range_days = int(cfg.get("out_of_range_days", 3))
    rows = base_rows(f)
    queues: Dict[str, List[Dict[str, Any]]] = {"persuade": [], "activate": [], "out_of_range": []}
    for r in rows:
        reason = _flow_abnormal(r)
        r["abnormal_flow"] = bool(reason)
        r["abnormal_reason"] = reason
        if reason:
            continue
        # 超运营范围：最后定位城市 ≠ 运营城市，或距最近网点 > 5km，或命中"电池超出运营区域"事件
        short = r.get("_short_city") or ""
        loc = r.get("last_location_address") or ""
        addr_mismatch = bool(loc) and bool(short) and (short not in loc)
        ev_days = int(r.get("out_of_range_days") or 0)
        km = None
        if not addr_mismatch and ev_days <= 0:
            km = dataset.nearest_site_km(r.get("sys_city_name") or "", r.get("lng"), r.get("lat"))
        far = km is not None and km > float(cfg.get("out_of_range_km", 5))
        if addr_mismatch or far or ev_days > 0:
            reasons = []
            if addr_mismatch:
                reasons.append(f"最后定位（{loc}）与运营城市（{short}）不一致")
            if far:
                reasons.append(f"距最近网点 {km} km")
            if ev_days > 0:
                reasons.append(f"命中电池超出运营区域事件 {ev_days} 天")
            r["out_of_range"] = True
            r["nearest_site_km"] = km
            r["out_of_range_reason"] = "；".join(reasons)
            r["escalate"] = ev_days > out_range_days
            r["escalate_text"] = f"连续超范围 {ev_days} 天，加大预警" if r["escalate"] else ""
            queues["out_of_range"].append(r)
        days_since = None
        if r.get("last_take_time"):
            days_since = int((common.now_ms() - int(r["last_take_time"])) / MS_DAY)
        r["days_since_last_exchange"] = days_since if days_since is not None else r.get("active_days")
        if r["battery_sn"] and (r.get("days_since_last_exchange") or 0) > persuade_days:
            r["action"] = "劝退"
            r["action_reason"] = f"距今 {r['days_since_last_exchange']} 天未换电（>2 个月）"
            queues["persuade"].append(r)
        else:
            lvl = low_freq_level(r)
            if lvl:
                r["level"] = lvl
                r["action"] = "引导换电" if (r.get("power") is None or float(r.get("power") or 0) >= 25) else "引导换电或退订"
                r["action_reason"] = f"低频 {lvl}（月均频次 {r['monthly_freq']} 次/月）"
                queues["activate"].append(r)
    for key in queues:
        queues[key].sort(key=lambda x: x.get("days_since_last_exchange") or 0, reverse=True)
    _qcap = int(cfg.get("queue_cap", 20000))
    return {
        "queues": {k: v[:_qcap] for k, v in queues.items()},
        "counts": {k: len(v) for k, v in queues.items()},
    }


def summary(f: Filters) -> Dict[str, Any]:
    silent = silent_users(f)
    low = low_freq_users(f)
    levels = {"L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0}
    for r in low:
        if r.get("level") in levels:
            levels[r["level"]] += 1
    base = base_rows(f)
    abnormal = [r for r in base if _flow_abnormal(r)]
    return {
        "kpi": {
            "active_agreements": len(base),
            "silent_count": len(silent),
            "low_freq_count": len(low),
            "abnormal_count": len(abnormal),
            "battery_in_hand": sum(1 for r in base if r.get("battery_sn")),
            "monthly_freq_avg": round(
                sum(r["monthly_freq"] for r in low) / len(low), 2) if low else 0.0,
        },
        "levels": levels,
        "abnormal_samples": [
            {"agreement_id": r["id"], "user_id": r["user_id"], "battery_sn": r["battery_sn"],
             "reason": _flow_abnormal(r)} for r in abnormal[:20]
        ],
    }


def silent_list(f: Filters, page: int = 1, size: int = 50, keyword: str = "",
                cap: int = 500) -> Dict[str, Any]:
    rows = silent_users(f)
    if keyword:
        rows = [r for r in rows if keyword in (r.get("user_name") or "") or keyword in (r.get("user_phone") or "")
                or keyword in (r.get("battery_sn") or "")]
    rows.sort(key=lambda r: -(r.get("days_since_last_exchange") or 0))
    return common.paginate(rows, page, size, cap=cap)


def low_freq_list(f: Filters, level: str = "", page: int = 1, size: int = 50, keyword: str = "",
                  cap: int = 500) -> Dict[str, Any]:
    rows = low_freq_users(f)
    if level:
        rows = [r for r in rows if r.get("level") == level]
    if keyword:
        rows = [r for r in rows if keyword in (r.get("user_name") or "") or keyword in (r.get("user_phone") or "")]
    rows.sort(key=lambda r: (r.get("monthly_freq") or 0))
    return common.paginate(rows, page, size, cap=cap)


def leads_list(f: Filters, group: str = "silent", level: str = "", page: int = 1, size: int = 50,
               keyword: str = "", cap: int = 500) -> Dict[str, Any]:
    """线索统一明细：沉默（group=silent）或低频（group=low_freq），附线索展示字段。"""
    rows = silent_users(f) if group == "silent" else low_freq_users(f)
    if level:
        rows = [r for r in rows if r.get("level") == level]
    if keyword:
        kw = keyword.strip()
        rows = [r for r in rows if kw in (r.get("user_name") or "") or kw in (r.get("user_phone") or "")
                or kw in (r.get("battery_sn") or "")]
    rows.sort(key=lambda r: -(r.get("days_since_last_exchange") or 0))
    out = []
    for r in rows:
        rec = dict(r)
        rec.update(lead_fields(r))
        out.append(rec)
    return common.paginate(out, page, size, cap=cap)


def lead_fields(r: Dict[str, Any]) -> Dict[str, Any]:
    """线索展示字段：持有电池 SN / 最后定位 / 最后换电网点 / 在线状态 / 电量 / 是否已归还柜机。"""
    return {
        "battery_sn": r.get("battery_sn") or "—",
        "last_location_address": r.get("last_location_address") or "—",
        "last_site": r.get("last_site") or "—",
        "online_status": r.get("online_status") or "—",
        "power": r.get("power"),
        "returned": bool(r.get("last_back_exchange_sn")),
        "last_back_exchange_sn": r.get("last_back_exchange_sn") or "",
    }


# --------------------------------------------------------------------------- 展示层补全 / KPI / 下钻

def _enrich_page(rows: List[Dict[str, Any]], f: Filters) -> List[Dict[str, Any]]:
    """分页后补全「用户当前手机号」与押金划扣口径信息（按页取数，避免全量等待）。"""
    if not rows:
        return rows
    uids = [r.get("user_id") for r in rows]
    phones = common.user_phone_map(uids)
    dep = deposit.due_rows_for(f, [r.get("id") for r in rows])
    for r in rows:
        uid = int(r.get("user_id") or 0)
        r["current_phone"] = phones.get(uid) or ""
        r["user_phone"] = r.get("user_phone") or ""
        d = dep.get(int(r.get("id") or 0))
        r["deduct_status_cn"] = cn.cn("deduct_status", d.get("deduct_status")) if d else "未纳入划扣口径"
        r["failure_name"] = (d.get("failure_name") if d else "") or "—"
        if d:
            r["deposit_expire_str"] = d.get("deposit_expire_str") or r.get("deposit_expire_str")
            r["deposit_expire_ms"] = d.get("deposit_expire_ms")
            r["deposit_days_left"] = d.get("deposit_days_left")
            r["deposit_stage_cn"] = d.get("stage_cn")
            r["deposit_warn_text"] = d.get("warn_text")
        r.setdefault("level", r.get("level") or "")
        r.update(lead_fields(r))
    return rows


KPI_CARDS = [
    ("active_agreements", "生效中协议", "人", "协议状态 ∈ 生效中 / 欠租中 / 已暂停", "active"),
    ("silent_count", "沉默用户", "人", "近 90 天 0 次换电，且租期已到期或状态为欠租中", "silent"),
    ("low_freq_count", "低频用户", "人", "命中 L1-L5 低频分层的用户", "low_freq"),
    ("battery_in_hand", "持有电池用户", "人", "能定位到最后一次换电电池 SN 的生效协议", "in_hand"),
    ("abnormal_count", "电池流通异常", "人", "电池已归还柜机 / 不在使用状态 / 命中异常事件（已从线索中剔除）", "abnormal"),
    ("action_persuade", "劝退动作", "人", "持有电池且超过 2 个月未换电", "action_persuade"),
    ("action_activate", "引导换电动作", "人", "低频 L1-L5 且需引导换电", "action_activate"),
    ("action_out_of_range", "超范围预警", "人", "最后定位与运营城市不一致 / 距最近网点 > 5km / 命中超范围事件", "action_out_of_range"),
    ("monthly_freq_avg", "低频用户月均换电", "次/月", "低频用户近 60 天换电次数 / 2", "low_freq"),
]


def kpi_cards(f: Filters) -> List[Dict[str, Any]]:
    kpi = summary(f)["kpi"]
    counts = action_queue(f)["counts"]
    vals = dict(kpi)
    vals["action_persuade"] = counts.get("persuade", 0)
    vals["action_activate"] = counts.get("activate", 0)
    vals["action_out_of_range"] = counts.get("out_of_range", 0)
    out = []
    for key, label, unit, basis, drill in KPI_CARDS:
        out.append({"key": key, "label": label, "value": vals.get(key, 0), "unit": unit,
                    "drill": drill, "basis": basis})
    return out


DRILL_LABELS = {
    "active": "生效中协议明细（含未进入线索的用户）",
    "silent": "沉默用户线索（近 90 天 0 次换电且租期到期/欠租）",
    "low_freq": "低频用户线索（L1-L5 分层）",
    "in_hand": "持有电池用户明细",
    "abnormal": "电池流通异常明细",
    "action_persuade": "动作队列 · 劝退",
    "action_activate": "动作队列 · 引导换电",
    "action_out_of_range": "动作队列 · 超运营范围预警",
}


def drill_list(f: Filters, drill: str = "silent", page: int = 1, size: int = 50,
               keyword: str = "", level: str = "", cap: int = 500) -> Dict[str, Any]:
    """线索/动作统一明细入口：一个 drill 键对应一张卡片的明细数据。"""
    drill = (drill or "silent").strip()
    base = base_rows(f)
    if drill == "active":
        rows = [dict(r) for r in base]
    elif drill == "in_hand":
        rows = [dict(r) for r in base if r.get("battery_sn")]
    elif drill == "abnormal":
        rows = []
        for r in base:
            reason = _flow_abnormal(r)
            if reason:
                rec = dict(r)
                rec["abnormal_flow"] = True
                rec["abnormal_reason"] = reason
                rows.append(rec)
    elif drill == "silent":
        rows = list(silent_users(f))
    elif drill.startswith("low_L"):
        lv = drill.split("low_", 1)[1]
        rows = [r for r in low_freq_users(f) if r.get("level") == lv]
    elif drill == "low_freq":
        rows = list(low_freq_users(f))
    elif drill.startswith("action_"):
        key = drill.split("action_", 1)[1]
        rows = list(action_queue(f)["queues"].get(key) or [])
    else:
        rows = list(silent_users(f))
    if level:
        rows = [r for r in rows if r.get("level") == level]
    if keyword:
        kw = keyword.strip()
        rows = [r for r in rows
                if kw in (r.get("user_name") or "") or kw in (r.get("user_phone") or "")
                or kw in (r.get("battery_sn") or "") or kw in str(r.get("id") or "")
                or kw in str(r.get("user_id") or "")]
    rows.sort(key=lambda r: -(r.get("days_since_last_exchange") or 0))
    data = common.paginate(rows, page, size, cap=cap)
    _enrich_page(data["rows"], f)
    data["drill"] = {"key": drill,
                     "label": DRILL_LABELS.get(drill, DRILL_LABELS.get("silent")),
                     "basis": dict((k[4], k[3]) for k in KPI_CARDS).get(drill, "沉默低频线索口径")}
    return data
