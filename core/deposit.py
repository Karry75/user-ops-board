"""押金板块：足额划扣链路预警 + 失败归因 + 建议可划扣最大金额 + KPI。

到期口径：押金到期 = deposit_bind_time（押金绑定/创建时间）+ 1 年（超过一年自动解绑）；
无绑定时间时回退租期到期时间 rent_expire_time。字段经实测核查（temp/probe2.py / measure.py）。

判定与归因为「组合推导」：
  1) 押金订单（t_user_exchange_deposit_package_order）：order_status / is_pay / pay_way
  2) 周期代扣签约（t_user_cyclepay_sign_log）：status / unsign_reason
  3) 支付流水（t_pay_wechat_log，business_type=exchangeDepositOrder）：pay_status（含 PAYERROR）
  4) 还电/扣款回执（t_user_exchange_deposit_back_log）：back_status / error_code / error_message
  5) 协议状态（t_exchange_agreement.status）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from config import settings
from core import cn, common, dataset, db
from core.common import MS_DAY, Filters, ms_to_str

# 数据中"生效中"的协议状态（需求文档口径写作 status=use）
ACTIVE_STATUS = ("working", "owe_rent", "paused", "unsubscribing")
STOP_STATUS = ("stop", "stopped", "cancelled", "canceled", "unsubscribing")
UNPAID_RECENT_DAYS = 30
EXP_NOT_SET = "—"


def _thresholds() -> Dict[str, Any]:
    return settings.load_thresholds().get("deposit", {}) or {}


def _suggest_rule() -> Dict[str, Any]:
    return _thresholds().get("suggest_rule", {}) or {}


def _failure_rules() -> List[Dict[str, Any]]:
    return _thresholds().get("failure_rules", []) or []


def _expire_expr(alias: str = "a") -> str:
    """押金到期时间 SQL 表达式（毫秒时间戳）。"""
    return f"(COALESCE(NULLIF({alias}.deposit_bind_time, 0), {alias}.rent_expire_time) + {cn.YEAR_MS})"


# --------------------------------------------------------------------------- 应划扣集合

def due_rows(f: Filters, window_days: int = 30) -> List[Dict[str, Any]]:
    """应划扣集合：押金在账 + 押金到期进入 T-N 窗口（含已逾期）的协议（带 TTL 缓存）。"""
    key = "due:" + common.filter_key(f) + f":{window_days}"
    return common.memo(key, 180, lambda: _build_due(f, window_days))


def _due_query(f: Filters, window_days: int = 30,
               only_ids: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
    """应划扣集合查询；only_ids 非空时按协议 id 精确取（分页明细用）。"""
    where, args = f.agreement_where()
    now = common.now_ms()
    conds = [where, "a.deposit_status='on'", "a.deposit_fee > 0",
             "(a.deposit_bind_time > 0 OR a.rent_expire_time > 0)",
             f"a.status IN {ACTIVE_STATUS}"]
    params: List[Any] = list(args)
    wcond, wargs = f.deposit_warn_cond()
    if only_ids:
        ids = [int(x) for x in only_ids]
        conds.append("a.id IN (" + ",".join(["%s"] * len(ids)) + ")")
        params += ids
    elif wcond:
        conds.append(wcond)
        params += list(wargs)
    else:
        conds.append(f"{_expire_expr()} <= %s")
        params.append(now + int(window_days) * MS_DAY)
    sql = f"""
    SELECT a.id, a.user_id, a.user_name, a.user_phone, a.sys_city_name, a.battery_product_id,
           a.type, a.status, a.deposit_status, a.deposit_payway, a.deposit_fee, a.deposit_real_fee,
           a.deposit_bind_time, a.deposit_unbind_time, a.rent_expire_time, a.activation_time,
           a.deposit_package_order_id, a.is_auto_pay, a.user_rent_id, a.battery_take_status
    FROM {db.biz('t_exchange_agreement')} a
    WHERE {' AND '.join(conds)}
    """
    return db.query_dicts(sql, params, ttl=180)


def _build_due(f: Filters, window_days: int) -> List[Dict[str, Any]]:
    return _compute(_due_query(f, window_days), f)


def due_rows_for(f: Filters, agreement_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """仅计算指定协议集合的押金划扣状态（分页明细用，避免全量构建等待）。"""
    ids = sorted({int(i) for i in agreement_ids if i})
    if not ids:
        return {}
    rows = _due_query(f, only_ids=ids)
    if not rows:
        return {}
    return {int(r["id"]): r for r in _compute(rows, f)}


def _compute(rows: List[Dict[str, Any]], f: Filters) -> List[Dict[str, Any]]:
    if not rows:
        return []
    now = common.now_ms()
    ids = [int(r["id"]) for r in rows]
    pnames = common.product_name_map()
    orders = common.deposit_orders_map(ids)
    cycles = common.cyclepay_map(ids)
    unpaid_oids: List[int] = []
    for lst in orders.values():
        for o in lst:
            if (o.get("order_status") or "").lower() == "created" and int(o.get("is_pay") or 0) == 0:
                unpaid_oids.append(int(o["id"]))
    pays = common.pay_status_map(unpaid_oids) if unpaid_oids else {}
    recent_ms = now - UNPAID_RECENT_DAYS * MS_DAY
    windows: Optional[Dict[int, Dict[str, Any]]] = None

    for r in rows:
        aid = int(r["id"])
        bind = int(r.get("deposit_bind_time") or 0)
        expire = cn.deposit_expire_ms(bind, r.get("rent_expire_time"))
        left = common.days_left(expire)
        stage = cn.stage_of(left)
        r["days_left"] = left
        r["deposit_days_left"] = left
        r["stage"] = stage
        r["stage_cn"] = cn.stage_cn(stage)
        r["rent_days_left"] = common.days_left(r.get("rent_expire_time"))
        r["deposit_fee_yuan"] = round(int(r.get("deposit_fee") or 0) / 100.0, 2)
        r["deposit_real_fee_yuan"] = round(int(r.get("deposit_real_fee") or 0) / 100.0, 2)
        r["rent_expire_str"] = ms_to_str(r.get("rent_expire_time"))
        r["deposit_bind_str"] = ms_to_str(bind)
        r["deposit_unbind_str"] = ms_to_str(r.get("deposit_unbind_time"))
        r["deposit_expire_ms"] = expire
        r["deposit_expire_str"] = ms_to_str(expire)
        r["deposit_bind_source"] = "押金绑定时间" if bind else "租期到期时间(回退)"
        r["product_name"] = pnames.get(int(r.get("battery_product_id") or 0), "")
        r["agreement_type_cn"] = cn.cn("agreement_type", r.get("type"))
        r["agreement_status_cn"] = cn.cn("agreement_status", r.get("status"))
        r["deposit_status_cn"] = cn.cn("deposit_status", r.get("deposit_status"))
        r["deposit_payway_cn"] = cn.cn("deposit_payway", r.get("deposit_payway"))

        lst = sorted(orders.get(aid) or [], key=lambda o: (int(o.get("create_time") or 0), int(o.get("id") or 0)))
        cyc = cycles.get(aid) or {}
        unpaid = [o for o in lst
                  if (o.get("order_status") or "").lower() == "created" and int(o.get("is_pay") or 0) == 0]
        unpaid_recent = [o for o in unpaid if int(o.get("create_time") or 0) >= recent_ms]
        last = lst[-1] if lst else None
        last_unpaid = bool(last is not None
                           and (last.get("order_status") or "").lower() == "created"
                           and int(last.get("is_pay") or 0) == 0)
        paid = [o for o in lst
                if int(o.get("is_pay") or 0) == 1 or (o.get("order_status") or "").lower() in ("success", "refund")]
        cyc_status = (cyc.get("status") or "").upper()

        r["order_count"] = len(lst)
        r["deposit_order_create_str"] = ms_to_str(
            max([int(o.get("create_time") or 0) for o in lst]) if lst else 0)
        r["last_order_status"] = (last or {}).get("order_status") or ""
        r["last_order_status_cn"] = cn.cn("deposit_order_status", r["last_order_status"]) if lst else EXP_NOT_SET
        r["last_order_is_pay"] = int((last or {}).get("is_pay") or 0)
        r["last_order_pay_way"] = (last or {}).get("pay_way") or ""
        r["last_order_pay_way_cn"] = cn.cn("pay_way", r["last_order_pay_way"]) if lst else EXP_NOT_SET
        r["had_unpaid_ever"] = bool(unpaid)
        r["cycle_status"] = cyc.get("status") or ""
        r["cycle_status_cn"] = cn.cn("cycle_status", cyc.get("status"))
        r["cycle_sign_status"] = cyc.get("sign_status") or ""
        r["cycle_sign_status_cn"] = cn.cn("cycle_sign_status", cyc.get("sign_status"))
        r["cycle_unsign_reason"] = cyc.get("unsign_reason") or ""
        r["cycle_last_deduct"] = ms_to_str(cyc.get("last_deduct_time"))
        r["pay_status"] = ""
        r["pay_status_cn"] = ""
        r["failure_code"] = ""
        r["failure_name"] = ""
        r["failure_evidence"] = ""

        if last_unpaid or unpaid_recent or cyc_status == "UNSIGN":
            r["deduct_status"] = "fail"
            ev_order = (unpaid_recent or unpaid or [None])[0]
            ev_pay = (pays.get(int(ev_order["id"])) if ev_order else None) or {}
            r["pay_status"] = ev_pay.get("pay_status") or ""
            r["pay_status_cn"] = cn.cn("pay_status", r["pay_status"]) if r["pay_status"] else EXP_NOT_SET
            code, evidence = _classify(r, cyc, ev_pay, ev_order)
            r["failure_code"] = code
            r["failure_name"] = _rule_name(code)
            r["failure_evidence"] = evidence
        elif paid:
            r["deduct_status"] = "success"
        else:
            r["deduct_status"] = "unknown"
        r["deduct_status_cn"] = cn.cn("deduct_status", r["deduct_status"])

        # 建议可划扣金额（仅失败协议需要）
        if r["deduct_status"] == "fail":
            if windows is None:
                windows = dataset.order_windows()
            fee90 = int((windows.get(int(r.get("user_id") or 0)) or {}).get("fee90") or 0)
            sg = suggest_fee(int(r.get("deposit_fee") or 0), fee90)
            r["suggest_fee_yuan"] = sg["suggest_fee_yuan"]
            r["suggest_level"] = sg["level_name"]
        else:
            r["suggest_fee_yuan"] = None
            r["suggest_level"] = ""

        r["warn_text"] = _warn_text(r)
    return rows


def _warn_text(r: Dict[str, Any]) -> str:
    """押金预警列文案：快到期「剩 X 天押金到期」；已到期未扣成功「押金到期划扣失败，建议按 X 元划扣」。"""
    left = r.get("deposit_days_left")
    deduct = r.get("deduct_status")
    if left is None:
        return "无押金到期时间"
    if left <= 0:
        if deduct == "fail":
            fee = r.get("suggest_fee_yuan")
            return f"押金到期划扣失败，建议按 {fee if fee is not None else 0.0} 元划扣"
        over = f"（逾期 {abs(left)} 天）" if left < 0 else "（到期日=今日）"
        if deduct == "success":
            return f"押金已到期{over}，划扣成功"
        return f"押金已到期{over}，未生成划扣订单"
    if deduct == "fail":
        return f"剩 {left} 天押金到期（已识别划扣失败）"
    return f"剩 {left} 天押金到期"


# --------------------------------------------------------------------------- 失败归因（组合推导）

def _classify(r: Dict[str, Any], cycle: Dict[str, Any], pay: Dict[str, Any],
              order: Optional[Dict[str, Any]]) -> Any:
    cyc_status = str((cycle or {}).get("status") or "")
    unsign_reason = str((cycle or {}).get("unsign_reason") or "")
    pay_status = str((pay or {}).get("pay_status") or "")
    pay_way = str((order or {}).get("pay_way") or r.get("deposit_payway") or "")
    ag_status = str(r.get("status") or "")
    blob = " ".join([pay_status, cyc_status, unsign_reason, ag_status, pay_way]).lower()

    # 1) 代扣签约失效
    if cyc_status.upper() == "UNSIGN":
        ev = f"周期代扣签约状态=已解约"
        if unsign_reason:
            ev += f"（原因：{unsign_reason}）"
        return "sign_lost", ev
    for kw in ("解约", "失效", "unsign"):
        if kw in blob:
            return "sign_lost", f"命中关键词「{kw}」；代扣签约={cn.cn('cycle_status', cyc_status) or '—'}/{unsign_reason or '—'}"

    # 2) 卡状态 / 渠道异常
    if pay_status.upper() in ("PAYERROR", "CLOSED", "TRADE_CLOSED"):
        return "card_error", f"支付流水状态={cn.cn('pay_status', pay_status)}"
    for kw in ("卡状态", "银行卡", "card"):
        if kw in blob:
            return "card_error", f"命中关键词「{kw}」"

    # 3) 余额不足 / 扣款未成功
    bits: List[str] = []
    if pay_status:
        bits.append(f"支付流水状态={cn.cn('pay_status', pay_status)}")
    if pay_way:
        bits.append(f"扣款渠道={cn.cn('pay_way', pay_way)}")
    if order is not None and int(order.get("is_pay") or 0) == 0:
        bits.append("押金划扣订单未支付完成")
    if int(r.get("last_order_is_pay") or 0) == 0 and r.get("last_order_status"):
        bits.append(f"最新押金订单状态={cn.cn('deposit_order_status', r.get('last_order_status'))}")
    if bits:
        if ag_status in STOP_STATUS:
            return "balance", "；".join(bits) + f"；协议状态={cn.cn('agreement_status', ag_status)}"
        return "balance", "；".join(bits)

    # 4) 协议已停用
    if ag_status in STOP_STATUS:
        return "agreement_stop", f"协议状态={cn.cn('agreement_status', ag_status)}"

    # 5) 其他
    return "other", f"无支付流水/签约/回执证据，需人工核查（协议状态={cn.cn('agreement_status', ag_status)}）"


def _rule_name(code: str) -> str:
    for rule in _failure_rules():
        if rule.get("code") == code:
            return rule.get("name") or code
    return {"balance": "余额不足", "sign_lost": "代扣签约失效", "card_error": "卡状态异常",
            "agreement_stop": "协议已停用", "other": "其他"}.get(code, code)


# --------------------------------------------------------------------------- 展示层补全

def _back_fail_map(uids: List[int], days: int = 90) -> Dict[int, Dict[str, Any]]:
    """近 N 天「还电/扣款回执」失败记录（t_user_exchange_deposit_back_log，仅作辅助佐证）。

    注意：该表存量数据以历史「电池归还/入库失败」为主，与押金足额划扣失败并非强对应关系，
    因此仅在近 90 天内确有失败回执时作为证据补充展示，不参与归因主判。
    """
    clean = [int(x) for x in {int(i) for i in uids if i}]
    if not clean:
        return {}
    since = common.now_ms() - int(days) * MS_DAY
    out: Dict[int, Dict[str, Any]] = {}
    step = 2000
    for i in range(0, len(clean), step):
        batch = clean[i:i + step]
        ph = ",".join(["%s"] * len(batch))
        rows = db.query_dicts(
            f"""SELECT user_id, battery_sn, error_code, error_message, create_time
                FROM {db.biz('t_user_exchange_deposit_back_log')}
                WHERE is_del=0 AND back_status='fail' AND create_time >= %s AND user_id IN ({ph})""",
            [since] + batch, ttl=300,
        )
        for r in rows:
            uid = int(r["user_id"])
            prev = out.get(uid)
            if prev is None or int(r.get("create_time") or 0) > int(prev.get("create_time") or 0):
                out[uid] = r
    return out


def _enrich(rows: List[Dict[str, Any]], with_battery: bool = False) -> None:
    if not rows:
        return
    uids = [r.get("user_id") for r in rows]
    aids = [r.get("id") for r in rows]
    rent_ids = [r.get("user_rent_id") for r in rows]
    tags = common.user_tags(uids)
    cycles = common.cyclepay_map(aids)
    notes = common.reception_notes(aids)
    backs = _back_fail_map(uids)
    phones = common.user_phone_map(uids)
    rents = common.rent_info_map(rent_ids)
    lasts: Dict[int, Dict[str, Any]] = {}
    bmap: Dict[str, Dict[str, Any]] = {}
    if with_battery:
        lasts = dataset.last_exchange_for(uids)
        sns = [(lasts.get(int(u.get("user_id") or 0)) or {}).get("battery_sn") for u in rows]
        bmap = common.battery_map([s for s in sns if s])
    for r in rows:
        uid = int(r.get("user_id") or 0)
        aid = int(r.get("id") or 0)
        cyc = cycles.get(aid) or {}
        back = backs.get(uid) or {}
        r["tags"] = tags.get(uid, [])
        r["current_phone"] = phones.get(uid) or ""
        r["user_phone"] = r.get("user_phone") or ""
        r["cycle_status"] = r.get("cycle_status") or cyc.get("status") or ""
        r["cycle_status_cn"] = cn.cn("cycle_status", r["cycle_status"])
        r["cycle_sign_status"] = cyc.get("sign_status") or r.get("cycle_sign_status") or ""
        r["cycle_sign_status_cn"] = cn.cn("cycle_sign_status", r["cycle_sign_status"])
        r["cycle_unsign_reason"] = r.get("cycle_unsign_reason") or cyc.get("unsign_reason") or ""
        r["cycle_last_deduct"] = ms_to_str(cyc.get("last_deduct_time"))
        note = notes.get(aid) or {}
        r["note"] = note.get("detail") or ""
        r["note_time"] = note.get("time") or ""
        r["back_fail_code"] = back.get("error_code") or ""
        r["back_fail_msg"] = back.get("error_message") or ""
        r["back_fail_battery"] = back.get("battery_sn") or ""
        pkg = rents.get(int(r.get("user_rent_id") or 0)) or {}
        if pkg:
            r["package_name"] = pkg.get("package_name") or ""
            r["rent_card_status"] = pkg.get("card_status") or ""
            r["rent_card_status_cn"] = cn.cn("card_status", r.get("rent_card_status"))
            r["rent_card_expire"] = ms_to_str(pkg.get("expire_time"))
            r["package_text"] = (
                f"{r['package_name'] or '套餐未命名'}（{r['rent_card_status_cn']}"
                f"{'，到期 ' + r['rent_card_expire'] if r.get('rent_card_expire') else ''}）"
            )
        else:
            r["package_name"] = ""
            r["rent_card_status"] = ""
            r["rent_card_status_cn"] = EXP_NOT_SET
            r["rent_card_expire"] = ""
            r["package_text"] = "无租期套餐记录"
        if r["back_fail_msg"] and r.get("deduct_status") == "fail":
            extra = f"近90天还电回执：{r['back_fail_msg']}"
            r["failure_evidence"] = f"{r.get('failure_evidence')}；{extra}" if r.get("failure_evidence") else extra
        if not r.get("failure_code") and r.get("deduct_status") == "fail":
            code, evidence = _classify(r, cyc, {}, None)
            r["failure_code"] = code
            r["failure_name"] = _rule_name(code)
            r["failure_evidence"] = evidence
        r["failure_name"] = r.get("failure_name") or "—"
        if with_battery:
            last = lasts.get(uid) or {}
            sn = last.get("battery_sn") or ""
            bat = bmap.get(sn, {}) if sn else {}
            r["battery_sn"] = sn or EXP_NOT_SET
            r["battery_location"] = bat.get("last_location_address") or EXP_NOT_SET
            r["battery_status_cn"] = cn.cn("battery_status", bat.get("battery_status")) if bat else EXP_NOT_SET
            r["battery_online_cn"] = cn.cn("online_status", bat.get("online_status")) if bat else EXP_NOT_SET
            r["battery_power"] = bat.get("power")
            r["last_site"] = last.get("site_name") or EXP_NOT_SET


# --------------------------------------------------------------------------- 接口

def overview(f: Filters) -> Dict[str, Any]:
    rows = due_rows(f)
    stages: Dict[str, Dict[str, Any]] = {k: {"count": 0, "fee": 0} for k in
                                         ("T1", "T3", "T7", "T30", "overdue", "beyond", "unknown")}
    for r in rows:
        st = r["stage"]
        if st in stages:
            stages[st]["count"] += 1
            stages[st]["fee"] += int(r.get("deposit_fee") or 0)
    success = [r for r in rows if r["deduct_status"] == "success"]
    fail = [r for r in rows if r["deduct_status"] == "fail"]
    unknown = [r for r in rows if r["deduct_status"] == "unknown"]
    recover = [r for r in rows if r["deduct_status"] == "success" and r.get("had_unpaid_ever")]
    today_due = [r for r in rows
                 if r.get("deposit_days_left") == 0 and r["deduct_status"] != "success"]
    overdue_open = [r for r in rows
                    if (r.get("deposit_days_left") is not None and r["deposit_days_left"] < 0
                        and r["deduct_status"] != "success")]
    should_fee = sum(int(r.get("deposit_fee") or 0) for r in rows)

    def _fee(items):
        return sum(int(r.get("deposit_fee") or 0) for r in items)

    return {
        "kpi": {
            "should_count": len(rows),
            "should_fee": should_fee,
            "should_fee_yuan": round(should_fee / 100.0, 2),
            "today_due_count": len(today_due),
            "today_due_fee": _fee(today_due),
            "today_due_fee_yuan": round(_fee(today_due) / 100.0, 2),
            "success_count": len(success),
            "success_fee": _fee(success),
            "success_fee_yuan": round(_fee(success) / 100.0, 2),
            "fail_count": len(fail),
            "fail_fee": _fee(fail),
            "fail_fee_yuan": round(_fee(fail) / 100.0, 2),
            "fail_rate": round(len(fail) / len(rows) * 100, 2) if rows else 0.0,
            "unknown_count": len(unknown),
            "overdue_open_count": len(overdue_open),
            "overdue_open_fee_yuan": round(_fee(overdue_open) / 100.0, 2),
            "recover_count": len(recover),
            "recover_fee": _fee(recover),
            "recover_fee_yuan": round(_fee(recover) / 100.0, 2),
            "recover_rate": round(len(recover) / len(rows) * 100, 2) if rows else 0.0,
            "bind_based_count": sum(1 for r in rows if r.get("deposit_bind_source") == "押金绑定时间"),
            "fallback_count": sum(1 for r in rows if r.get("deposit_bind_source") != "押金绑定时间"),
        },
        "stages": stages,
        "window": {"window_days": 30, "now": ms_to_str(common.now_ms()),
                   "due_basis": "押金绑定时间 + 1 年（无绑定时间回退租期到期时间）"},
    }


KPI_CARDS = [
    ("should", "应划扣用户", "人", "T-30 窗口内押金到期（含已逾期）的押金在账协议数"),
    ("today_due", "今日应划扣用户", "人", "押金到期日=今天且尚未划扣成功的协议数（应划扣但未成功）"),
    ("fail", "划扣失败用户", "人", "押金订单未支付 / 周期代扣已解约 / 支付渠道异常 / 协议已停用"),
    ("success", "划扣成功用户", "人", "押金订单已支付或已完成（order_status=success/is_pay=1）"),
    ("overdue_open", "已逾期未成功", "人", "押金到期日已过且仍未划扣成功的协议数"),
    ("recover", "挽回用户", "人", "历史曾出现未支付押金订单、当前已完成划扣的协议"),
    ("fail_rate", "划扣失败率", "%", "划扣失败用户 / 应划扣用户"),
    ("should_fee", "涉及押金总额", "元", "应划扣用户的押金金额合计"),
]


def kpi_cards(f: Filters) -> List[Dict[str, Any]]:
    ov = overview(f)
    k = ov["kpi"]
    out: List[Dict[str, Any]] = []
    for key, label, unit, basis in KPI_CARDS:
        if key == "should":
            value = k["should_count"]
        elif key == "today_due":
            value = k["today_due_count"]
        elif key == "fail":
            value = k["fail_count"]
        elif key == "success":
            value = k["success_count"]
        elif key == "overdue_open":
            value = k["overdue_open_count"]
        elif key == "recover":
            value = k["recover_count"]
        elif key == "fail_rate":
            value = k["fail_rate"]
        elif key == "should_fee":
            value = k["should_fee_yuan"]
        else:
            value = 0
        out.append({"key": key, "label": label, "value": value, "unit": unit,
                    "drill": key, "basis": basis})
    return out


DRILL_LABELS = {
    "should": "应划扣用户（T-30 窗口内押金到期）",
    "today_due": "今日应划扣用户（今天押金到期且未划扣成功）",
    "fail": "划扣失败用户",
    "success": "划扣成功用户",
    "overdue_open": "已逾期未成功用户",
    "recover": "挽回用户",
    "unknown": "未生成划扣订单用户",
}


def _apply_drill(rows: List[Dict[str, Any]], drill: str) -> List[Dict[str, Any]]:
    if not drill or drill == "should":
        return rows
    if drill == "today_due":
        return [r for r in rows if r.get("deposit_days_left") == 0 and r["deduct_status"] != "success"]
    if drill == "overdue_open":
        return [r for r in rows
                if r.get("deposit_days_left") is not None and r["deposit_days_left"] < 0
                and r["deduct_status"] != "success"]
    if drill == "recover":
        return [r for r in rows if r["deduct_status"] == "success" and r.get("had_unpaid_ever")]
    if drill in ("fail", "success", "unknown"):
        return [r for r in rows if r["deduct_status"] == drill]
    if drill in ("T1", "T3", "T7", "T30", "overdue"):
        return [r for r in rows if r["stage"] == drill]
    return rows


def warn_list(f: Filters, stage: str = "", keyword: str = "", page: int = 1, size: int = 50,
              cap: int = 1000, drill: str = "") -> Dict[str, Any]:
    """押金明细：支持预警分层（stage）、KPI 下钻（drill）与关键词检索。"""
    rows = due_rows(f)
    rows = _apply_drill(rows, drill)
    if stage:
        rows = [r for r in rows if r["stage"] == stage]
    if keyword:
        kw = keyword.strip()
        rows = [r for r in rows
                if kw in (r.get("user_name") or "") or kw in (r.get("user_phone") or "")
                or kw in str(r.get("user_id") or "") or kw in str(r.get("id") or "")]
    rows.sort(key=lambda r: (r.get("deposit_expire_ms") or 0))
    page_data = common.paginate(rows, page, size, cap=cap)
    _enrich(page_data["rows"], with_battery=True)
    page_data["drill"] = {"key": drill or "should",
                          "label": DRILL_LABELS.get(drill or "should", DRILL_LABELS["should"] if not drill else "全部数据"),
                          "basis": "押金到期 = 押金绑定时间 + 1 年（无绑定时间回退租期到期时间）；"
                                   "仅统计押金在账且押金金额 > 0 的协议"}
    page_data["stage_counts"] = _stage_counts(rows)
    return page_data


def _stage_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {k: 0 for k in ("T1", "T3", "T7", "T30", "overdue", "beyond", "unknown")}
    for r in rows:
        st = r.get("stage") or "unknown"
        out[st] = out.get(st, 0) + 1
    return out


def failure_breakdown(f: Filters) -> Dict[str, Any]:
    rows = due_rows(f)
    fail = [r for r in rows if r["deduct_status"] == "fail"]
    _enrich(fail)
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in fail:
        code = r.get("failure_code") or "other"
        b = buckets.setdefault(code, {"code": code, "name": _rule_name(code), "count": 0, "fee": 0,
                                      "samples": []})
        b["count"] += 1
        b["fee"] += int(r.get("deposit_fee") or 0)
        if len(b["samples"]) < 5:
            b["samples"].append({
                "agreement_id": r.get("id"), "user_id": r.get("user_id"),
                "user_name": r.get("user_name"), "user_phone": r.get("user_phone"),
                "current_phone": r.get("current_phone"),
                "city": r.get("sys_city_name"), "days_left": r.get("deposit_days_left"),
                "stage_cn": r.get("stage_cn"),
                "deposit_fee_yuan": r.get("deposit_fee_yuan"),
                "pay_status_cn": r.get("pay_status_cn"),
                "cycle_status_cn": r.get("cycle_status_cn"),
                "unsign_reason": r.get("cycle_unsign_reason"),
                "evidence": r.get("failure_evidence"),
            })
    items = sorted(buckets.values(), key=lambda x: -x["count"])
    total = len(fail) or 1
    for it in items:
        it["ratio"] = round(it["count"] / total * 100, 2)
        it["fee_yuan"] = round(it["fee"] / 100.0, 2)
    return {"total_fail": len(fail), "items": items,
            "sources": ["t_user_exchange_deposit_package_order.order_status/is_pay/pay_way",
                        "t_user_cyclepay_sign_log.status/unsign_reason",
                        "t_pay_wechat_log.pay_status(exchangeDepositOrder)",
                        "t_user_exchange_deposit_back_log.error_code/error_message",
                        "t_exchange_agreement.status"]}


def suggest_list(f: Filters, page: int = 1, size: int = 50, cap: int = 1000) -> Dict[str, Any]:
    rows = due_rows(f)
    fail = [r for r in rows if r["deduct_status"] == "fail"]
    _enrich(fail)
    wins = dataset.order_windows()
    rule = _suggest_rule()
    levels = rule.get("levels", []) or []
    risk_kw = rule.get("risk_tag_keywords", []) or []
    for r in fail:
        uid = int(r.get("user_id") or 0)
        stat = wins.get(uid, {})
        fee90 = int(stat.get("fee90") or 0)
        n90 = int(stat.get("n90") or 0)
        tags = r.get("tags") or []
        risky = any(kw in t for kw in risk_kw for t in tags)
        level = {"name": "谨慎划扣", "max_ratio": 0.3}
        for lv in levels:
            if fee90 >= int(lv.get("min_consume_90d_fee", 0)):
                level = lv
                break
        ratio = float(level.get("max_ratio", 0.3))
        if risky:
            ratio = min(ratio, 0.3)
            level = {"name": "谨慎划扣（命中风险标签）", "max_ratio": ratio}
        r["consume_90d_fee"] = fee90
        r["exchange_90d"] = n90
        r["level_name"] = level.get("name")
        r["suggest_ratio"] = ratio
        r["suggest_fee"] = int(int(r.get("deposit_fee") or 0) * ratio)
        r["suggest_fee_yuan"] = round(r["suggest_fee"] / 100.0, 2)
        r["suggest_text"] = f"押金到期划扣失败，建议按 {r['suggest_fee_yuan']} 元划扣"
    fail.sort(key=lambda r: -int(r.get("deposit_fee") or 0))
    return common.paginate(fail, page, size, cap=cap)


def config_view() -> Dict[str, Any]:
    """界面展示用：当前生效口径与阈值。"""
    t = _thresholds()
    return {
        "warn_stages": t.get("warn_stages") or {},
        "warn_stage_options": cn.WARN_STAGE_OPTIONS,
        "failure_rules": _failure_rules(),
        "suggest_rule": _suggest_rule(),
        "unpaid_recent_days": UNPAID_RECENT_DAYS,
        "active_status": list(ACTIVE_STATUS),
        "due_basis": {
            "field": "t_exchange_agreement.deposit_bind_time",
            "text": "押金到期时间 = 押金绑定/创建时间 + 1 年（押金超过一年自动解绑）；"
                    "无绑定时间时回退租期到期时间 rent_expire_time",
            "year_ms": cn.YEAR_MS,
        },
        "kpi_basis": [{"key": k, "label": lb, "unit": u, "basis": b} for k, lb, u, b in KPI_CARDS],
    }


def suggest_fee(deposit_fee: int, consume_90d_fee: int, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """按阈值配置给出「建议可划扣最大金额」（押金板块与用户 360 共用）。"""
    rule = _suggest_rule()
    levels = rule.get("levels", []) or []
    risk_kw = rule.get("risk_tag_keywords", []) or []
    level = {"name": "谨慎划扣", "max_ratio": 0.3}
    for lv in levels:
        if consume_90d_fee >= int(lv.get("min_consume_90d_fee", 0)):
            level = lv
            break
    ratio = float(level.get("max_ratio", 0.3))
    name = level.get("name") or "谨慎划扣"
    if any(kw in t for kw in risk_kw for t in (tags or [])):
        ratio = min(ratio, float(rule.get("risk_max_ratio", 0.3)))
        name = "谨慎划扣（命中风险标签）"
    fee = int(int(deposit_fee or 0) * ratio)
    return {"level_name": name, "ratio": ratio, "suggest_fee": fee,
            "suggest_fee_yuan": round(fee / 100.0, 2)}
