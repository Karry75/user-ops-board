"""策略价值条：押金划扣漏斗 + 客服接待成效（劝退 / 引导换电）。

页面结构：上方 KPI 卡片（可点击下钻） + 下方明细表。
明细表统一包含「用户协议ID」与「用户当前手机号」两列。
"""
from __future__ import annotations

from typing import Any, Dict, List

from config import settings
from core import common, db, deposit
from core.common import MS_DAY, Filters, ms_to_str


def _cfg() -> Dict[str, Any]:
    return settings.load_thresholds().get("strategy_value", {}) or {}


FUNNEL_STAGES = [
    ("should", "应划扣", "T-30 窗口内押金到期（含已逾期）的押金在账协议"),
    ("success", "划扣成功", "押金订单已支付或已完成（order_status=success 或 is_pay=1）"),
    ("fail", "划扣失败", "押金订单未支付 / 周期代扣已解约 / 支付渠道异常 / 协议已停用"),
    ("recover", "挽回", "历史曾出现未支付押金订单、当前已完成划扣的协议"),
]


def deposit_funnel(f: Filters) -> Dict[str, Any]:
    ov = deposit.overview(f)
    k = ov["kpi"]
    items = []
    for key, label, basis in FUNNEL_STAGES:
        if key == "should":
            cnt, fee = k["should_count"], k["should_fee"]
        elif key == "success":
            cnt, fee = k["success_count"], k["success_fee"]
        elif key == "fail":
            cnt, fee = k["fail_count"], k["fail_fee"]
        else:
            cnt, fee = k["recover_count"], k["recover_fee"]
        items.append({"key": key, "label": label, "count": cnt, "fee": fee,
                      "fee_yuan": round(fee / 100.0, 2), "basis": basis})
    return {
        "items": items,
        "fail_rate": k["fail_rate"],
        "recover_rate": round(k["recover_count"] / k["should_count"] * 100, 2) if k["should_count"] else 0.0,
        "basis": "应划扣=押金在账且押金到期进入 T-30 窗口（押金到期=押金绑定时间+1 年）；"
                 "成功=押金订单已支付/已完成；失败=押金订单未支付或周期代扣已解约或支付渠道异常；"
                 "挽回=历史曾出现未支付订单、当前已完成划扣的协议",
    }


def funnel_list(f: Filters, stage: str = "should", page: int = 1, size: int = 50,
                keyword: str = "") -> Dict[str, Any]:
    """漏斗环节明细（直接复用押金板块下钻口径，含用户协议ID与当前手机号）。"""
    key = stage if stage in ("should", "success", "fail", "recover") else "should"
    data = deposit.warn_list(f, page=page, size=size, keyword=keyword, drill=key)
    label = dict((k, lb) for k, lb, _ in FUNNEL_STAGES).get(key, "应划扣")
    data["drill"] = {"key": key, "label": f"押金划扣漏斗 · {label}",
                     "basis": dict((k, b) for k, _, b in FUNNEL_STAGES).get(key, "")}
    return data


def reception_effect(f: Filters, days: int = 90) -> Dict[str, Any]:
    """从客服接待记录统计劝退 / 引导换电成效。"""
    cfg = _cfg()
    persuade_kw = cfg.get("persuade_keywords", []) or []
    activate_kw = cfg.get("activate_keywords", []) or []
    now = common.now_ms()
    where, args = f.agreement_where(alias="a")
    sql = f"""
    SELECT r.id, r.agreement_id, r.type, r.detail, r.create_time, a.user_id, a.sys_city_name
    FROM {db.biz('t_reception_log')} r
    JOIN {db.biz('t_exchange_agreement')} a ON a.id = r.agreement_id
    WHERE r.is_del=0 AND r.create_time >= %s AND {where}
    """
    rows = db.query_dicts(sql, [now - days * MS_DAY] + list(args), ttl=300)
    persuade, activate = [], []
    for r in rows:
        detail = r.get("detail") or ""
        item = {
            "agreement_id": r.get("agreement_id"), "user_id": r.get("user_id"),
            "city": r.get("sys_city_name"), "type": r.get("type"),
            "detail": detail[:120], "time": ms_to_str(r.get("create_time")),
            "_ts": int(r.get("create_time") or 0),
        }
        if any(kw in detail for kw in persuade_kw):
            item["kind"] = "劝退"
            persuade.append(item)
        if any(kw in detail for kw in activate_kw):
            item["kind"] = "引导"
            activate.append(item)

    def _recovered(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """接待后 30 天内是否恢复换电（成效判定）。"""
        uids = sorted({int(i["user_id"]) for i in items if i.get("user_id")})
        if not uids:
            return {"recovered": 0, "rate": 0.0}
        first_ts: Dict[int, int] = {}
        for i in items:
            uid = int(i["user_id"])
            ts = i["_ts"]
            if uid not in first_ts or ts < first_ts[uid]:
                first_ts[uid] = ts
        orders: Dict[int, List[int]] = {}
        step = 2000
        for i in range(0, len(uids), step):
            batch = uids[i:i + step]
            ph = ",".join(["%s"] * len(batch))
            for uid, ct in db.query(
                f"""SELECT take_user_id, create_time FROM {db.biz('t_exchange_order')}
                    WHERE is_del=0 AND create_time >= %s AND take_user_id IN ({ph})""",
                [now - days * MS_DAY] + batch, ttl=300,
            ):
                orders.setdefault(int(uid), []).append(int(ct or 0))
        recovered = 0
        for uid, ts in first_ts.items():
            window = 30 * MS_DAY
            if any(ts < ct <= ts + window for ct in orders.get(uid, [])):
                recovered += 1
        return {"recovered": recovered, "rate": round(recovered / len(uids) * 100, 2)}

    # 统一补全「用户协议ID / 签约手机号 / 当前手机号」
    all_items = persuade + activate
    p = _contacts(all_items)
    for it in all_items:
        aid = int(it.get("agreement_id") or 0)
        uid = int(it.get("user_id") or 0)
        it["user_phone"] = (p.get(aid) or {}).get("user_phone", "")
        it["current_phone"] = (p.get(aid) or {}).get("current_phone") or common.user_phone_map([uid]).get(uid, "")

    return {
        "window_days": days,
        "reception_total": len(rows),
        "persuade": {"count": len(persuade), **_recovered(persuade), "samples": persuade[:20]},
        "activate": {"count": len(activate), **_recovered(activate), "samples": activate[:20]},
        "basis": "客服接待记录（t_reception_log.detail）关键词分类；成效=接待后 30 天内该用户产生换电订单；"
                 "当前手机号取自 t_user.phone",
        "keywords": {"persuade": persuade_kw, "activate": activate_kw},
    }


def _contacts(items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """协议 id → {签约手机号(协议 user_phone), 当前手机号(t_user.phone)}。"""
    aids = sorted({int(i["agreement_id"]) for i in items if i.get("agreement_id")})
    if not aids:
        return {}
    phone_map: Dict[int, str] = {}
    for i in range(0, len(aids), 2000):
        batch = aids[i:i + 2000]
        ph = ",".join(["%s"] * len(batch))
        for aid, uph, uid in db.query(
            f"SELECT id, user_phone, user_id FROM {db.biz('t_exchange_agreement')} WHERE id IN ({ph})",
            batch, ttl=300,
        ):
            phone_map[int(aid)] = uph or ""
            items_uid = int(uid or 0)
            if items_uid:
                phone_map.setdefault(-items_uid, "")
    uids = sorted({int(i["user_id"]) for i in items if i.get("user_id")})
    cur = common.user_phone_map(uids)
    out: Dict[int, Dict[str, Any]] = {}
    for i in items:
        aid = int(i.get("agreement_id") or 0)
        out[aid] = {"user_phone": phone_map.get(aid, ""),
                    "current_phone": cur.get(int(i.get("user_id") or 0), "")}
    return out


def kpi_cards(f: Filters, days: int = 90) -> List[Dict[str, Any]]:
    funnel = deposit_funnel(f)
    items = {i["key"]: i for i in funnel["items"]}
    rec = reception_effect(f, days=days)
    return [
        {"key": "should", "label": "应划扣用户", "value": items["should"]["count"], "unit": "人",
         "drill": "should", "basis": items["should"]["basis"]},
        {"key": "success", "label": "划扣成功用户", "value": items["success"]["count"], "unit": "人",
         "drill": "success", "basis": items["success"]["basis"]},
        {"key": "fail", "label": "划扣失败用户", "value": items["fail"]["count"], "unit": "人",
         "drill": "fail", "basis": items["fail"]["basis"]},
        {"key": "recover", "label": "挽回用户", "value": items["recover"]["count"], "unit": "人",
         "drill": "recover", "basis": items["recover"]["basis"]},
        {"key": "fail_rate", "label": "划扣失败率", "value": funnel["fail_rate"], "unit": "%",
         "drill": "fail", "basis": "划扣失败用户 / 应划扣用户（下钻展示失败用户明细）"},
        {"key": "recover_rate", "label": "挽回率", "value": funnel["recover_rate"], "unit": "%",
         "drill": "recover", "basis": "挽回用户 / 应划扣用户（下钻展示挽回用户明细）"},
        {"key": "reception", "label": f"客服接待（{days}天）", "value": rec["reception_total"], "unit": "次",
         "drill": "reception", "basis": "近 N 天客服接待记录（t_reception_log）"},
        {"key": "persuade", "label": "劝退记录", "value": rec["persuade"]["count"], "unit": "次",
         "drill": "reception_persuade", "basis": "接待内容命中劝退关键词的记录数"},
        {"key": "activate", "label": "引导换电记录", "value": rec["activate"]["count"], "unit": "次",
         "drill": "reception_activate", "basis": "接待内容命中引导换电关键词的记录数"},
    ]


def reception_rows(f: Filters, days: int = 90, kind: str = "") -> List[Dict[str, Any]]:
    """客服接待记录明细（全量）：命中劝退 / 引导换电关键词的接待记录。

    含「用户协议ID / 用户签约手机号 / 用户当前手机号」与关键词归类，供 KPI 下钻使用。
    """
    cfg = _cfg()
    persuade_kw = cfg.get("persuade_keywords", []) or []
    activate_kw = cfg.get("activate_keywords", []) or []
    now = common.now_ms()
    where, args = f.agreement_where(alias="a")
    sql = f"""
    SELECT r.id, r.agreement_id, r.type, r.detail, r.create_time, a.user_id, a.sys_city_name
    FROM {db.biz('t_reception_log')} r
    JOIN {db.biz('t_exchange_agreement')} a ON a.id = r.agreement_id
    WHERE r.is_del=0 AND r.create_time >= %s AND {where}
    """
    raw = db.query_dicts(sql, [now - days * MS_DAY] + list(args), ttl=300)
    items: Dict[int, Dict[str, Any]] = {}
    for r in raw:
        detail = r.get("detail") or ""
        kinds = []
        if any(kw in detail for kw in persuade_kw):
            kinds.append("劝退")
        if any(kw in detail for kw in activate_kw):
            kinds.append("引导")
        rid = int(r.get("id") or 0)
        it = items.get(rid)
        if it is None:
            it = {"id": rid, "agreement_id": r.get("agreement_id"), "user_id": r.get("user_id"),
                  "city": r.get("sys_city_name"), "type": r.get("type"), "detail": detail[:120],
                  "time": ms_to_str(r.get("create_time")), "kind": "", "_ts": int(r.get("create_time") or 0)}
            items[rid] = it
        for k in kinds:
            if k not in (it.get("kind") or ""):
                it["kind"] = (it.get("kind") + "/" + k) if it.get("kind") else k
        if not it["kind"]:
            it["kind"] = "其他"
    rows = [it for it in items.values() if (not kind) or (kind in (it.get("kind") or ""))]
    p = _contacts(rows)
    for it in rows:
        aid = int(it.get("agreement_id") or 0)
        uid = int(it.get("user_id") or 0)
        it["user_phone"] = (p.get(aid) or {}).get("user_phone", "")
        it["current_phone"] = (p.get(aid) or {}).get("current_phone") or common.user_phone_map([uid]).get(uid, "")
    rows.sort(key=lambda r: -int(r.get("_ts") or 0))
    return rows


def drill_rows(f: Filters, drill: str = "should", page: int = 1, size: int = 50,
               keyword: str = "", days: int = 90) -> Dict[str, Any]:
    """策略价值条 KPI 下钻：漏斗环节 → 押金明细；接待 KPI → 接待记录全量明细。"""
    if drill in ("should", "success", "fail", "recover", ""):
        return funnel_list(f, stage=drill or "should", page=page, size=size, keyword=keyword)
    kind = "劝退" if "persuade" in drill else ("引导" if "activate" in drill else "")
    rows = reception_rows(f, days=days, kind=kind)
    if keyword:
        kw = keyword.strip()
        rows = [r for r in rows if kw in str(r.get("agreement_id") or "") or kw in str(r.get("user_id") or "")
                or kw in (r.get("user_phone") or "") or kw in (r.get("current_phone") or "")
                or kw in (r.get("detail") or "")]
    out = common.paginate(rows, page, size, cap=20000)
    out["drill"] = {"key": drill or "reception",
                    "label": "客服接待记录明细" + (f" · {kind}" if kind else ""),
                    "basis": "t_reception_log 近 N 天记录（detail 关键词归类）；成效=接待后 30 天内产生换电订单"}
    return out
