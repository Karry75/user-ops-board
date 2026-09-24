"""全站中文化映射与押金到期口径计算（唯一出口，枚举取值均来自真实库盘点）。

真实枚举来源（temp/probe3.py 全库 GROUP BY 盘点，2026-09-24）：
  t_exchange_agreement.type: single / company
  t_exchange_agreement.status: stop / cancelled / working / paused / owe_rent / unsubscribing / wait_activate
  t_exchange_agreement.deposit_status: off / on
  t_exchange_agreement.deposit_payway: alipay_free / imprest / sys_free / unionpay_wechatmini /
      wechat / alipay_free_app / offline / wechat_app / system / money / company_deposit_guarantee
  t_user_exchange_deposit_package_order.order_status: refund / created / success
  t_user_cyclepay_sign_log.status: WAIT / UNSIGN / NORMAL；sign_status: off / on
  t_battery.battery_status: none / using / scrap；online_status: offline / online；
      oem_device_status: delivered / await_deliver
  t_user_exchange_rent.card_status: used / using / stop / refunded / unused / working
  t_pay_wechat_log.pay_status: SUCCESS / init / NOTPAY / WAIT_BUYER_PAY / TRADE_CLOSED
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MS_DAY = 86400000
YEAR_MS = 365 * MS_DAY  # 押金到期口径：押金绑定/创建时间 + 1 年（超过一年自动解绑）

AGREEMENT_TYPE = {
    "single": "个人协议",
    "company": "企业协议",
}

AGREEMENT_STATUS = {
    "working": "生效中",
    "owe_rent": "欠租中",
    "paused": "已暂停",
    "unsubscribing": "退订中",
    "wait_activate": "待激活",
    "stop": "已停用",
    "stopped": "已停用",
    "cancelled": "已取消",
    "canceled": "已取消",
}

DEPOSIT_STATUS = {
    "on": "押金在账",
    "off": "押金已退",
}

DEPOSIT_PAYWAY = {
    "alipay_free": "支付宝免押",
    "alipay_free_app": "支付宝免押(App)",
    "imprest": "预授权/备用金",
    "sys_free": "系统免押",
    "unionpay_wechatmini": "云闪付(小程序)",
    "wechat": "微信支付",
    "wechat_app": "微信支付(App)",
    "offline": "线下收款",
    "system": "系统代扣",
    "money": "现金/转账",
    "company_deposit_guarantee": "企业押金担保",
}

CONTRACT_TYPE = {
    "day": "按天签约",
    "year": "按年签约",
}

DEPOSIT_ORDER_STATUS = {
    "created": "已创建待支付",
    "success": "支付成功",
    "refund": "已退款",
    "closed": "已关闭",
    "cancel": "已取消",
    "fail": "支付失败",
}

DEDUCT_STATUS = {
    "success": "划扣成功",
    "fail": "划扣失败",
    "unknown": "未生成划扣订单",
}

CYCLE_STATUS = {
    "NORMAL": "代扣正常",
    "WAIT": "等待签约",
    "UNSIGN": "已解约",
}

CYCLE_SIGN_STATUS = {
    "on": "已签约",
    "off": "未签约",
}

PAY_STATUS = {
    "SUCCESS": "支付成功",
    "init": "待支付",
    "NOTPAY": "未支付",
    "WAIT_BUYER_PAY": "等待买家付款",
    "TRADE_CLOSED": "交易关闭",
    "PAYERROR": "支付失败",
}

BATTERY_STATUS = {
    "none": "在库/未使用",
    "using": "用户使用中",
    "scrap": "已报废",
}

ONLINE_STATUS = {
    "online": "在线",
    "offline": "离线",
}

OEM_DEVICE_STATUS = {
    "delivered": "已交付",
    "await_deliver": "待交付",
}

CARD_STATUS = {
    "used": "已使用",
    "using": "使用中",
    "stop": "已停用",
    "refunded": "已退款",
    "unused": "未使用",
    "working": "生效中",
}

PRESENT_STATUS = {
    "present": "已赠送",
    "init": "未赠送",
}

PAY_WAY = {
    "alipay_free": "支付宝免押",
    "alipay_free_app": "支付宝免押(App)",
    "imprest": "预授权/备用金",
    "sys_free": "系统免押",
    "unionpay_wechatmini": "云闪付(小程序)",
    "wechat": "微信支付",
    "wechat_app": "微信支付(App)",
    "offline": "线下收款",
}

BUSINESS_TYPE = {
    "exchangeDepositOrder": "押金订单",
    "exchangeOrder": "换电订单",
    "exchangeServiceOrder": "换电服务订单",
    "rentViolatedOrder": "租期违约订单",
    "CouponConvertCodeOrder": "优惠券兑换订单",
    "goodsBook": "商品预约单",
    "materielGoods": "物料商品单",
    "contractSchemeOrder": "合约方案订单",
    "EarningBalanceTopUPOrder": "收益余额充值单",
    "exchangePackage": "换电套餐订单",
}

SIGN_SITE_BUSINESS_TYPE = {
    "agencyEmployee": "网点员工代签",
    "agency": "网点代签",
}

PACKAGE_CATEGORY = {
    "house": "家用套餐",
    "rider": "骑手套餐",
}

PACKAGE_VALID_TYPE = {
    "day": "按天",
    "month": "按月",
    "year": "按年",
}

STAGE = {
    "T30": "T-30 内",
    "T7": "T-7 内",
    "T3": "T-3 内",
    "T1": "T-1 内",
    "overdue": "已逾期",
    "beyond": "未进入预警窗口",
    "unknown": "无到期时间",
}

WARN_STAGE_OPTIONS = [
    {"value": "", "label": "全部预警时间"},
    {"value": "T30", "label": "T-30 内到期"},
    {"value": "T7", "label": "T-7 内到期"},
    {"value": "T3", "label": "T-3 内到期"},
    {"value": "T1", "label": "T-1 内到期"},
    {"value": "overdue", "label": "已逾期"},
]

WARN_STAGE_DAYS = {"T30": 30, "T7": 7, "T3": 3, "T1": 1}

MAPS: Dict[str, Dict[str, str]] = {
    "agreement_type": AGREEMENT_TYPE,
    "agreement_status": AGREEMENT_STATUS,
    "deposit_status": DEPOSIT_STATUS,
    "deposit_payway": DEPOSIT_PAYWAY,
    "contract_type": CONTRACT_TYPE,
    "deposit_order_status": DEPOSIT_ORDER_STATUS,
    "deduct_status": DEDUCT_STATUS,
    "cycle_status": CYCLE_STATUS,
    "cycle_sign_status": CYCLE_SIGN_STATUS,
    "pay_status": PAY_STATUS,
    "battery_status": BATTERY_STATUS,
    "online_status": ONLINE_STATUS,
    "oem_device_status": OEM_DEVICE_STATUS,
    "card_status": CARD_STATUS,
    "present_status": PRESENT_STATUS,
    "pay_way": PAY_WAY,
    "business_type": BUSINESS_TYPE,
    "sign_site_business_type": SIGN_SITE_BUSINESS_TYPE,
    "package_category": PACKAGE_CATEGORY,
    "package_valid_type": PACKAGE_VALID_TYPE,
    "stage": STAGE,
}

EMPTY_TEXT = "未记录"


def _key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def cn(group: str, value: Any, default: str = "未知") -> str:
    """枚举值 → 中文。空值返回「未记录」，未命中映射返回 default。"""
    k = _key(value)
    if not k:
        return EMPTY_TEXT
    table = MAPS.get(group) or {}
    if k in table:
        return table[k]
    up = k.upper()
    for key, val in table.items():
        if key.upper() == up:
            return val
    return default


def deposit_expire_ms(bind_ms: Any, fallback_ms: Any) -> int:
    """押金到期时间：押金绑定时间 + 1 年；无绑定时间时回退租期到期时间。"""
    try:
        bind = int(bind_ms or 0)
    except Exception:
        bind = 0
    if bind > 0:
        return bind + YEAR_MS
    try:
        return int(fallback_ms or 0)
    except Exception:
        return 0


def stage_of(days_left: Optional[int]) -> str:
    if days_left is None:
        return "unknown"
    if days_left < 0:
        return "overdue"
    for key in ("T1", "T3", "T7", "T30"):
        if days_left <= WARN_STAGE_DAYS[key]:
            return key
    return "beyond"


def stage_cn(stage: Any) -> str:
    return STAGE.get(_key(stage), "未知")
