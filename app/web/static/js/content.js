/* ===== AI-QFund 内容域：日志 / 实时指数 / 表格分页 / 推荐刷新 =====
   候选6 拆分（原 app.js 内容区段，非 module：全局函数互见，行为不变）。*/
function _parseLogLine(str) {
    try { var j = JSON.parse(str); return { id: j.id || 0, level: j.level || 'INFO', ts: (j.timestamp || '').substring(0, 19), msg: j.message || j.event || '' }; }
    catch(e) {
        var s = str.indexOf('['), e2 = str.indexOf(']', s);
        var level = (s >= 0 && e2 > s) ? str.substring(s + 1, e2).trim() : 'INFO';
        if (['ERROR','WARNING','INFO','DEBUG'].indexOf(level) < 0) level = 'INFO';
        var ts = s > 0 ? str.substring(0, s - 1).trim() : str.substring(0, 19).trim();
        var tail = e2 >= 0 ? str.substring(e2 + 1).trim() : str.substring(19).trim();
        var colon = tail.indexOf(':');
        var msg = colon > 0 ? tail.substring(colon + 1).trim() : tail;
        return { id: 0, level: level, ts: ts, msg: msg };
    }
}

var _minId = 0;          // 已显示日志中最小的 id（向前翻页游标）
var _noMoreLogs = false; // 已翻到最早日志
var _loadingOlder = false;

function _buildLogRow(line) {
    var p = _parseLogLine(line);
    var row = document.createElement('div');
    row.className = 'log-row flex items-start gap-1 px-3 py-[3px] transition-colors';
    row.setAttribute('data-level', p.level);
    var ts = _esc(p.ts);
    var msg = _esc(p.msg);
    if (p.level === 'ERROR') {
        row.classList.add('log-row--error');
        row.innerHTML = '<span class="shrink-0 w-[52px] text-[12px] font-bold text-up uppercase">ERROR</span><span class="shrink-0 whitespace-nowrap text-[12px] opacity-50 w-[160px]">' + ts + '</span><span class="break-all text-up font-medium">' + msg + '</span>';
    } else if (p.level === 'WARNING') {
        row.innerHTML = '<span class="shrink-0 w-[52px] text-[12px] font-bold uppercase" style="color:var(--color-warn-bright)">WARN</span><span class="shrink-0 whitespace-nowrap text-[12px] opacity-50 w-[160px]">' + ts + '</span><span class="break-all text-white">' + msg + '</span>';
    } else if (p.level === 'DEBUG') {
        row.innerHTML = '<span class="shrink-0 w-[52px] text-[12px] font-bold opacity-40 uppercase">DEBUG</span><span class="shrink-0 whitespace-nowrap text-[12px] opacity-30 w-[160px]">' + ts + '</span><span class="break-all opacity-50">' + msg + '</span>';
    } else {
        row.innerHTML = '<span class="shrink-0 w-[52px] text-[12px] font-bold opacity-60 uppercase">INFO</span><span class="shrink-0 whitespace-nowrap text-[12px] opacity-50 w-[160px]">' + ts + '</span><span class="break-all">' + msg + '</span>';
    }
    return row;
}

async function loadOlderLogs() {
    if (_loadingOlder || _noMoreLogs || !_minId) return;
    _loadingOlder = true;
    var container = document.getElementById('logContainer');
    var linesEl = document.getElementById('logLines');
    if (!container || !linesEl) { _loadingOlder = false; return; }
    var prevHeight = container.scrollHeight;
    var prevScroll = container.scrollTop;
    try {
        var r = await fetch('/api/logs?lines=200&before=' + _minId);
        var data = await r.json();
        if (data.lines && data.lines.length) {
            var frag = document.createDocumentFragment();
            for (var i = 0; i < data.lines.length; i++) {
                frag.appendChild(_buildLogRow(data.lines[i]));
            }
            linesEl.insertBefore(frag, linesEl.firstChild);
            _minId = _parseLogLine(data.lines[0]).id || _minId;
            if (data.lines.length < 200) _noMoreLogs = true;
            // prepend 后保持视口位置不变（补偿新增高度）
            container.scrollTop = prevScroll + (container.scrollHeight - prevHeight);
            applyLogFilter();
        } else {
            _noMoreLogs = true;
        }
    } catch(e) {
        console.error('更早日志加载失败:', e);
    }
    _loadingOlder = false;
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
            linesEl.appendChild(_buildLogRow(data.lines[i]));
        }
        _lastId = data.last_id;
        if (!_minId && data.lines.length) {
            _minId = _parseLogLine(data.lines[0]).id || 0;
            _noMoreLogs = false;
        }
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
        ? 'px-3 py-1 rounded-md text-[11px] font-bold t-fill-accent text-white'
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
// ===== 每日投研报告（tab-logs 默认显示，替代原始日志流；原始日志仍进 system.log 文件供排查）=====
async function fetchDailyReport() {
    try {
        var r = await fetch('/api/daily-report');
        var d = await r.json();
        renderReport(d.lines || []);
        var lc = document.getElementById('logCount');
        if (lc) lc.textContent = (d.lines || []).length + ' 条';
    } catch(e) { console.error('report fetch fail', e); }
}

function renderReport(lines) {
    var data = [], reco = [], sup = [], evo = [];
    lines.forEach(function(l) {
        var m = (l.message || ''), lg = (l.logger || '');
        if (lg === 'recommend_v2' || /推荐|排雷|审计|剪枝|复合分/.test(m)) reco.push(l);
        else if (lg === 'supervise' || /监控|转移|状态机|信号/.test(m)) sup.push(l);
        else if (['quality','knowledge','calibration','drift'].indexOf(lg) >= 0) evo.push(l);
        else data.push(l);
    });
    var html = '';
    function sec(title, arr) {
        if (!arr.length) return;
        html += '<div style="margin-bottom:16px"><div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--color-accent);margin-bottom:6px;border-bottom:1px solid var(--color-rule);padding-bottom:4px;font-weight:700">' + _esc(title) + '</div>';
        arr.forEach(function(l) {
            html += '<div style="display:flex;gap:12px;font-size:13px;line-height:1.7;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)"><span style="color:var(--color-muted);font-family:var(--font-mono);font-size:11px;flex-shrink:0;white-space:nowrap">' + (l.timestamp || '').slice(11,19) + '</span><span style="color:var(--color-log-ink)">' + _esc(l.message) + '</span></div>';
        });
        html += '</div>';
    }
    sec('数据基座', data);
    sec('今日推荐', reco);
    sec('追踪监控', sup);
    sec('自我进化', evo);
    var ll = document.getElementById('logLines');
    if (ll) ll.innerHTML = html || '<p style="color:var(--color-muted);padding:24px;text-align:center">今日暂无报告数据（管线运行后生成）</p>';
}
