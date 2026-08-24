/* ===== AI-QFund 管理域：模态框 / 基金详情 / 调度设置 / 管线状态卡 / 清空与密码 =====
   候选6 拆分（原 app.js 管理区段，非 module：全局函数互见，行为不变）。*/
var _modalCloseTimers = {};

function openModal(id) {
    var el = document.getElementById(id);
    if (!el) return;
    if (_modalCloseTimers[id]) { clearTimeout(_modalCloseTimers[id]); _modalCloseTimers[id] = null; }
    el.classList.remove('modal-closing');
    el.classList.remove('hidden');
    if (id === 'systemLogModal') {
        if (logInterval) { clearInterval(logInterval); logInterval = null; }
        _logCount = 0;
        _lastId = 0;
        _minId = 0;
        _noMoreLogs = false;
        document.getElementById('logLines').innerHTML = '';
        resetLogFilter();
        fetchLogs();
        logInterval = setInterval(fetchLogs, 5000);
        var container = document.getElementById('logContainer');
        container.addEventListener('scroll', function() {
            var btn = document.getElementById('logScrollBtn');
            if (!btn) return;
            var atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
            if (atBottom) btn.classList.add('hidden');
            else btn.classList.remove('hidden');
            // 滚动到顶部附近：自动加载更早日志
            if (container.scrollTop < 40) loadOlderLogs();
        });
    }
    if (id === 'settingsModal') {
        loadSettings();
    }
}

document.addEventListener('click', function(e) {
    if (e.target.classList.contains('log-filter')) {
        document.querySelectorAll('.log-filter').forEach(function(b) { b.classList.remove('active'); b.style.background = ''; });
        e.target.classList.add('active');
        e.target.style.background = 'var(--color-surface-tint)';
        applyLogFilter();
    }
});

function closeModal(id) {
    var el = document.getElementById(id);
    if (!el) return;
    if (id === 'systemLogModal' && logInterval) {
        clearInterval(logInterval);
        logInterval = null;
    }
    // 关闭快讯弹窗后恢复轮播推进（滚动模式继续滚动 / 短摘要重启停留计时）
    if (id === 'newsModal') {
        _newsPaused = false;
        var inner = _newsSummaryEl();
        if (inner) {
            if (_newsScrolling) inner.style.animationPlayState = 'running';
            else scheduleNewsAdvance();
        }
    }
    el.classList.add('modal-closing');
    _modalCloseTimers[id] = setTimeout(function() {
        el.classList.add('hidden');
        el.classList.remove('modal-closing');
        _modalCloseTimers[id] = null;
    }, 230);
}
function openFundDetail(code) {
    var modal = document.getElementById('fundDetailModal');
    var panel = document.getElementById('fundDetailPanel');
    if (_modalCloseTimers['fundDetailModal']) { clearTimeout(_modalCloseTimers['fundDetailModal']); _modalCloseTimers['fundDetailModal'] = null; }
    modal.classList.remove('modal-closing');
    modal.classList.remove('hidden');
    setTimeout(function() { panel.classList.remove('translate-x-full'); }, 10);
    document.getElementById('fundDetailTitle').textContent = code + ' 详情';
    document.getElementById('fundDetailContent').innerHTML = FUND_SKEL;
    fetchFundDetail(code);
}
function closeFundDetail() {
    var panel = document.getElementById('fundDetailPanel');
    panel.classList.add('translate-x-full');
    setTimeout(function() { document.getElementById('fundDetailModal').classList.add('hidden'); }, 300);
}
async function fetchFundDetail(code) {
    var container = document.getElementById('fundDetailContent');
    try {
        var r = await fetch('/api/fund-detail/' + encodeURIComponent(code));
        var d = await r.json();
        if (d.error) { container.innerHTML = '<p class="text-error font-bold">' + _esc(d.error) + '</p>'; return; }
        var f = d.fund;

        var holdingsHtml = '';
        if (d.top_holdings && d.top_holdings.length) {
            holdingsHtml = '<div class="space-y-2">';
            for (var i = 0; i < d.top_holdings.length; i++) {
                var h = d.top_holdings[i];
                var pct = (h.weight || 0).toFixed(2);
                holdingsHtml += '<div class="flex items-center gap-2 text-[13px] font-bold">' +
                    '<span class="w-8 text-right font-data-md text-[13px] text-on-surface-variant">#' + (i+1) + '</span>' +
                    '<span class="flex-1 truncate">' + _esc(h.stock_name) + '</span>' +
                    '<span class="text-[11px] text-on-surface-variant bg-surface px-2 py-0.5 rounded-md border border-outline">' + _esc(h.industry || '--') + '</span>' +
                    '<span class="w-16 text-right font-data-md">' + pct + '%</span>' +
                    '</div>';
            }
            holdingsHtml += '</div>';
        } else {
            holdingsHtml = '<p class="text-on-surface-variant text-[13px]">暂无持仓数据</p>';
        }

        var navChartHtml = '';
        if (d.nav_data && d.nav_data.length >= 2) {
            var navs = d.nav_data.map(function(x) { return x.nav; }).filter(function(v) { return v !== null; });
            if (navs.length >= 2) {
                var nMin = Math.min.apply(null, navs);
                var nMax = Math.max.apply(null, navs);
                var nRange = nMax - nMin || 1;
                var pad = nRange * 0.05;
                nMin -= pad;
                nMax += pad;
                nRange = nMax - nMin;
                var pts = navs.map(function(v, i) {
                    var x = (i / (navs.length - 1)) * 100;
                    var y = 100 - ((v - nMin) / nRange) * 100;
                    return x + ',' + y;
                }).join(' ');
                navChartHtml = '<div class="relative h-[200px] bg-surface rounded-md border border-outline p-2">' +
                    '<svg class="w-full h-full" viewBox="0 0 100 100" preserveAspectRatio="none">' +
                    '<polyline points="' + pts + '" fill="none" stroke="var(--color-accent)" stroke-width="0.5" vector-effect="non-scaling-stroke"/>' +
                    '</svg>' +
                    '<div class="flex justify-between text-[11px] font-bold text-on-surface-variant mt-1">' +
                    '<span>' + _esc(d.nav_data[0].date) + '</span>' +
                    '<span>' + _esc(d.nav_data[d.nav_data.length-1].date) + '</span>' +
                    '</div>' +
                    '</div>';
            } else {
                navChartHtml = '<p class="text-on-surface-variant text-[13px]">净值数据不足，无法绘制走势图</p>';
            }
        } else {
            navChartHtml = '<p class="text-on-surface-variant text-[13px]">暂无净值走势数据</p>';
        }

        // 推荐当日预测超额（与首页「预测超额（1月）」同源：recommend_log.score = 模型预测的未来 20 日收益分）
        var predAlphaHtml = '';
        if (f.score !== null && f.score !== undefined && f.score !== '') {
            var pa = parseFloat(f.score) * 100;
            var paCls = pa >= 0 ? 'text-up' : 'text-down';
            predAlphaHtml = '<div class="border-b border-outline pb-3">' +
                '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">推荐当日预测超额</h4>' +
                '<div class="flex items-baseline gap-2">' +
                '<span class="text-[24px] font-data-md font-bold leading-none ' + paCls + '">' + (pa >= 0 ? '+' : '') + pa.toFixed(1) + '%</span>' +
                '<span class="text-[11px] text-on-surface-variant font-bold">(1月)</span>' +
                '</div>' +
                '</div>';
        }

        // 多周期涨跌幅（近1周/1月/3月/6月 + 同期沪深300）
        var periodLabels = ['1周', '1月', '3月', '6月'];
        var periodRowsHtml = '';
        var hasPeriod = false;
        for (var pi = 0; pi < periodLabels.length; pi++) {
            var lbl = periodLabels[pi];
            var fv = d.period_returns ? d.period_returns[lbl] : null;
            var hv = d.period_returns ? d.period_returns[lbl + '_hs'] : null;
            if (fv !== null && fv !== undefined && fv !== '') { hasPeriod = true; }
            var fStr = (fv === null || fv === undefined || fv === '') ? '--' : ((fv >= 0 ? '+' : '') + fv.toFixed(2) + '%');
            var hStr = (hv === null || hv === undefined || hv === '') ? '--' : ((hv >= 0 ? '+' : '') + hv.toFixed(2) + '%');
            var fCls = (fv === null || fv === undefined || fv === '') ? 'text-on-surface-variant' : (fv >= 0 ? 'text-up' : 'text-down');
            var hCls = (hv === null || hv === undefined || hv === '') ? 'text-on-surface-variant' : (hv >= 0 ? 'text-up' : 'text-down');
            periodRowsHtml += '<tr class="border-b border-outline last:border-b-0">' +
                '<td class="py-2 text-[12px] font-bold text-on-surface-variant">' + lbl + '</td>' +
                '<td class="py-2 text-right text-[13px] font-data-md font-bold ' + fCls + '">' + fStr + '</td>' +
                '<td class="py-2 text-right text-[12px] font-data-md ' + hCls + '">' + hStr + '</td>' +
                '</tr>';
        }
        var periodHtml = '';
        if (hasPeriod) {
            periodHtml = '<div class="border-b border-outline pb-3">' +
                '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">多周期涨跌幅</h4>' +
                '<table class="w-full">' +
                '<thead><tr class="text-[11px] uppercase tracking-widest text-on-surface-variant">' +
                '<th class="py-1 text-left font-bold">周期</th>' +
                '<th class="py-1 text-right font-bold">基金</th>' +
                '<th class="py-1 text-right font-bold">沪深300</th>' +
                '</tr></thead>' +
                '<tbody>' + periodRowsHtml + '</tbody>' +
                '</table>' +
                '</div>';
        }

        container.innerHTML =
            '<div class="space-y-4">' +
            '<div class="border-b border-outline pb-3">' +
            '<h3 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-1">' + _esc(f.name) + '</h3>' +
            '<p class="text-[11px] text-on-surface-variant">' + _esc(f.type || '--') + ' | ' + _esc(f.first_date || '--') + ' | 状态: <span class="font-bold">' + _esc(f.status) + '</span></p>' +
            '</div>' +
            predAlphaHtml +
            periodHtml +
            (d.current_signal ? (
                '<div class="border-b border-outline pb-3">' +
                '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">当前信号 / SIGNAL</h4>' +
                '<div class="flex items-center gap-2 mb-1">' +
                '<span class="badge ' + (
                    d.current_signal.signal === 'HOLD' ? 'badge--hold' :
                    d.current_signal.signal === 'BUY_MORE' ? 'badge--add' :
                    d.current_signal.signal === 'WARNING' ? 'badge--warn' :
                    d.current_signal.signal === 'EXIT' ? 'badge--exit' :
                    'badge--muted'
                ) + '">' + (
                    (window.SIGNAL_LABELS && window.SIGNAL_LABELS[d.current_signal.signal]) ?
                        window.SIGNAL_LABELS[d.current_signal.signal] : _esc(d.current_signal.signal)
                ) + '</span>' +
                '<span class="text-[11px] text-on-surface-variant font-bold">' + _esc(d.current_signal.logic_verdict) + '</span>' +
                '<span class="text-[11px] text-on-surface-variant">' + _esc(d.current_signal.date) + '</span>' +
                '</div>' +
                '<p class="text-[13px] font-bold leading-relaxed">' + _esc(d.current_signal.reason || '无详细原因') + '</p>' +
                (d.current_signal.sector_risk ? '<p class="text-[11px] text-up mt-1">赛道风险</p>' : '') +
                (d.current_signal.holding_risk ? '<p class="text-[11px] text-up mt-1">持仓风险</p>' : '') +
                '</div>'
            ) : '' ) +
            '<div>' +
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">推荐理由</h4>' +
            '<p class="text-[13px] font-bold leading-relaxed whitespace-pre-wrap">' + _esc(f.buy_reason || '暂无推荐理由') + '</p>' +
            '</div>' +
            '<div>' +
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">前十大重仓</h4>' +
            holdingsHtml +
            '</div>' +
            '<div>' +
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 t-border-accent border-b pb-1">净值走势（近90日）</h4>' +
            navChartHtml +
            '</div>' +
            '</div>';
    } catch(e) {
        container.innerHTML = '<p class="text-error font-bold">加载失败: ' + _esc(String(e.message || e)) + '</p>';
    }
}
window.onclick = function(event) {
    if (event.target.classList.contains('modal-backdrop')) {
        if (event.target.id === 'fundDetailModal') {
            closeFundDetail();
        } else {
            closeModal(event.target.id);
        }
    }
}

var _pendingClear = false;

function openPasswordPrompt(action) {
    _pendingClear = (action === 'clear');
    document.getElementById('passwordInput').value = '';
    document.getElementById('passwordError').classList.add('hidden');
    openModal('passwordModal');
}

async function submitPassword() {
    var pwd = val('passwordInput');
    if (!pwd) { document.getElementById('passwordError').classList.remove('hidden'); return; }
    try {
        var r = await fetch('/api/check-password', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password:pwd}) });
        var d = await r.json();
        if (d.ok) {
            sessionStorage.setItem('settingsAuth', pwd);
            closeModal('passwordModal');
            if (_pendingClear) { _pendingClear = false; doClearRecommendations(); }
            else { openModal('settingsModal'); }
        }
        else { document.getElementById('passwordError').classList.remove('hidden'); }
    } catch(e) { document.getElementById('passwordError').classList.remove('hidden'); }
}

function clearRecommendationsFlow() {
    openPasswordPrompt('clear');
}

async function doClearRecommendations() {
    try {
        var r = await fetch('/api/clear-recommendations', { method:'POST', headers:{'Content-Type':'application/json', 'X-Settings-Password': sessionStorage.getItem('settingsAuth') || ''}, body: JSON.stringify({dry_run:true}) });
        if (r.status === 403) { alert('密码已变更，请重新验证'); openPasswordPrompt(); return; }
        var d = await r.json();
        if (d.status !== 'ok') { alert(d.message || '请求失败'); return; }
        var del = d.deleted || {};
        var rec = del.recommend_log || 0;
        var total = rec + (del.sector_selections || 0) + (del.monitor_events || 0) + (del.evolution_insights || 0) + (del.quality_metrics || 0) + (del.macro_news || 0);
        if (!confirm('将永久删除 ' + rec + ' 条推荐记录及关联数据（共 ' + total + ' 条），此操作不可恢复。确定继续吗？')) return;
        var r2 = await fetch('/api/clear-recommendations', { method:'POST', headers:{'Content-Type':'application/json', 'X-Settings-Password': sessionStorage.getItem('settingsAuth') || ''}, body: JSON.stringify({dry_run:false}) });
        if (r2.status === 403) { alert('密码已变更，请重新验证'); openPasswordPrompt(); return; }
        var d2 = await r2.json();
        if (d2.status === 'ok') { alert('已清除全部推荐数据'); location.reload(); }
        else { alert('清除失败: ' + (d2.message || '未知错误')); }
    } catch(e) { alert('请求失败: ' + e.message); }
}

// ===== 定时调度（SCHEDULE：开关 + 下拉选择） =====
var _sched = { enabled: false, hour: 8, minute: 0 };

function renderSchedSelects() {
  var hsel = document.getElementById('schedHour');
  var msel = document.getElementById('schedMinute');
  if (!hsel || !msel) return;
  var hhtml = '';
  for (var h = 0; h < 24; h++) {
    hhtml += '<option value="' + h + '">' + ('0' + h).slice(-2) + ' 时</option>';
  }
  hsel.innerHTML = hhtml;
  var mhtml = '';
  [0, 15, 30, 45].forEach(function(m) {
    mhtml += '<option value="' + m + '">' + ('0' + m).slice(-2) + ' 分</option>';
  });
  msel.innerHTML = mhtml;
}

function onSchedHourChange() { _sched.hour = parseInt(document.getElementById('schedHour').value) || 0; updateSchedNextRun(); }
function onSchedMinuteChange() { _sched.minute = parseInt(document.getElementById('schedMinute').value) || 0; updateSchedNextRun(); }
function toggleSchedule() { _sched.enabled = !_sched.enabled; syncSchedUI(); }

function updateSchedNextRun() {
  var next = document.getElementById('schedNextRun');
  if (!next) return;
  var now = new Date();
  var run = new Date(now.getFullYear(), now.getMonth(), now.getDate(), _sched.hour, _sched.minute, 0);
  if (run <= now) run.setDate(run.getDate() + 1);
  var day = run.getDate() === now.getDate() ? '今天' : '明天';
  var hh = ('0' + run.getHours()).slice(-2), mm = ('0' + run.getMinutes()).slice(-2);
  next.textContent = '下次执行：' + day + ' ' + hh + ':' + mm;
}

function syncSchedUI() {
  var toggle = document.getElementById('schedToggle');
  var knob = document.getElementById('schedToggleKnob');
  var body = document.getElementById('schedBody');
  var next = document.getElementById('schedNextRun');
  var hsel = document.getElementById('schedHour');
  var msel = document.getElementById('schedMinute');
  if (!toggle || !knob || !body || !next) return;
  toggle.setAttribute('aria-checked', _sched.enabled ? 'true' : 'false');
  if (_sched.enabled) {
    toggle.className = 'relative w-11 h-6 rounded-full t-fill-accent transition-colors shrink-0';
    knob.className = 'absolute top-0.5 left-[22px] w-[18px] h-[18px] rounded-full bg-white transition-[left,background-color] duration-200';
    body.style.opacity = '1';
    if (hsel) hsel.disabled = false;
    if (msel) msel.disabled = false;
    updateSchedNextRun();
  } else {
    toggle.className = 'relative w-11 h-6 rounded-full border border-outline bg-surface transition-colors shrink-0';
    knob.className = 'absolute top-0.5 left-0.5 w-[18px] h-[18px] rounded-full bg-on-surface-variant transition-[left,background-color] duration-200';
    body.style.opacity = '0.5';
    if (hsel) hsel.disabled = true;
    if (msel) msel.disabled = true;
    next.textContent = '定时未启用';
  }
}

async function loadSettings() {
    try {
        var r = await fetch('/api/settings');
        var s = await r.json();
        var llm = s.llm || {};
        document.getElementById('llmBaseUrl').value = llm.base_url || '';
        document.getElementById('llmApiKey').value = llm.api_key || '';
        document.getElementById('llmModel').value = llm.model || '';
        var sched = s.scheduler || {};
        _sched.enabled = sched.hour !== '' && sched.hour != null;
        _sched.hour = _sched.enabled ? parseInt(sched.hour) : 8;
        _sched.minute = sched.minute != null ? parseInt(sched.minute) : 0;
        renderSchedSelects();
        document.getElementById('schedHour').value = _sched.hour;
        document.getElementById('schedMinute').value = _sched.minute;
        syncSchedUI();
    } catch(e) { console.error('settings load failed', e); }
}

async function saveSettings() {
    var btn = document.getElementById('saveSettingsBtn');
    btn.textContent = '保存中...';
    btn.disabled = true;
    try {
        var body = {
            llm: {
                base_url: val('llmBaseUrl'),
                api_key: val('llmApiKey'),
                model: val('llmModel'),
            },
            scheduler: {
                hour: _sched.enabled ? _sched.hour : '',
                minute: _sched.enabled ? _sched.minute : 0,
            },
        };
        var r = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json', 'X-Settings-Password': sessionStorage.getItem('settingsAuth') || ''}, body: JSON.stringify(body) });
        if (r.status === 403) { btn.textContent = '密码已变更，请重新验证'; btn.disabled = false; openPasswordPrompt(); return; }
        var d = await r.json();
        if (d.status === 'ok') { btn.textContent = '已保存 ✓'; setTimeout(function(){ btn.textContent = '保存并更新配置'; btn.disabled = false; }, 1500); }
        else { btn.textContent = '保存失败'; btn.disabled = false; }
    } catch(e) { btn.textContent = '保存失败'; btn.disabled = false; console.error(e); }
}

async function triggerPipeline() {
    try {
        var r = await fetch('/api/run-pipeline', { method: 'POST', headers: {'X-Settings-Password': sessionStorage.getItem('settingsAuth') || ''} });
        if (r.status === 403) { alert('密码已变更，请重新验证'); openPasswordPrompt(); return; }
        var d = await r.json();
        if (d.status === 'started') {
            openModal('systemLogModal');
        } else {
            alert('管线启动失败');
        }
    } catch(e) {
        alert('请求失败: ' + e.message);
    }
}

// ===== 管线自动执行状态卡 =====
var _pipeStarted = null; // 系统进程启动时刻（后端 /api/pipeline-schedule 返回）

function updatePipeUptime() {
    var el = document.getElementById('pipeUptime');
    if (!el || !_pipeStarted) return;
    var s = Math.max(0, Math.floor((Date.now() - _pipeStarted.getTime()) / 1000));
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    var text;
    if (d > 0) text = d + '天' + h + '小时' + m + '分钟';
    else if (h > 0) text = h + '小时' + m + '分钟';
    else if (m > 0) text = m + '分钟';
    else text = '刚刚启动';
    el.textContent = text;
}

async function loadPipelineSchedule() {
  try {
    var r = await fetch('/api/pipeline-schedule');
    var d = await r.json();
    var dot = document.getElementById('pipeStateDot');
    var next = document.getElementById('pipeNextRun');
    var last = document.getElementById('pipeLastRun');
    var state = document.getElementById('pipeStateText');
    if (!dot || !next || !last || !state) return;
    if (d.started_at) {
        _pipeStarted = new Date(d.started_at);
        updatePipeUptime();
    }
    if (d.enabled) {
      next.textContent = d.next_run ? d.next_run.substring(5) : '未设置';
      dot.className = 'w-1.5 h-1.5 rounded-full t-fill-accent';
    } else {
      next.textContent = '定时关闭';
      dot.className = 'w-1.5 h-1.5 rounded-full bg-on-surface-variant';
    }
    last.textContent = d.last_run_date || '暂无记录';
    if (d.state === 'running') {
      dot.className = 'w-1.5 h-1.5 rounded-full bg-warn pulse-dot';
      state.textContent = '管线运行中…';
    } else if (d.state === 'error') {
      state.textContent = '上次执行失败';
    } else if (d.state === 'done') {
      state.textContent = '上次执行成功';
    } else {
      state.textContent = d.last_run_date ? '等待定时触发' : '未运行过';
    }
  } catch (e) {
    console.error('pipeline schedule load failed', e);
  }
}
loadPipelineSchedule();
setInterval(loadPipelineSchedule, 30000);
setInterval(updatePipeUptime, 1000);
