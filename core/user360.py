"""用户 360 明细表：一行一份协议，含押金/频次/消费/电池/客服/标签/评价/处置建议。

性能设计：
  - 浏览（不带 advice 过滤）：SQL 层直接按协议分页（ORDER BY 租期到期），仅对当前页补全附属数据；
  - 按「综合处置建议」过滤 / 全量导出：走后端全量构建，结果进进程内缓存（TTL 600s）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from config import settings
from core import cn, common, dataset, db, deposit, silent
from core.common import MS_DAY, Filters, ms_to_str

ACTIVE_STATUS = ("working", "owe_rent", "paused", "unsubscribing")

FIELDS = """a.id, a.user_id, a.user_name, a.user_phone, a.sys_city_name, a.battery_product_id,
       a.type, a.status, a.activation_time, a.rent_expire_time, a.deposit_status,
       a.deposit_fee, a.deposit_real_fee, a.deposit_payway, a.deposit_bind_time,
       a.deposit_unbind_time, a.user_rent_id,
       a.contract_expire_time, a.is_auto_pay, a.battery_take_status,
       pr.name AS product_name,
       rp.name AS rent_package_name,
       ur.card_status AS rent_card_status, ur.expire_time AS rent_card_expire"""

DRILL_LABELS = {
    "all": "全部生效中协议",
    "deposit_on": "押金在账协议（押金金额 > 0）",
}


def _drill_cond(drill: str) -> str:
    if drill == "deposit_on":
        return "a.deposit_status='on' AND a.deposit_fee > 0"
    return ""

JOINS = f"""
    FROM {db.biz('t_exchange_agreement')} a
    LEFT JOIN {db.biz('t_battery_product')} pr ON pr.id = a.battery_product_id
    LEFT JOIN {db.biz('t_user_exchange_rent')} ur ON ur.id = a.user_rent_id
    LEFT JOIN {db.biz('t_exchange_rent_package')} rp ON rp.id = ur.package_id"""

ORDER = "ORDER BY a.rent_expire_time ASC, a.id ASC"
CHUNK = 5000


# --------------------------------------------------------------------------- SQL 条件

def _sn_uids(kw: str) -> List[int]:
    """按电池 SN 反查持有该电池的用户（复用最后换电缓存，供关键词检索）。"""
    k = (kw or "").upper()
    if not k:
        return []
    lasts = dataset.last_exchange()
    return [uid for uid, v in lasts.items() if k in str(v.get("battery_sn") or "").upper()][:5000]


def _where(f: Filters, keyword: str = "", drill: str = "") -> Tuple[str, list]:
    where, args = f.agreement_where()
    parts = [where, f"a.status IN {ACTIVE_STATUS}"]
    wcond, wargs = f.deposit_warn_cond()
    if wcond:
        parts.append(wcond)
        args = list(args) + list(wargs)
    dc = _drill_cond(drill)
    if dc:
        parts.append(dc)
    kw = (keyword or "").strip()
    if kw:
        cond = ["a.user_name LIKE %s", "a.user_phone LIKE %s"]
        args = list(args) + [f"%{kw}%", f"%{kw}%"]
        uids = _sn_uids(kw)
        if uids:
            cond.append(f"a.user_id IN ({','.join(['%s'] * len(uids))})")
            args += [int(u) for u in uids]
        if kw.isdigit():
            cond.append("a.user_id = %s")
            args.append(int(kw))
        parts.append("(" + " OR ".join(cond) + ")")
    return " AND ".join(parts), args


def count(f: Filters, keyword: str = "", drill: str = "") -> int:
    cond, args = _where(f, keyword, drill)
    rows = db.query(f"SELECT COUNT(*) FROM {db.biz('t_exchange_agreement')} a WHERE {cond}",
                    list(args), ttl=180)
    return int(rows[0][0] or 0) if rows else 0


def _raw(f: Filters, keyword: str, limit: int, offset: int, drill: str = "") -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    cond, args = _where(f, keyword, drill)
    sql = f"SELECT {FIELDS} {JOINS} WHERE {cond} {ORDER} LIMIT {int(limit)} OFFSET {int(offset)}"
    return db.query_dicts(sql, list(args), ttl=180)


def _raw_all(f: Filters, keyword: str, max_rows: int = 0, drill: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        chunk = _raw(f, keyword, CHUNK, offset, drill)
        out.extend(chunk)
        if len(chunk) < CHUNK:
            break
        offset += CHUNK
        if max_rows and len(out) >= max_rows:
            break
    return out[:max_rows] if max_rows else out


# --------------------------------------------------------------------------- 行构建

def _build_rows(base: List[Dict[str, Any]], f: Filters,
                dep_map: Optional[Dict[int, Dict[str, Any]]] = None,
                ext: bool = True, full: bool = False) -> List[Dict[str, Any]]:
    if not base:
        return []
    uids = [b.get("user_id") for b in base]
    aids = [b.get("id") for b in base]
    phones = common.user_phone_map(uids)
    if full:
        dataset.prewarm()
        wins = dataset.order_windows()
        lasts = dataset.last_exchange()
    else:
        # 分页浏览：只按当前页用户取窗口统计与最后换电记录，避免全量聚合等待
        wins = dataset.order_windows_for(uids)
        lasts = dataset.last_exchange_for(uids)
    short = dataset.city_short_names()
    sns = [(lasts.get(int(b.get("user_id") or 0)) or {}).get("battery_sn") for b in base]
    bmap = common.battery_map([s for s in sns if s])
    tags = common.user_tags(uids) if ext else {}
    scores = common.feedback_scores(uids) if ext else {}
    notes = common.reception_notes(aids) if ext else {}
    cycles = common.cyclepay_map(aids) if ext else {}
    if dep_map is not None:
        dep = dep_map
    elif full:
        dep = {r["id"]: r for r in deposit.due_rows(f)}
    else:
        dep = deposit.due_rows_for(f, aids)
    now = common.now_ms()
    out: List[Dict[str, Any]] = []
    for b in base:
        uid = int(b.get("user_id") or 0)
        aid = int(b.get("id") or 0)
        st = wins.get(uid, {})
        last = lasts.get(uid, {})
        sn = last.get("battery_sn") or ""
        bat = bmap.get(sn, {}) if sn else {}
        act = int(b.get("activation_time") or 0)
        active_days = int((now - act) / MS_DAY) if act else 0
        take_time = int(last.get("take_time") or 0)
        days_since = int((now - take_time) / MS_DAY) if take_time else active_days
        city = b.get("sys_city_name") or ""
        row = {
            "agreement_id": aid,
            "city": city,
            "product": b.get("product_name") or f"产品{b.get('battery_product_id')}",
            "line": last.get("site_name") or "—",
            "user_id": uid,
            "user_name": b.get("user_name") or "",
            "user_phone": b.get("user_phone") or "",
            "current_phone": phones.get(uid) or "",
            "agreement_type": b.get("type") or "",
            "agreement_status": b.get("status") or "",
            "agreement_type_cn": cn.cn("agreement_type", b.get("type")),
            "agreement_status_cn": cn.cn("agreement_status", b.get("status")),
            "activation_time": ms_to_str(act),
            "rent_expire_time": ms_to_str(b.get("rent_expire_time")),
            "active_days": active_days,
            "package_name": b.get("rent_package_name") or "—",
            "rent_card_status": b.get("rent_card_status") or "",
            "rent_card_status_cn": cn.cn("card_status", b.get("rent_card_status")),
            "rent_card_expire": ms_to_str(b.get("rent_card_expire")),
            "deposit_fee": int(b.get("deposit_fee") or 0),
            "deposit_fee_yuan": round(int(b.get("deposit_fee") or 0) / 100.0, 2),
            "deposit_status": b.get("deposit_status") or "",
            "deposit_status_cn": cn.cn("deposit_status", b.get("deposit_status")),
            "deposit_payway": b.get("deposit_payway") or "",
            "deposit_payway_cn": cn.cn("deposit_payway", b.get("deposit_payway")),
            "deposit_bind_str": ms_to_str(b.get("deposit_bind_time")),
            "n30": int(st.get("n30") or 0),
            "n90": int(st.get("n90") or 0),
            "monthly_freq": round(int(st.get("back60") or 0) / 2.0, 2),
            "fee30_yuan": round(int(st.get("fee30") or 0) / 100.0, 2),
            "fee90_yuan": round(int(st.get("fee90") or 0) / 100.0, 2),
            "fee180_yuan": round(int(st.get("fee180") or 0) / 100.0, 2),
            "battery_sn": sn or "—",
            "battery_online": bat.get("online_status") or "",
            "battery_online_cn": cn.cn("online_status", bat.get("online_status")),
            "battery_status_cn": cn.cn("battery_status", bat.get("battery_status")),
            "battery_power": bat.get("power"),
            "battery_location": bat.get("last_location_address") or "",
            "last_site": last.get("site_name") or "—",
            "last_take_time": ms_to_str(take_time),
            "days_since_last_exchange": days_since,
            "returned": bool(bat.get("last_back_exchange_sn")),
            "tags": tags.get(uid, []),
            "score": (scores.get(uid) or {}).get("avg_score"),
            "score_cnt": (scores.get(uid) or {}).get("cnt", 0),
            "note": (notes.get(aid) or {}).get("detail") or "",
            "note_time": (notes.get(aid) or {}).get("time") or "",
        }
        cyc = cycles.get(aid) or {}
        row["cycle_status"] = cyc.get("status") or ""
        row["cycle_status_cn"] = cn.cn("cycle_status", cyc.get("status"))
        row["cycle_sign_status_cn"] = cn.cn("cycle_sign_status", cyc.get("sign_status"))
        row["cycle_unsign_reason"] = cyc.get("unsign_reason") or ""
        row["last_deduct_str"] = ms_to_str(cyc.get("last_deduct_time"))

        # 押金：绑定/创建时间、到期时间（绑定时间 + 1 年）、划扣状态与失败原因
        d = dep.get(aid)
        bind = int(b.get("deposit_bind_time") or 0)
        dep_expire = cn.deposit_expire_ms(bind, b.get("rent_expire_time"))
        row["deposit_bind_str"] = ms_to_str(bind)
        row["deposit_unbind_str"] = ms_to_str(b.get("deposit_unbind_time"))
        row["deposit_expire_str"] = ms_to_str(dep_expire)
        row["deposit_expire_basis"] = "押金绑定时间 + 1 年" if bind else "租期到期时间（回退）"
        row["deposit_days_left"] = common.days_left(dep_expire)
        row["deduct_status"] = (d or {}).get("deduct_status") or ""
        row["deduct_status_cn"] = cn.cn("deduct_status", row["deduct_status"]) if row["deduct_status"] else "—"
        row["failure_name"] = (d or {}).get("failure_name") or "—"
        row["failure_evidence"] = (d or {}).get("failure_evidence") or ""
        row["suggest_fee_yuan"] = (d or {}).get("suggest_fee_yuan")

        # 押金预警文案（与押金板块同一口径）
        left = row["deposit_days_left"]
        if d and d.get("warn_text"):
            row["deposit_warn"] = d["warn_text"]
        elif row["deposit_status"] == "on" and int(row["deposit_fee"] or 0) > 0:
            if left is None:
                row["deposit_warn"] = "无押金到期时间"
            elif left >= 0:
                row["deposit_warn"] = f"剩 {left} 天押金到期"
            else:
                row["deposit_warn"] = f"已逾期 {abs(left)} 天押金到期"
        else:
            row["deposit_warn"] = "无押金或押金已退"

        # 综合处置建议
        advice_items: List[str] = []
        if d and d.get("deduct_status") == "fail":
            advice_items.append("划扣")
        flow_reason = silent._flow_abnormal({
            "battery_events": {}, "last_take_time": take_time,
            "battery_status": bat.get("battery_status") or "",
            "last_back_exchange_sn": bat.get("last_back_exchange_sn") or "",
        })
        if flow_reason:
            advice_items.append("回收")
        if days_since and days_since > 60:
            advice_items.append("劝退")
        elif days_since and days_since > 30:
            advice_items.append("激活")
        loc = bat.get("last_location_address") or ""
        sc = short.get(city, city)
        if loc and sc and sc not in loc:
            advice_items.append("预警")
        row["abnormal_flow_reason"] = flow_reason
        row["advice"] = " / ".join(dict.fromkeys(advice_items)) if advice_items else "正常"
        out.append(row)
    return out


# --------------------------------------------------------------------------- 全量（缓存）

def full_rows(f: Filters, keyword: str = "", max_rows: int = 0,
              drill: str = "") -> List[Dict[str, Any]]:
    key = "u360full:" + common.filter_key(f) + "|" + (keyword or "") + f"|{max_rows}|{drill}"
    return common.memo(key, 600, lambda: _build_rows(_raw_all(f, keyword, max_rows, drill), f, full=True))


# --------------------------------------------------------------------------- 对外接口

def rows(f: Filters, keyword: str = "", advice: str = "", page: int = 1, size: int = 20,
         drill: str = "") -> Dict[str, Any]:
    """分页明细。带 advice 过滤时先做全量构建（首次较慢，结果缓存）。"""
    if advice:
        data = [r for r in full_rows(f, keyword, 0, drill) if advice in (r.get("advice") or "")]
        data = common.paginate(data, page, size, cap=2000)
        data["drill"] = {"key": drill or "all", "label": DRILL_LABELS.get(drill or "all", "全部生效中协议")}
        return data
    total = count(f, keyword, drill)
    size = max(1, min(int(size or 20), 200))
    page = max(1, int(page or 1))
    if (page - 1) * size >= total:
        return {"total": total, "page": page, "size": size, "rows": [],
                "drill": {"key": drill or "all", "label": DRILL_LABELS.get(drill or "all", "全部生效中协议")}}
    base = _raw(f, keyword, size, (page - 1) * size, drill)
    out = {"total": total, "page": page, "size": size, "rows": _build_rows(base, f),
           "drill": {"key": drill or "all", "label": DRILL_LABELS.get(drill or "all", "全部生效中协议")}}
    return out


def export_rows(f: Filters, keyword: str = "", max_rows: int = 30000,
                drill: str = "") -> List[Dict[str, Any]]:
    return full_rows(f, keyword, max_rows, drill)[:max_rows]


def counts(f: Filters) -> Dict[str, int]:
    """用户 360 页 KPI 基础计数（全量构建，结果随 full_rows 缓存）。"""
    data = full_rows(f)
    adv = {"划扣": 0, "回收": 0, "劝退": 0, "激活": 0, "预警": 0, "正常": 0}
    abnormal = 0
    for r in data:
        a = r.get("advice") or "正常"
        if a == "正常":
            adv["正常"] += 1
        else:
            abnormal += 1
        for k in a.split(" / "):
            if k in adv:
                adv[k] += 1
    return {"total": len(data), "abnormal": abnormal, "advice": adv}


KPI_CARDS = [
    ("total", "生效中协议", "份", "当前筛选条件下生效中的协议总数"),
    ("deposit_on", "押金在账协议", "份", "押金状态=在账且押金金额 > 0 的协议"),
    ("deposit_fail", "划扣失败协议", "份", "押金到期划扣失败（未支付订单/代扣解约/渠道异常）"),
    ("abnormal", "需处置协议", "份", "综合处置建议非「正常」的协议（含划扣/回收/劝退/激活/预警）"),
    ("advice_jitui", "建议劝退", "份", "超过 60 天未换电"),
    ("advice_jihuo", "建议激活", "份", "30-60 天未换电"),
    ("advice_huishou", "建议回收", "份", "电池流转异常（如电池未归还/已入库异常）"),
]


def kpi_cards(f: Filters) -> List[Dict[str, Any]]:
    data = full_rows(f)
    cnt = counts(f)
    fails = sum(1 for r in data if r.get("deduct_status") == "fail")
    on_cnt = sum(1 for r in data if r.get("deposit_status") == "on" and int(r.get("deposit_fee") or 0) > 0)
    adv = cnt["advice"]
    vals = {"total": cnt["total"], "deposit_on": on_cnt, "deposit_fail": fails,
            "abnormal": cnt["abnormal"], "advice_jitui": adv["劝退"],
            "advice_jihuo": adv["激活"], "advice_huishou": adv["回收"]}
    drills = {"total": "all", "deposit_on": "deposit_on", "deposit_fail": "deposit_fail",
              "abnormal": "abnormal", "advice_jitui": "advice_jitui",
              "advice_jihuo": "advice_jihuo", "advice_huishou": "advice_huishou"}
    out: List[Dict[str, Any]] = []
    for key, label, unit, basis in KPI_CARDS:
        out.append({"key": key, "label": label, "value": vals.get(key, 0), "unit": unit,
                    "drill": drills.get(key, key), "basis": basis})
    return out


def drill_rows(f: Filters, drill: str = "", keyword: str = "", page: int = 1, size: int = 50,
               cap: int = 5000) -> Dict[str, Any]:
    """KPI 下钻：直接返回该 KPI 口径对应的明细数据（含用户协议ID与当前手机号）。"""
    if drill in ("all", "deposit_on", ""):
        out = rows(f, keyword=keyword, page=page, size=size, drill="" if drill == "all" else drill)
        out["drill"] = {"key": drill or "all",
                        "label": DRILL_LABELS.get(drill or "all", "全部生效中协议"),
                        "basis": "用户协议ID = t_exchange_agreement.id；当前手机号 = t_user.phone"}
        return out
    data = full_rows(f)
    if drill == "deposit_fail":
        data = [r for r in data if r.get("deduct_status") == "fail"]
        label, basis = "划扣失败协议", "押金到期划扣失败（未支付押金订单 / 周期代扣已解约 / 支付渠道异常）"
    elif drill == "abnormal":
        data = [r for r in data if (r.get("advice") or "正常") != "正常"]
        label, basis = "需处置协议", "综合处置建议非「正常」"
    elif drill.startswith("advice_"):
        kw = {"advice_jitui": "劝退", "advice_jihuo": "激活", "advice_huishou": "回收"}.get(drill, "")
        data = [r for r in data if kw and kw in (r.get("advice") or "")]
        label, basis = f"建议{kw}", f"综合处置建议包含「{kw}」"
    else:
        label, basis = DRILL_LABELS.get(drill, "全部生效中协议"), ""
    if keyword:
        kw = keyword.strip()
        data = [r for r in data if kw in (r.get("user_name") or "") or kw in (r.get("user_phone") or "")
                or kw in (r.get("current_phone") or "") or kw in str(r.get("user_id") or "")
                or kw in str(r.get("agreement_id") or "")]
    out = common.paginate(data, page, size, cap=cap)
    out["drill"] = {"key": drill, "label": label, "basis": basis}
    return out


def summary(f: Filters) -> Dict[str, Any]:
    cnt = counts(f)
    return {"total": cnt["total"], "advice_count": cnt["advice"], "abnormal": cnt["abnormal"]}
