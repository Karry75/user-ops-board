/* user_ops_board 前端：左侧固定菜单 + 每页「KPI 卡片（可点击下钻） + 明细表」，全字段中文化。
   全部数据来自只读 AnalyticDB 真实库；接口失败时如实报错，不使用任何样本/兜底假数据。 */
(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  var ST = {
    page: 'deposit',
    drill: { deposit: 'should', silent: 'silent', user360: 'all', strategy: 'should' },
    pg: { deposit: 1, silent: 1, user360: 1, strategy: 1 },
    kw: { deposit: '', silent: '', user360: '', strategy: '' },
    level: '', advice: ''
  };

  /* 卡片配色 */
  var TONE = {
    today_due: 'warn', overdue_open: 'danger', fail: 'danger', fail_rate: 'danger',
    success: 'ok', recover: 'ok', recover_rate: 'ok', should_fee: 'warn',
    silent_count: 'warn', low_freq_count: 'warn', abnormal_count: 'danger',
    battery_in_hand: 'ok', action_out_of_range: 'danger',
    deposit_fail: 'danger', abnormal: 'danger', advice_huishou: 'danger'
  };

  /* 客服接待类型中文映射 */
  var RECEPTION_TYPE = {
    exchange_take_battery: '换电取电池', exchange_back_battery: '换电还电池',
    take_first_after_back: '还电后首次取电', back_validate: '还电核验',
    offline_verify: '线下核验', artificial_confirm_back: '人工确认还电',
    init_order: '初始下单', deposit: '押金业务', refund: '退款业务',
    phone: '电话接待', online: '线上接待', other: '其他'
  };

  /* ---------------- 渲染小工具 ---------------- */
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function blank(v) { return v === null || v === undefined || v === '' || v === '—' || v === '-'; }
  function dash(v) { return blank(v) ? '—' : esc(v); }
  function yuan(v) { if (blank(v)) return '—'; var n = Number(v); return isNaN(n) ? esc(v) : n.toFixed(2); }
  function num(v, d) { if (blank(v)) return '—'; var n = Number(v); return isNaN(n) ? esc(v) : n.toFixed(d === undefined ? 0 : d); }
  function daysText(v) {
    if (blank(v)) return '—';
    var n = Number(v); if (isNaN(n)) return esc(v);
    if (n > 0) return '剩 ' + n + ' 天';
    if (n === 0) return '今日到期';
    return '已逾期 ' + Math.abs(n) + ' 天';
  }
  function levelText(v) { return blank(v) ? '沉默' : esc(v); }
  function boolText(v) { return v ? '是' : '否'; }
  function typeCn(v) { return RECEPTION_TYPE[v] || (blank(v) ? '—' : esc(v)); }
  function tagList(v) {
    if (!v || !v.length) return '—';
    return v.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('');
  }
  function chip(text, cls) { return blank(text) ? '—' : '<span class="chip ' + (cls || 'gray') + '">' + esc(text) + '</span>'; }
  function deductCell(v) {
    if (blank(v)) return '—';
    var cls = v.indexOf('失败') >= 0 ? 'red' : (v.indexOf('成功') >= 0 ? 'green' : (v.indexOf('未') >= 0 ? 'gray' : 'orange'));
    return chip(v, cls);
  }
  function warnCell(r) {
    var t = r.warn_text || r.deposit_warn_text || r.deposit_warn || '';
    if (blank(t)) return '—';
    return chip(t, t.indexOf('失败') >= 0 ? 'red' : 'orange');
  }
  function stageCell(v) { return blank(v) ? '—' : chip(v, v.indexOf('逾期') >= 0 ? 'red' : 'orange'); }

  function renderTable(sel, cols, rows, emptyMsg) {
    var t = $(sel); if (!t) return;
    var head = '<thead><tr>' + cols.map(function (c) { return '<th>' + esc(c.t) + '</th>'; }).join('') + '</tr></thead>';
    var body;
    if (!rows || !rows.length) {
      body = '<tbody><tr><td colspan="' + cols.length + '" style="color:#8a94a6">' + esc(emptyMsg || '当前条件下暂无数据') + '</td></tr></tbody>';
    } else {
      body = '<tbody>' + rows.map(function (r) {
        return '<tr>' + cols.map(function (c) {
          var v = c.f ? c.f(r) : r[c.k];
          return '<td>' + (blank(v) ? '—' : v) + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody>';
    }
    t.innerHTML = head + body;
  }

  function renderPager(sel, data, go) {
    var el = $(sel); if (!el) return;
    var total = data.total || 0, page = data.page || 1, size = data.size || 50;
    var pages = Math.max(1, Math.ceil(total / (size || 50)));
    el.innerHTML = '共 ' + total + ' 条 · 第 ' + page + ' / ' + pages + ' 页 · 每页 ' + size +
      ' <button class="ghost" data-go="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
      ' <button class="ghost" data-go="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>下一页</button>';
    $$(sel + ' button[data-go]').forEach(function (b) {
      b.onclick = function () {
        var p = parseInt(b.getAttribute('data-go'), 10);
        if (p >= 1 && p <= pages) go(p);
      };
    });
  }

  function renderKpi(sel, cards, page, drillKey) {
    var el = $(sel); if (!el) return;
    if (!cards || !cards.length) { el.innerHTML = '<div class="kv">当前条件下暂无 KPI 数据</div>'; return; }
    el.innerHTML = cards.map(function (c) {
      var on = String(c.drill) === String(drillKey) ? ' on' : '';
      return '<div class="kpi ' + (TONE[c.key] || '') + on + '" data-drill="' + esc(c.drill) + '" title="' +
        esc(c.basis || '') + '"><div class="l">' + esc(c.label) + '</div><div class="v">' +
        (blank(c.value) ? '—' : esc(c.value)) + '<span class="u">' + esc(c.unit || '') + '</span></div>' +
        '<div class="d">' + (c.hint || '点击查看明细') + '</div></div>';
    }).join('');
    $$(sel + ' .kpi').forEach(function (d) {
      d.onclick = function () { goDrill(page, d.getAttribute('data-drill')); };
    });
  }

  function api(path, params) {
    var q = Object.keys(params || {}).filter(function (k) {
      return params[k] !== '' && params[k] !== null && params[k] !== undefined;
    }).map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); }).join('&');
    return fetch(path + (q ? '?' + q : ''), { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || j.ok === false) throw new Error((j && j.error) || '接口返回异常');
        return j.data === undefined ? j : j.data;
      });
  }

  function flt(extra) {
    var o = {
      city: ($('#fCity') || {}).value || '',
      product: ($('#fProduct') || {}).value || '',
      oem: ($('#fOem') || {}).value || '',
      warn_stage: ($('#fWarn') || {}).value || ''
    };
    Object.keys(extra || {}).forEach(function (k) {
      if (extra[k] !== '' && extra[k] !== null && extra[k] !== undefined) o[k] = extra[k];
    });
    return o;
  }

  function showErr(e) {
    var b = $('#errBar');
    b.classList.remove('hidden');
    b.innerHTML = '数据加载失败：' + esc(e && e.message ? e.message : e) +
      '<br>已如实报错，未使用任何样本或兜底假数据。请检查只读库连通性与服务日志，修复后点击「刷新」。';
  }
  function clearErr() { $('#errBar').classList.add('hidden'); }

  function drillLabel(sel, data) {
    var el = $(sel); if (!el || !data || !data.drill) return;
    el.textContent = (data.drill.label || '') + '（' + (data.total || 0) + ' 条）';
    if (data.drill.basis) el.title = data.drill.basis;
  }

  /* ---------------- 一、押金板块 ---------------- */
  var DEP_COLS = [
    { t: '用户协议ID', k: 'id' },
    { t: '用户ID', k: 'user_id' },
    { t: '用户姓名', k: 'user_name', f: function (r) { return dash(r.user_name); } },
    { t: '用户签约手机号', k: 'user_phone', f: function (r) { return dash(r.user_phone); } },
    { t: '用户当前手机号', k: 'current_phone', f: function (r) { return dash(r.current_phone); } },
    { t: '城市', k: 'sys_city_name' },
    { t: '电池产品', k: 'product_name' },
    { t: '押金金额(元)', f: function (r) { return yuan(r.deposit_fee_yuan); } },
    { t: '押金订单创建时间', k: 'deposit_order_create_str' },
    { t: '押金创建时间', k: 'deposit_bind_str' },
    { t: '押金到期时间', k: 'deposit_expire_str' },
    { t: '到期倒计时', f: function (r) { return daysText(r.deposit_days_left); } },
    { t: '预警层级', f: function (r) { return stageCell(r.stage_cn); } },
    { t: '押金预警', f: function (r) { return warnCell(r); } },
    { t: '划扣状态', f: function (r) { return deductCell(r.deduct_status_cn); } },
    { t: '押金划扣失败原因', f: function (r) { return dash(r.failure_name); } },
    { t: '失败证据', f: function (r) { return dash(r.failure_evidence); } },
    { t: '建议划扣(元)', f: function (r) { return yuan(r.suggest_fee_yuan); } },
    { t: '协议状态', k: 'agreement_status_cn' },
    { t: '押金状态', k: 'deposit_status_cn' },
    { t: '押金支付方式', k: 'deposit_payway_cn' },
    { t: '周期代扣状态', k: 'cycle_status_cn' },
    { t: '代扣解约原因', k: 'cycle_unsign_reason' },
    { t: '电池SN', k: 'battery_sn' },
    { t: '电池定位', k: 'battery_location' },
    { t: '电池状态', k: 'battery_status_cn' },
    { t: '电池在线', k: 'battery_online_cn' },
    { t: '套餐情况', k: 'package_text' },
    { t: '用户标签', f: function (r) { return tagList(r.tags); } },
    { t: '最近接待备注', k: 'note' },
    { t: '接待时间', k: 'note_time' }
  ];

  function loadDeposit() {
    var drill = ST.drill.deposit;
    Promise.all([
      api('/api/deposit/kpi', flt()),
      api('/api/deposit/warn', flt({ drill: drill, page: ST.pg.deposit, size: 50, keyword: ST.kw.deposit })),
      api('/api/deposit/failure', flt())
    ]).then(function (res) {
      clearErr();
      var kd = res[0], wd = res[1], fb = res[2], ov = kd.overview || {}, k = ov.kpi || {};
      renderKpi('#depKpi', kd.cards || [], 'deposit', drill);
      var sc = wd.stage_counts || {};
      $('#depFailChips').innerHTML =
        '失败归因：' + (((fb.items || []).map(function (i) {
          return '<span class="tag">' + esc(i.name) + ' ' + i.count + ' 人（' + i.ratio + '%，' + yuan(i.fee_yuan) + ' 元）</span>';
        }).join('')) || '—') +
        '<br>预警层级分布：T-1 ' + (sc.T1 || 0) + ' · T-3 ' + (sc.T3 || 0) + ' · T-7 ' + (sc.T7 || 0) +
        ' · T-30 ' + (sc.T30 || 0) + ' · 已逾期 ' + (sc.overdue || 0) + ' · 窗口外 ' + (sc.beyond || 0) +
        ' · 无到期时间 ' + (sc.unknown || 0) +
        '<br>其他口径：划扣成功 ' + (k.success_count || 0) + ' 人 · 未划扣成功 ' + (k.unknown_count || 0) +
        ' 人 · 已逾期未成功 ' + (k.overdue_open_count || 0) + ' 人 · 挽回 ' + (k.recover_count || 0) + ' 人';
      $('#depBasis').innerHTML = '口径：押金到期时间 = 押金绑定/创建时间 + 1 年（押金超过一年自动解绑）；' +
        '本次命中「押金绑定时间」' + (k.bind_based_count || 0) + ' 人、回退「租期到期时间」' + (k.fallback_count || 0) +
        ' 人。<br>数据窗口：' + esc(((ov.window || {}).due_basis) || '') + '；统计时间 ' + esc(((ov.window || {}).now) || '');
      renderTable('#depTable', DEP_COLS, wd.rows, '当前下钻口径下暂无数据');
      renderPager('#depPager', wd, function (p) { ST.pg.deposit = p; loadDeposit(); });
      drillLabel('#depDrillLabel', wd);
      $('#depBasis2').innerHTML = '下钻口径：' + esc((wd.drill || {}).basis || '') +
        '；导出 Excel 为当前下钻口径的全部数据（同样取自真实库）。';
    }).catch(showErr);
  }

  function depExport() {
    var p = flt({ type: 'deposit_warn', drill: ST.drill.deposit, keyword: ST.kw.deposit });
    location.href = '/api/export?' + Object.keys(p).map(function (k) { return k + '=' + encodeURIComponent(p[k]); }).join('&');
  }

  /* ---------------- 二、沉默低频 · 电池寻找 ---------------- */
  var SIL_COLS = [
    { t: '用户协议ID', k: 'id' },
    { t: '用户ID', k: 'user_id' },
    { t: '用户姓名', k: 'user_name', f: function (r) { return dash(r.user_name); } },
    { t: '用户签约手机号', k: 'user_phone', f: function (r) { return dash(r.user_phone); } },
    { t: '用户当前手机号', k: 'current_phone', f: function (r) { return dash(r.current_phone); } },
    { t: '城市', k: 'sys_city_name' },
    { t: '电池产品', k: 'product_name' },
    { t: '分层', f: function (r) { return levelText(r.level); } },
    { t: '协议状态', k: 'agreement_status_cn' },
    { t: '激活时间', k: 'activation_str' },
    { t: '租期到期时间', k: 'rent_expire_str' },
    { t: '租期是否超期', f: function (r) { return boolText(r.rent_overdue); } },
    { t: '沉默/低频天数', f: function (r) { return daysText(r.days_since_last_exchange); } },
    { t: '近15天换电', f: function (r) { return num(r.n15); } },
    { t: '近30天换电', f: function (r) { return num(r.n30); } },
    { t: '近60天换电', f: function (r) { return num(r.n60); } },
    { t: '近90天换电', f: function (r) { return num(r.n90); } },
    { t: '月均换电(次/月)', f: function (r) { return num(r.monthly_freq, 2); } },
    { t: '押金状态', k: 'deposit_status_cn' },
    { t: '押金到期时间', k: 'deposit_expire_str' },
    { t: '押金预警', f: function (r) { return warnCell(r); } },
    { t: '划扣状态', f: function (r) { return deductCell(r.deduct_status_cn); } },
    { t: '押金划扣失败原因', f: function (r) { return dash(r.failure_name); } },
    { t: '套餐情况', k: 'package_text' },
    { t: '持有电池SN', k: 'battery_sn' },
    { t: '电池状态', k: 'battery_status_cn' },
    { t: '电池在线', k: 'online_status_cn' },
    { t: '电量(%)', f: function (r) { return num(r.power); } },
    { t: '电池最后定位', k: 'last_location_address' },
    { t: '最后换电网点', k: 'last_site' },
    { t: '最后换电时间', k: 'last_take_time_str' },
    { t: '动作建议', f: function (r) { return dash(r.action); } },
    { t: '线索/异常说明', f: function (r) { return dash(r.action_reason || r.abnormal_reason || r.out_of_range_reason); } }
  ];

  function loadSilent() {
    var drill = ST.drill.silent;
    Promise.all([
      api('/api/silent/kpi', flt()),
      api('/api/silent/leads', flt({ drill: drill, page: ST.pg.silent, size: 50, keyword: ST.kw.silent, level: ST.level }))
    ]).then(function (res) {
      clearErr();
      var kd = res[0], ld = res[1];
      renderKpi('#silKpi', kd.cards || [], 'silent', drill);
      var su = kd.summary || {}, lv = su.levels || su.level_counts || {};
      var lvHtml = Object.keys(lv).map(function (n) { return '<span class="tag">' + esc(n) + ' ' + lv[n] + ' 人</span>'; }).join('');
      $('#silActionChips').innerHTML = '分层分布：' + (lvHtml || '—') +
        '<br>动作队列已并入上方 KPI，点击「劝退动作 / 引导换电动作 / 超范围预警」卡片即可查看对应队列明细。';
      $('#silBasis').innerHTML = '口径：沉默=近 90 天 0 次换电且租期到期或欠租中；低频=近 60 天换电次数落入 L1-L5 分层；' +
        '动作队列与电池定位均取自最后换电记录与电池最新状态（真实库）。';
      renderTable('#silTable', SIL_COLS, ld.rows, '当前下钻口径下暂无数据');
      renderPager('#silPager', ld, function (p) { ST.pg.silent = p; loadSilent(); });
      drillLabel('#silDrillLabel', ld);
      $('#silBasis2').innerHTML = '下钻口径：' + esc((ld.drill || {}).basis || '');
    }).catch(showErr);
  }

  function silExport() {
    var p = flt({ type: ST.drill.silent === 'low_freq' || String(ST.drill.silent).indexOf('low_L') === 0 ? 'lowfreq' : 'silent',
      drill: ST.drill.silent, keyword: ST.kw.silent, level: ST.level });
    location.href = '/api/export?' + Object.keys(p).map(function (k) { return k + '=' + encodeURIComponent(p[k]); }).join('&');
  }

  /* ---------------- 三、用户 360 明细 ---------------- */
  var U360_COLS = [
    { t: '用户协议ID', k: 'agreement_id' },
    { t: '用户ID', k: 'user_id' },
    { t: '用户姓名', k: 'user_name', f: function (r) { return dash(r.user_name); } },
    { t: '用户签约手机号', k: 'user_phone', f: function (r) { return dash(r.user_phone); } },
    { t: '用户当前手机号', k: 'current_phone', f: function (r) { return dash(r.current_phone); } },
    { t: '城市', k: 'city' },
    { t: '电池产品', k: 'product' },
    { t: '协议类型', k: 'agreement_type_cn' },
    { t: '协议状态', k: 'agreement_status_cn' },
    { t: '激活时间', k: 'activation_time' },
    { t: '租期到期时间', k: 'rent_expire_time' },
    { t: '套餐情况', k: 'package_name' },
    { t: '押金金额(元)', f: function (r) { return yuan(r.deposit_fee_yuan); } },
    { t: '押金状态', k: 'deposit_status_cn' },
    { t: '押金创建时间', k: 'deposit_bind_str' },
    { t: '押金到期时间', k: 'deposit_expire_str' },
    { t: '押金预警', f: function (r) { return warnCell(r); } },
    { t: '划扣状态', f: function (r) { return deductCell(r.deduct_status_cn); } },
    { t: '押金划扣失败原因', f: function (r) { return dash(r.failure_name); } },
    { t: '建议划扣(元)', f: function (r) { return yuan(r.suggest_fee_yuan); } },
    { t: '近30天换电', f: function (r) { return num(r.n30); } },
    { t: '近90天换电', f: function (r) { return num(r.n90); } },
    { t: '月均换电(次/月)', f: function (r) { return num(r.monthly_freq, 2); } },
    { t: '近30天消费(元)', f: function (r) { return yuan(r.fee30_yuan); } },
    { t: '近90天消费(元)', f: function (r) { return yuan(r.fee90_yuan); } },
    { t: '持有电池SN', k: 'battery_sn' },
    { t: '电池状态', k: 'battery_status_cn' },
    { t: '电池在线', k: 'battery_online_cn' },
    { t: '电池定位', k: 'battery_location' },
    { t: '最后换电网点', k: 'last_site' },
    { t: '最后换电时间', k: 'last_take_time' },
    { t: '距上次换电天数', f: function (r) { return daysText(r.days_since_last_exchange); } },
    { t: '用户标签', f: function (r) { return tagList(r.tags); } },
    { t: '综合处置建议', f: function (r) { return dash(r.advice); } },
    { t: '最近接待备注', k: 'note' },
    { t: '接待时间', k: 'note_time' }
  ];

  function loadU360() {
    var drill = ST.drill.user360;
    var p = flt({ drill: drill, page: ST.pg.user360, size: 50, keyword: ST.kw.user360 });
    if (ST.advice) p.advice = ST.advice;
    Promise.all([
      api('/api/user360/kpi', flt()),
      api('/api/user360', p)
    ]).then(function (res) {
      clearErr();
      var kd = res[0], dd = res[1];
      renderKpi('#u360Kpi', kd.cards || [], 'user360', drill);
      $('#u360Basis').innerHTML = '口径：一行一份生效中协议；押金到期 = 押金绑定时间 + 1 年（无绑定时间回退租期到期时间）；' +
        '押金预警与划扣状态复用押金板块同一口径；用户当前手机号取 t_user.phone，签约号取协议 user_phone。';
      renderTable('#u360Table', U360_COLS, dd.rows, '当前下钻口径下暂无数据');
      renderPager('#u360Pager', dd, function (p) { ST.pg.user360 = p; loadU360(); });
      drillLabel('#u360DrillLabel', dd);
      $('#u360Basis2').innerHTML = '下钻口径：' + esc((dd.drill || {}).basis || '');
    }).catch(showErr);
  }

  function u360Export() {
    var p = flt({ type: 'user360', drill: ST.drill.user360, keyword: ST.kw.user360 });
    location.href = '/api/export?' + Object.keys(p).map(function (k) { return k + '=' + encodeURIComponent(p[k]); }).join('&');
  }

  /* ---------------- 四、策略价值条 ---------------- */
  var REC_COLS = [
    { t: '用户协议ID', k: 'agreement_id' },
    { t: '用户ID', k: 'user_id' },
    { t: '用户签约手机号', k: 'user_phone', f: function (r) { return dash(r.user_phone); } },
    { t: '用户当前手机号', k: 'current_phone', f: function (r) { return dash(r.current_phone); } },
    { t: '城市', k: 'city' },
    { t: '接待类型', f: function (r) { return typeCn(r.type); } },
    { t: '接待内容', f: function (r) { return dash(r.detail); } },
    { t: '接待时间', k: 'time' },
    { t: '归类', f: function (r) { return dash(r.kind); } }
  ];

  function loadStrategy() {
    var drill = ST.drill.strategy;
    Promise.all([
      api('/api/strategy/kpi', flt()),
      api('/api/strategy', flt()),
      api('/api/strategy/drill', flt({ drill: drill, page: ST.pg.strategy, size: 50, keyword: ST.kw.strategy }))
    ]).then(function (res) {
      clearErr();
      var kd = res[0], sd = res[1], dd = res[2];
      var cards = kd.cards || [];
      var funnelKeys = { should: 1, success: 1, fail: 1, recover: 1, fail_rate: 1, recover_rate: 1 };
      renderKpi('#strKpi', cards.filter(function (c) { return funnelKeys[c.key]; }), 'strategy', drill);
      renderKpi('#strRecKpi', cards.filter(function (c) { return !funnelKeys[c.key]; }), 'strategy', drill);
      var items = (kd.funnel || {}).items || [];
      $('#strBasis').innerHTML = '环节人数与金额：' + items.map(function (i) {
        return '<span class="tag">' + esc(i.label) + ' ' + i.count + ' 人 / ' + yuan(i.fee_yuan) + ' 元</span>';
      }).join('') + '<br>口径：' + esc((kd.funnel || {}).basis || '');
      $('#strReceptionBasis').innerHTML = '口径：' + esc(((sd.reception || {}).basis) || '');
      var isRec = String(drill).indexOf('reception') === 0;
      renderTable('#strTable', isRec ? REC_COLS : DEP_COLS, dd.rows, '当前下钻口径下暂无数据');
      renderPager('#strPager', dd, function (p) { ST.pg.strategy = p; loadStrategy(); });
      drillLabel('#strDrillLabel', dd);
      $('#strExport').disabled = isRec;
      $('#strExport').textContent = isRec ? '接待记录暂不支持导出' : '导出 Excel';
      $('#strBasis2').innerHTML = '下钻口径：' + esc((dd.drill || {}).basis || '') + (isRec ? '' : '（明细列入同押金板块）');
    }).catch(showErr);
  }

  function strExport() {
    if (String(ST.drill.strategy).indexOf('reception') === 0) return;
    var p = flt({ type: 'deposit_warn', drill: ST.drill.strategy, keyword: ST.kw.strategy });
    location.href = '/api/export?' + Object.keys(p).map(function (k) { return k + '=' + encodeURIComponent(p[k]); }).join('&');
  }

  /* ---------------- 页面切换 / 下钻 ---------------- */
  var LOADERS = { deposit: loadDeposit, silent: loadSilent, user360: loadU360, strategy: loadStrategy };
  var RESET = { deposit: 'should', silent: 'silent', user360: 'all', strategy: 'should' };

  function navigate(page) {
    ST.page = page;
    $$('.side .nav').forEach(function (b) {
      b.className = 'nav' + (b.getAttribute('data-page') === page ? ' on' : '');
    });
    ['deposit', 'silent', 'user360', 'strategy'].forEach(function (p) {
      var sec = document.getElementById('page-' + p);
      if (sec) sec.className = p === page ? '' : 'hidden';
    });
    LOADERS[page]();
  }

  function goDrill(page, key) {
    if (!key) return;
    ST.drill[page] = key;
    ST.pg[page] = 1;
    if (page !== ST.page) { navigate(page); return; }
    LOADERS[page]();
  }

  function bindPage(prefix, page, exportFn) {
    var search = document.getElementById(prefix + 'Search');
    var kw = document.getElementById(prefix + 'Kw');
    if (search) search.onclick = function () { ST.kw[page] = kw.value.trim(); ST.pg[page] = 1; LOADERS[page](); };
    if (kw) kw.addEventListener('keydown', function (e) { if (e.key === 'Enter') search.click(); });
    var ex = document.getElementById(prefix + 'Export');
    if (ex) ex.onclick = exportFn;
    var rs = document.getElementById(prefix + 'Reset');
    if (rs) rs.onclick = function () { goDrill(page, RESET[page]); };
  }

  function loadMeta() {
    api('/api/meta', {}).then(function (m) {
      var ds = m.datasource || {};
      $('#dsInfo').textContent = '数据源：' + (ds.engine || '') + ' · 业务库 ' + (ds.biz || '') +
        ' + 基础库 ' + (ds.base || '') + ' · ' + (ds.note || '');
      fill('#fCity', m.cities, '全部城市');
      fill('#fProduct', m.products, '全部电池产品');
      fill('#fOem', m.oems, '全部 OEM');
      var th = m.thresholds || {};
      $('#thBody').textContent = JSON.stringify({ 押金预警分层: th.warn_stages || {}, 失败归因规则: th.failure_rules || {},
        建议划扣规则: th.suggest_rule || {}, 沉默低频: th.silent_low_freq || {}, 策略价值条: th.strategy_value || {} }, null, 2);
      var w = m.warn_stages || [];
      if (w.length) {
        var sel = $('#fWarn');
        sel.innerHTML = '<option value="">全部押金划扣预警时间</option>' + w.map(function (o) {
          var v = o.value || o.key || '', t = o.label || o.name || v;
          return '<option value="' + esc(v) + '">' + esc(t) + '</option>';
        }).join('');
      }
      var fdesc = m.filters || {};
      $('#filterDesc').textContent = '当前筛选：' + Object.keys(fdesc).map(function (k) {
        return k + '=' + fdesc[k];
      }).join(' · ');
    }).catch(showErr);
  }

  function fill(sel, items, placeholder) {
    var el = $(sel); if (!el || !items) return;
    el.innerHTML = '<option value="">' + esc(placeholder) + '</option>' + items.map(function (o) {
      var v = typeof o === 'object' ? (o.value || o.key || o.name || '') : o;
      var t = typeof o === 'object' ? (o.label || o.name || v) : o;
      return '<option value="' + esc(v) + '">' + esc(t) + '</option>';
    }).join('');
  }

  /* ---------------- 启动 ---------------- */
  $$('.side .nav').forEach(function (b) {
    b.onclick = function () { navigate(b.getAttribute('data-page')); };
  });
  bindPage('dep', 'deposit', depExport);
  bindPage('sil', 'silent', silExport);
  bindPage('u360', 'user360', u360Export);
  bindPage('str', 'strategy', strExport);
  $('#silLevel').onchange = function () { ST.level = this.value; ST.pg.silent = 1; loadSilent(); };
  $('#u360Advice').onchange = function () {
    ST.advice = this.value;
    ST.pg.user360 = 1;
    loadU360();
  };
  $('#btnRefresh').onclick = function () {
    ['deposit', 'silent', 'user360', 'strategy'].forEach(function (p) { ST.pg[p] = 1; });
    loadMeta(); navigate(ST.page);
  };
  $('#btnThresholds').onclick = function () {
    var el = $('#panelThresholds');
    el.className = el.className.indexOf('hidden') >= 0 ? 'card' : 'card hidden';
  };
  ['fCity', 'fProduct', 'fOem', 'fWarn'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.onchange = function () {
      ['deposit', 'silent', 'user360', 'strategy'].forEach(function (p) { ST.pg[p] = 1; });
      loadMeta(); navigate(ST.page);
    };
  });

  loadMeta();
  navigate('deposit');
})();
