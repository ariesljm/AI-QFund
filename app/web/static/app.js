/* ===== AI-QFund 页面入口：通用工具 + Tab 编排（候选6 拆分） =====
   序列：app.js（本文件）→ js/manage.js → js/content.js。工具函数挂全局供两域共用。*/
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
  if (!today || !quality || !bToday || !bQuality) return;
  // 仅切换 active 类与 aria-current，不再整串覆盖 className：
  // 各模板（标准版/浅色版）可自定义 tab 视觉，避免切换后类名被重置跳动
  var toQuality = name === 'quality';
  today.classList.toggle('hidden', toQuality);
  quality.classList.toggle('hidden', !toQuality);
  bToday.classList.toggle('active', !toQuality);
  bQuality.classList.toggle('active', toQuality);
  if (toQuality) {
    bToday.removeAttribute('aria-current');
    bQuality.setAttribute('aria-current', 'page');
  } else {
    bQuality.removeAttribute('aria-current');
    bToday.setAttribute('aria-current', 'page');
  }
}

function val(id) { return document.getElementById(id).value; }

function _upd(el, v) { if (el) el.textContent = v; }

function _color(v, dir) {
  if (dir === 'flat') return 'text-on-surface-variant';
  return dir === 'up' ? 'text-up' : 'text-down';
}
