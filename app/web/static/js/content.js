/* ===== AI-QFund 内容域：日志 / 实时指数 / 快讯轮播 / 表格分页 / 推荐刷新 =====
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

// ===== 快讯轮播：标题 + 摘要单行，摘要向左滚动（marquee），滚动完成后切下一条 =====
var _newsItems = window.__NEWS_ITEMS__ || [];
var _newsIdx = 0;
var _newsOpen = -1;          // 弹窗列表中当前展开的条目索引（-1 表示无）
var _newsScrolling = false;  // 当前摘要是否处于滚动模式
var _newsStayTimer = null;   // 短摘要静止停留计时器
var _newsPaused = false;     // 悬停/弹窗打开时暂停推进
var _newsSpeed = 50;         // 摘要滚动速度 px/s
var _reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

function _newsSummaryEl() { return document.getElementById('newsSummaryText'); }

// 按摘要实际宽度启动滚动：溢出容器才滚（时长 = 溢出像素 / 速度），短摘要静止停留 4s 后切换
function startNewsMarquee() {
  var inner = _newsSummaryEl();
  var track = document.getElementById('newsMarquee');
  if (!inner || !track) return;
  // 手动切换时先清掉上一摘要的停留计时，避免旧定时器到期打断当前滚动
  if (_newsStayTimer) { clearTimeout(_newsStayTimer); _newsStayTimer = null; }
  inner.classList.remove('news-marquee-anim');
  void inner.offsetWidth; // 强制 reflow，重启动画
  var overflow = inner.scrollWidth - track.clientWidth;
  _newsScrolling = !_reducedMotion && overflow > 0;
  if (_newsScrolling) {
    inner.style.animationDuration = (overflow / _newsSpeed) * 1000 + 'ms';
    inner.style.animationPlayState = _newsPaused ? 'paused' : 'running';
    inner.classList.add('news-marquee-anim');
  } else {
    inner.style.animationDuration = '';
    scheduleNewsAdvance();
  }
}

function scheduleNewsAdvance() {
  if (_newsStayTimer) { clearTimeout(_newsStayTimer); _newsStayTimer = null; }
  if (_newsPaused || _newsItems.length < 2) return;
  _newsStayTimer = setTimeout(function() { _newsStayTimer = null; newsNext(); }, 4000);
}

// 摘要滚动完成 → 切下一条（滚动模式的推进唯一驱动，替代原固定 8s 定时器）
document.addEventListener('animationend', function(e) {
  if (e.target && e.target.id === 'newsSummaryText' && e.animationName === 'news-marquee') {
    // 仅一条快讯：滚动完成即静止停留，不循环回第一条重复显示
    if (_newsItems.length < 2) return;
    newsNext();
  }
});

function renderNews() {
  var cur = document.getElementById('newsCurrentText');
  var sum = _newsSummaryEl();
  var idx = document.getElementById('newsIdx');
  if (!cur || !sum || !idx) return;
  if (!_newsItems.length) {
    cur.textContent = '暂无快讯'; sum.textContent = ''; idx.textContent = '';
    return;
  }
  var it = _newsItems[_newsIdx];
  cur.textContent = (it && it.title) ? it.title : it;
  sum.textContent = (it && it.summary) ? it.summary : cur.textContent;
  idx.textContent = (_newsIdx + 1) + '/' + _newsItems.length;
  startNewsMarquee();
}

function newsNext() {
  if (_newsItems.length < 2) return;  // 单条时无下一条可切（滚动完成即静止）
  _newsIdx = (_newsIdx + 1) % _newsItems.length;
  _newsOpen = _newsIdx;
  renderNews();
  var m = document.getElementById('newsModal');
  if (m && !m.classList.contains('hidden')) renderNewsList();
}

function newsPrev() {
  if (_newsItems.length < 2) return;  // 单条时无上一条可切
  _newsIdx = (_newsIdx - 1 + _newsItems.length) % _newsItems.length;
  _newsOpen = _newsIdx;
  renderNews();
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
      (active ? 't-border-accent t-bg-accent-soft' : 'border-outline bg-surface') + '">' +
      '<button class="w-full text-left p-3 flex items-start gap-2" onclick="newsToggle(' + i + ')">' +
      '<span class="mt-0.5 w-5 h-5 shrink-0 flex items-center justify-center rounded bg-surface border border-outline font-data-sm text-[10px] text-on-surface-variant">' + (i + 1) + '</span>' +
      '<span class="flex-1 min-w-0">' +
      '<span class="block text-[13px] font-bold text-on-surface leading-snug line-clamp-2">' + _esc(title) + '</span>' +
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
  renderNews(); // 重启动画/停留计时（弹窗打开时保持暂停）
  renderNewsList();
}

function openNewsDetail() {
  if (!_newsItems.length) return;
  _newsPaused = true; // 弹窗打开时暂停推进，关闭后恢复
  if (_newsStayTimer) { clearTimeout(_newsStayTimer); _newsStayTimer = null; }
  var inner = _newsSummaryEl();
  if (inner) inner.style.animationPlayState = 'paused';
  _newsOpen = _newsIdx; // 打开弹窗默认展开当前轮播条
  renderNewsList();
  openModal('newsModal');
}

// 悬停暂停推进（动画暂停 / 停留计时挂起），移出后按模式恢复
(function() {
  var carousel = document.getElementById('newsCarousel');
  if (!carousel) return;
  carousel.addEventListener('mouseenter', function() {
    _newsPaused = true;
    if (_newsStayTimer) { clearTimeout(_newsStayTimer); _newsStayTimer = null; }
    var inner = _newsSummaryEl();
    if (inner) inner.style.animationPlayState = 'paused';
  });
  carousel.addEventListener('mouseleave', function() {
    _newsPaused = false;
    var inner = _newsSummaryEl();
    if (inner) {
      if (_newsScrolling) inner.style.animationPlayState = 'running';
      else scheduleNewsAdvance();
    }
  });
})();

renderNews();

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