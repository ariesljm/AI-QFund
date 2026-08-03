function _esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
var logInterval = null;
var _logCount = 0;
var _lastId = 0;
var _fetching = false;
var FUND_SKEL = '<div class="space-y-3"><div class="skel h-5 w-3/4"></div><div class="skel h-4 w-1/2"></div><div class="skel h-4 w-full"></div><div class="skel h-4 w-5/6"></div><div class="skel h-16 w-full"></div><div class="skel h-32 w-full"></div></div>';

function switchTab(name) {
  var today = document.getElementById('tab-today');
  var quality = document.getElementById('tab-quality');
  var bToday = document.getElementById('tabBtn-today');
  var bQuality = document.getElementById('tabBtn-quality');
  if (name === 'quality') {
    today.classList.add('hidden');
    quality.classList.remove('hidden');
    bToday.className = 'tab-btn px-4 py-2 font-label-caps text-on-surface-variant border-b-2 border-transparent hover:text-on-surface';
    bQuality.className = 'tab-btn px-4 py-2 font-label-caps text-accent border-b-2 border-accent';
  } else {
    quality.classList.add('hidden');
    today.classList.remove('hidden');
    bQuality.className = 'tab-btn px-4 py-2 font-label-caps text-on-surface-variant border-b-2 border-transparent hover:text-on-surface';
    bToday.className = 'tab-btn px-4 py-2 font-label-caps text-accent border-b-2 border-accent';
  }
}

function _parseLogLine(str) {
    try { var j = JSON.parse(str); return { level: j.level || 'INFO', ts: (j.timestamp || '').substring(0, 19), msg: j.message || j.event || '' }; }
    catch(e) {
        var s = str.indexOf('['), e2 = str.indexOf(']', s);
        var level = (s >= 0 && e2 > s) ? str.substring(s + 1, e2).trim() : 'INFO';
        if (['ERROR','WARNING','INFO','DEBUG'].indexOf(level) < 0) level = 'INFO';
        var ts = s > 0 ? str.substring(0, s - 1).trim() : str.substring(0, 19).trim();
        var tail = e2 >= 0 ? str.substring(e2 + 1).trim() : str.substring(19).trim();
        var colon = tail.indexOf(':');
        var msg = colon > 0 ? tail.substring(colon + 1).trim() : tail;
        return { level: level, ts: ts, msg: msg };
    }
}

function applyLogFilter() {
    var btn = document.querySelector('.log-filter.active');
    var level = btn ? btn.getAttribute('data-level') : 'all';
    var query = (document.getElementById('logSearch').value || '').toLowerCase();
    document.querySelectorAll('#logLines .log-row').forEach(function(el) {
        var show = true;
        if (level !== 'all' && el.getAttribute('data-level') !== level) show = false;
        if (show && query && !el.textContent.toLowerCase().includes(query)) show = false;
        el.style.display = show ? '' : 'none';
    });
}

function resetLogFilter() {
    document.querySelectorAll('.log-filter').forEach(function(b) { b.classList.remove('active'); b.style.background = ''; });
    var allBtn = document.querySelector('.log-filter[data-level="all"]');
    if (allBtn) { allBtn.classList.add('active'); allBtn.style.background = 'var(--color-surface-tint)'; }
}

function clearLogs() {
    document.getElementById('logLines').innerHTML = '';
    resetLogFilter();
}

function openModal(id) {
    document.getElementById(id).classList.remove('hidden');
    if (id === 'systemLogModal') {
        if (logInterval) { clearInterval(logInterval); logInterval = null; }
        _logCount = 0;
        _lastId = 0;
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
    document.getElementById(id).classList.add('hidden');
    if (id === 'systemLogModal' && logInterval) {
        clearInterval(logInterval);
        logInterval = null;
    }
}
function openFundDetail(code) {
    var modal = document.getElementById('fundDetailModal');
    var panel = document.getElementById('fundDetailPanel');
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

        container.innerHTML =
            '<div class="space-y-4">' +
            '<div class="border-b border-outline pb-3">' +
            '<h3 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-1">' + _esc(f.name) + '</h3>' +
            '<p class="text-[11px] text-on-surface-variant">' + _esc(f.type || '--') + ' | ' + _esc(f.first_date || '--') + ' | 状态: <span class="font-bold">' + _esc(f.status) + '</span></p>' +
            '</div>' +
            (d.current_signal ? (
                '<div class="border-b border-outline pb-3">' +
                '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 border-b border-accent pb-1">当前信号 / SIGNAL</h4>' +
                '<div class="flex items-center gap-2 mb-1">' +
                '<span class="badge ' + (
                    d.current_signal.signal === 'HOLD' ? 'badge--hold' :
                    d.current_signal.signal === 'BUY_MORE' ? 'badge--add' :
                    d.current_signal.signal === 'WARNING' ? 'badge--warn' :
                    d.current_signal.signal === 'EXIT' ? 'badge--exit' :
                    'badge--muted'
                ) + '">' + (
                    d.current_signal.signal === 'HOLD' ? '持有' :
                    d.current_signal.signal === 'BUY_MORE' ? '加仓' :
                    d.current_signal.signal === 'WARNING' ? '警惕' :
                    d.current_signal.signal === 'EXIT' ? '离场' :
                    _esc(d.current_signal.signal)
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
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 border-b border-accent pb-1">推荐理由</h4>' +
            '<p class="text-[13px] font-bold leading-relaxed whitespace-pre-wrap">' + _esc(f.buy_reason || '暂无推荐理由') + '</p>' +
            '</div>' +
            '<div>' +
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 border-b border-accent pb-1">前十大重仓</h4>' +
            holdingsHtml +
            '</div>' +
            '<div>' +
            '<h4 class="font-label-caps text-[13px] uppercase tracking-widest font-bold mb-2 border-b border-accent pb-1">净值走势（近90日）</h4>' +
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
            event.target.classList.add('hidden');
            if (logInterval) { clearInterval(logInterval); logInterval = null; }
        }
    }
}

async function fetchLogs() {
    if (_fetching) return;
    _fetching = true;
    var container = document.getElementById('logContainer');
    var linesEl = document.getElementById('logLines');
    if (!container || !linesEl) { _fetching = false; return; }
    var atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    try {
        var r = await fetch('/api/logs?lines=200&after=' + _lastId);
        var data = await r.json();
        if (!data.lines || !data.lines.length) { _fetching = false; return; }
        for (var i = 0; i < data.lines.length; i++) {
            var p = _parseLogLine(data.lines[i]);
            var row = document.createElement('div');
            row.className = 'log-row flex items-start gap-1 px-3 py-[3px] transition-colors';
            row.setAttribute('data-level', p.level);
            var ts = _esc(p.ts);
            var msg = _esc(p.msg);
            if (p.level === 'ERROR') {
                row.classList.add('log-row--error');
                row.innerHTML = '<span class="shrink-0 w-[52px] text-[11px] font-bold text-up uppercase">ERROR</span><span class="shrink-0 text-[11px] opacity-50 w-[130px]">' + ts + '</span><span class="break-all text-up font-medium">' + msg + '</span>';
            } else if (p.level === 'WARNING') {
                row.innerHTML = '<span class="shrink-0 w-[52px] text-[11px] font-bold uppercase" style="color:var(--color-warn-bright)">WARN</span><span class="shrink-0 text-[11px] opacity-50 w-[130px]">' + ts + '</span><span class="break-all text-white">' + msg + '</span>';
            } else if (p.level === 'DEBUG') {
                row.innerHTML = '<span class="shrink-0 w-[52px] text-[11px] font-bold opacity-40 uppercase">DEBUG</span><span class="shrink-0 text-[11px] opacity-30 w-[130px]">' + ts + '</span><span class="break-all opacity-50">' + msg + '</span>';
            } else {
                row.innerHTML = '<span class="shrink-0 w-[52px] text-[11px] font-bold opacity-60 uppercase">INFO</span><span class="shrink-0 text-[11px] opacity-50 w-[130px]">' + ts + '</span><span class="break-all">' + msg + '</span>';
            }
            linesEl.appendChild(row);
        }
        _lastId = data.last_id;
        _logCount = data.total;
        document.getElementById('logCount').textContent = _logCount + ' 条';
        if (atBottom) container.scrollTop = container.scrollHeight;
        else {
            var btn = document.getElementById('logScrollBtn');
            if (btn) btn.classList.remove('hidden');
        }
        applyLogFilter();
    } catch(e) {
        console.error('日志加载失败:', e);
    }
    _fetching = false;
}

function val(id) { return document.getElementById(id).value; }

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
        var r = await fetch('/api/clear-recommendations', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({dry_run:true}) });
        var d = await r.json();
        if (d.status !== 'ok') { alert(d.message || '请求失败'); return; }
        var del = d.deleted || {};
        var rec = del.recommend_log || 0;
        var total = rec + (del.sector_selections || 0) + (del.monitor_events || 0) + (del.evolution_insights || 0) + (del.quality_metrics || 0);
        if (!confirm('将永久删除 ' + rec + ' 条推荐记录及关联数据（共 ' + total + ' 条），此操作不可恢复。确定继续吗？')) return;
        var r2 = await fetch('/api/clear-recommendations', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({dry_run:false}) });
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
    toggle.className = 'relative w-11 h-6 rounded-full bg-accent border border-accent transition-colors shrink-0';
    knob.className = 'absolute top-0.5 left-[22px] w-[18px] h-[18px] rounded-full bg-white transition-all duration-200';
    body.style.opacity = '1';
    if (hsel) hsel.disabled = false;
    if (msel) msel.disabled = false;
    updateSchedNextRun();
  } else {
    toggle.className = 'relative w-11 h-6 rounded-full border border-outline bg-surface transition-colors shrink-0';
    knob.className = 'absolute top-0.5 left-0.5 w-[18px] h-[18px] rounded-full bg-on-surface-variant transition-all duration-200';
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
        var r = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
        var d = await r.json();
        if (d.status === 'ok') { btn.textContent = '已保存 ✓'; setTimeout(function(){ btn.textContent = '保存并更新配置'; btn.disabled = false; }, 1500); }
        else { btn.textContent = '保存失败'; btn.disabled = false; }
    } catch(e) { btn.textContent = '保存失败'; btn.disabled = false; console.error(e); }
}

async function triggerPipeline() {
    try {
        var r = await fetch('/api/run-pipeline', { method: 'POST' });
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

// ===== 实时指数 / 快讯 / 分页 / 推荐刷新（原 module 脚本） =====

// 实时指数：走后端代理 /api/indices（15s 轮询 + 后端缓存/降级），无第三方 CDN 依赖
function _upd(el, v) { if (el) el.textContent = v; }

function _color(v, dir) {
  if (dir === 'flat') return 'text-on-surface-variant';
  return dir === 'up' ? 'text-up' : 'text-down';
}

async function _updateIndices() {
  try {
    const r = await fetch('/api/indices');
    const d = await r.json();
    const map = {};
    (d.items || []).forEach(function(it) { map[it.code] = it; });
    const fallback = d.source !== 'live';

    [['sh000001', 'sse'], ['sh000300', '300']].forEach(function(pair) {
      const q = map[pair[0]];
      const pfx = pair[1];
      if (!q) return;
      const pct = q.change_percent;
      const dir = (pct === null || pct === undefined) ? 'flat' : (pct > 0.01 ? 'up' : pct < -0.01 ? 'down' : 'flat');
      const valEl = document.getElementById('idx-' + pfx + '-value');
      const pctEl = document.getElementById('idx-' + pfx + '-pct');
      _upd(valEl, q.price != null ? q.price.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—');
      _upd(pctEl, pct != null ? (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%' : '—');
      if (valEl) valEl.className = valEl.className.replace(/(text-up|text-down|text-on-surface-variant)/g, '') + ' ' + _color(pct, dir);
      if (pctEl) pctEl.className = 'font-data-sm ' + _color(pct, dir);
    });

    const fb = document.getElementById('quoteFallback');
    if (fb) {
      if (fallback) fb.classList.remove('hidden'); else fb.classList.add('hidden');
    }
    _upd(document.getElementById('quoteUpdated'), new Date().toTimeString().slice(0, 8));
  } catch (e) {
    console.error('实时指数更新失败:', e);
  }
}

_updateIndices();
setInterval(_updateIndices, 15000);

// ===== 快讯轮播：8s 自动切换 + 悬停暂停 + 点击展开全文 =====
var _newsItems = window.__NEWS_ITEMS__ || [];
var _newsIdx = 0;
var _newsTimer = null;
var _newsOpen = -1; // 弹窗列表中当前展开的条目索引（-1 表示无）

function renderNews() {
  var cur = document.getElementById('newsCurrentText');
  var idx = document.getElementById('newsIdx');
  if (!cur || !idx) return;
  if (!_newsItems.length) { cur.textContent = '暂无快讯'; idx.textContent = ''; return; }
  var it = _newsItems[_newsIdx];
  cur.textContent = (it && it.title) ? it.title : it;
  idx.textContent = (_newsIdx + 1) + '/' + _newsItems.length;
}

function restartNewsTimer() {
  if (_newsTimer) { clearInterval(_newsTimer); _newsTimer = null; }
  if (_newsItems.length > 1) {
    _newsTimer = setInterval(function() { newsNext(true); }, 8000);
  }
}

function newsNext(auto) {
  if (!_newsItems.length) return;
  _newsIdx = (_newsIdx + 1) % _newsItems.length;
  _newsOpen = _newsIdx;
  renderNews();
  if (auto !== true) restartNewsTimer();
  var m = document.getElementById('newsModal');
  if (m && !m.classList.contains('hidden')) renderNewsList();
}

function newsPrev() {
  if (!_newsItems.length) return;
  _newsIdx = (_newsIdx - 1 + _newsItems.length) % _newsItems.length;
  _newsOpen = _newsIdx;
  renderNews();
  restartNewsTimer();
  var m = document.getElementById('newsModal');
  if (m && !m.classList.contains('hidden')) renderNewsList();
}

function newsListHtml(activeIdx, openIdx) {
  var html = '';
  for (var i = 0; i < _newsItems.length; i++) {
    var it = _newsItems[i];
    var title = (it && it.title) ? it.title : it;
    var summary = (it && it.summary) ? it.summary : '';
    var active = i === activeIdx;
    var open = i === openIdx;
    html += '<div class="rounded-md border transition-colors overflow-hidden ' +
      (active ? 'border-accent bg-accent-soft' : 'border-outline bg-surface') + '">' +
      '<button class="w-full text-left p-3 flex items-start gap-2" onclick="newsToggle(' + i + ')">' +
      '<span class="mt-0.5 w-5 h-5 shrink-0 flex items-center justify-center rounded bg-surface border border-outline font-data-sm text-[10px] text-on-surface-variant">' + (i + 1) + '</span>' +
      '<span class="flex-1 min-w-0">' +
      '<span class="block text-[13px] font-bold text-on-surface leading-snug">' + _esc(title) + '</span>' +
      (open ? '<span class="block mt-1.5 text-[13px] text-on-surface-variant leading-relaxed whitespace-pre-wrap">' + _esc(summary || title) + '</span>' : '') +
      '</span>' +
      '<span class="material-symbols-outlined text-[16px] text-on-surface-variant shrink-0 transition-transform duration-200 ' + (open ? 'rotate-180' : '') + '">expand_more</span>' +
      '</button>' +
      '</div>';
  }
  return html;
}

function renderNewsList() {
  var list = document.getElementById('newsList');
  if (!list) return;
  list.innerHTML = newsListHtml(_newsIdx, _newsOpen);
  document.getElementById('newsDetailIdx').textContent = (_newsIdx + 1) + ' / ' + _newsItems.length;
}

function newsToggle(i) {
  _newsOpen = (_newsOpen === i) ? -1 : i; // 手风琴：再点收起
  _newsIdx = i; // 同步轮播位置
  renderNews();
  restartNewsTimer();
  renderNewsList();
}

function openNewsDetail() {
  if (!_newsItems.length) return;
  if (_newsTimer) { clearInterval(_newsTimer); _newsTimer = null; }
  _newsOpen = _newsIdx; // 打开弹窗默认展开当前轮播条
  renderNewsList();
  openModal('newsModal');
}

// 悬停暂停自动轮播
(function() {
  var carousel = document.getElementById('newsCarousel');
  if (!carousel) return;
  carousel.addEventListener('mouseenter', function() {
    if (_newsTimer) { clearInterval(_newsTimer); _newsTimer = null; }
  });
  carousel.addEventListener('mouseleave', function() {
    if (!_newsTimer && _newsItems.length > 1) {
      _newsTimer = setInterval(function() { newsNext(true); }, 8000);
    }
  });
})();

renderNews();
restartNewsTimer();

// ===== 管线自动执行状态卡 =====
async function loadPipelineSchedule() {
  try {
    var r = await fetch('/api/pipeline-schedule');
    var d = await r.json();
    var dot = document.getElementById('pipeStateDot');
    var next = document.getElementById('pipeNextRun');
    var last = document.getElementById('pipeLastRun');
    var state = document.getElementById('pipeStateText');
    if (!dot || !next || !last || !state) return;
    if (d.enabled) {
      next.textContent = d.next_run ? d.next_run.substring(5) : '未设置';
      dot.className = 'w-1.5 h-1.5 rounded-full bg-accent';
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

// 表格分页
(function() {
  var PAGE_SIZE = 15;
  var rows = document.querySelectorAll('#trackingBody tr[data-page]');
  var totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  var currentPage = 1;

  function renderPage(page) {
    rows.forEach(function(r) {
      var p = parseInt(r.getAttribute('data-page'));
      r.style.display = (p === page) ? '' : 'none';
    });
    var btns = document.querySelectorAll('#paginationControls button');
    btns.forEach(function(b) {
      var p = parseInt(b.getAttribute('data-page'));
      b.className = (p === page)
        ? 'px-3 py-1 rounded-md text-[11px] font-bold bg-accent text-white border border-accent'
        : 'px-3 py-1 rounded-md text-[11px] font-bold text-on-surface-variant border border-outline hover:bg-surface transition-colors';
    });
  }

  function buildControls() {
    var container = document.getElementById('paginationControls');
    if (!container) return;
    if (totalPages <= 1) { container.innerHTML = ''; return; }
    var html = '';
    for (var i = 1; i <= totalPages; i++) {
      html += '<button data-page="' + i + '" class="px-3 py-1 rounded-md text-[11px] font-bold border border-outline hover:bg-surface transition-colors">' + i + '</button>';
    }
    container.innerHTML = html;
    container.addEventListener('click', function(e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      var p = parseInt(btn.getAttribute('data-page'));
      if (p === currentPage) return;
      currentPage = p;
      renderPage(p);
    });
  }

  buildControls();
  if (rows.length > 0) renderPage(1);
})();

// 推荐结果自动刷新
var _initialRecId = parseInt(document.body.getAttribute('data-rec-id') || '0', 10) || 0;
setInterval(function() {
  fetch('/api/recommendation-status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.id && d.id !== _initialRecId) {
        location.reload();
      }
    })
    .catch(function() {});
}, 30000);
