"""user_ops_board 独立 Flask 看板服务（端口默认 8095）。

布局：左侧固定菜单（押金板块 / 沉默低频·电池寻找 / 用户360明细 / 策略价值条），
每页统一「上方 KPI 卡片区（可点击下钻） + 下方明细表」两段式，筛选器置顶常驻。

只读访问 AnalyticDB 双库，严禁写入；所有数据来自真实库，无任何样本 / 兜底假数据。
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict

from flask import Flask, jsonify, render_template, request, send_file

from config import settings
from core import cn, common, dataset, deposit, silent, strategy, user360
from core.common import Filters
from core import export as export_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


def _filters() -> Filters:
    return Filters(
        city=request.args.get("city", ""),
        product=request.args.get("product", ""),
        oem=request.args.get("oem", ""),
        start_ms=int(request.args.get("start_ms") or 0),
        end_ms=int(request.args.get("end_ms") or 0),
        warn_stage=request.args.get("warn_stage", ""),
    )


def _int(name: str, default: int) -> int:
    try:
        return int(request.args.get(name) or default)
    except Exception:
        return default


def _kw() -> str:
    return request.args.get("keyword", "")


@app.errorhandler(Exception)
def _on_error(e):  # pragma: no cover
    traceback.print_exc()
    return jsonify({"ok": False, "error": str(e)[:400]}), 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "db": settings.db_configured(),
                    "biz": settings.DB_NAME, "base": settings.DB_BASE,
                    "port": settings.BOARD_PORT})


MENU = [
    {"key": "deposit", "label": "一、押金板块", "desc": "押金足额划扣预警 · 失败归因 · 建议划扣"},
    {"key": "silent", "label": "二、沉默低频·电池寻找", "desc": "沉默/低频分层 · 电池定位 · 动作队列"},
    {"key": "user360", "label": "三、用户360明细", "desc": "一行一份协议 · 全维度画像"},
    {"key": "strategy", "label": "四、策略价值条", "desc": "押金划扣漏斗 · 客服接待成效"},
]


@app.route("/api/meta")
def meta():
    th = settings.load_thresholds()
    return jsonify({
        "ok": True,
        "menu": MENU,
        "cities": common.city_options(),
        "products": common.product_options(),
        "oems": common.oem_options(),
        "warn_stages": cn.WARN_STAGE_OPTIONS,
        "thresholds": th,
        "datasource": {
            "engine": "AnalyticDB MySQL (pymysql, 只读)",
            "biz": settings.DB_NAME,
            "base": settings.DB_BASE,
            "readonly": True,
            "note": "全部指标均为库内真实数据，无样本 / 兜底假数据",
        },
        "filters": _filters().describe(),
    })


@app.route("/api/thresholds")
def thresholds():
    return jsonify({"ok": True, "thresholds": settings.load_thresholds()})


# --------------------------------------------------------------------------- 一、押金板块

@app.route("/api/deposit/config")
def api_deposit_config():
    return jsonify({"ok": True, "data": deposit.config_view()})


@app.route("/api/deposit/kpi")
def api_deposit_kpi():
    f = _filters()
    return jsonify({"ok": True, "data": {"cards": deposit.kpi_cards(f),
                                         "overview": deposit.overview(f),
                                         "filters": f.describe()}})


@app.route("/api/deposit/overview")
def api_deposit_overview():
    f = _filters()
    return jsonify({"ok": True, "data": deposit.overview(f), "filters": f.describe()})


@app.route("/api/deposit/warn")
def api_deposit_warn():
    f = _filters()
    data = deposit.warn_list(f, stage=request.args.get("stage", ""), keyword=_kw(),
                             page=_int("page", 1), size=_int("size", 50),
                             drill=request.args.get("drill", ""))
    return jsonify({"ok": True, "data": data})


@app.route("/api/deposit/drill")
def api_deposit_drill():
    f = _filters()
    data = deposit.warn_list(f, keyword=_kw(), page=_int("page", 1), size=_int("size", 50),
                             drill=request.args.get("drill", "should"))
    return jsonify({"ok": True, "data": data})


@app.route("/api/deposit/failure")
def api_deposit_failure():
    f = _filters()
    return jsonify({"ok": True, "data": deposit.failure_breakdown(f)})


@app.route("/api/deposit/suggest")
def api_deposit_suggest():
    f = _filters()
    data = deposit.suggest_list(f, page=_int("page", 1), size=_int("size", 50))
    return jsonify({"ok": True, "data": data})


# --------------------------------------------------------------------------- 二、沉默低频·电池寻找

@app.route("/api/silent/kpi")
def api_silent_kpi():
    f = _filters()
    return jsonify({"ok": True, "data": {"cards": silent.kpi_cards(f),
                                         "summary": silent.summary(f),
                                         "filters": f.describe()}})


@app.route("/api/silent/summary")
def api_silent_summary():
    f = _filters()
    return jsonify({"ok": True, "data": silent.summary(f)})


@app.route("/api/silent/leads")
def api_silent_leads():
    f = _filters()
    data = silent.drill_list(f, drill=request.args.get("drill", "silent"), keyword=_kw(),
                             level=request.args.get("level", ""),
                             page=_int("page", 1), size=_int("size", 50))
    return jsonify({"ok": True, "data": data})


@app.route("/api/silent/silent")
def api_silent_list():
    f = _filters()
    data = silent.leads_list(f, group="silent", page=_int("page", 1), size=_int("size", 50),
                             keyword=_kw())
    return jsonify({"ok": True, "data": data})


@app.route("/api/silent/lowfreq")
def api_silent_lowfreq():
    f = _filters()
    data = silent.leads_list(f, group="low_freq", level=request.args.get("level", ""),
                             page=_int("page", 1), size=_int("size", 50), keyword=_kw())
    return jsonify({"ok": True, "data": data})


@app.route("/api/silent/actions")
def api_silent_actions():
    f = _filters()
    data = silent.action_queue(f)
    return jsonify({"ok": True, "data": data})


# --------------------------------------------------------------------------- 三、用户360

@app.route("/api/user360/kpi")
def api_user360_kpi():
    f = _filters()
    return jsonify({"ok": True, "data": {"cards": user360.kpi_cards(f),
                                         "summary": user360.summary(f),
                                         "filters": f.describe()}})


@app.route("/api/user360")
def api_user360():
    f = _filters()
    data = user360.rows(f, keyword=_kw(), advice=request.args.get("advice", ""),
                        page=_int("page", 1), size=_int("size", 50),
                        drill=request.args.get("drill", ""))
    return jsonify({"ok": True, "data": data})


@app.route("/api/user360/drill")
def api_user360_drill():
    f = _filters()
    data = user360.drill_rows(f, drill=request.args.get("drill", "all"), keyword=_kw(),
                              page=_int("page", 1), size=_int("size", 50))
    return jsonify({"ok": True, "data": data})


# --------------------------------------------------------------------------- 四、策略价值条

@app.route("/api/strategy/kpi")
def api_strategy_kpi():
    f = _filters()
    return jsonify({"ok": True, "data": {"cards": strategy.kpi_cards(f, days=_int("days", 90)),
                                         "funnel": strategy.deposit_funnel(f),
                                         "filters": f.describe()}})


@app.route("/api/strategy")
def api_strategy():
    f = _filters()
    return jsonify({"ok": True, "data": {
        "funnel": strategy.deposit_funnel(f),
        "reception": strategy.reception_effect(f, days=_int("days", 90)),
    }})


@app.route("/api/strategy/drill")
def api_strategy_drill():
    f = _filters()
    data = strategy.drill_rows(f, drill=request.args.get("drill", "should"), keyword=_kw(),
                               page=_int("page", 1), size=_int("size", 50),
                               days=_int("days", 90))
    return jsonify({"ok": True, "data": data})


# --------------------------------------------------------------------------- 导出

@app.route("/api/export")
def api_export():
    f = _filters()
    kind = request.args.get("type", "user360")
    path = export_mod.export(kind, f, keyword=_kw(), drill=request.args.get("drill", ""))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


if __name__ == "__main__":
    print(f"[user_ops_board] 启动中，端口 {settings.BOARD_PORT}，DB={'OK' if settings.db_configured() else 'MISSING'}")
    app.run(host=settings.BOARD_HOST, port=settings.BOARD_PORT, debug=False, threaded=True)
