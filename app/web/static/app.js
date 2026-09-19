/* ===== AI-QFund 页面入口：通用工具 + Tab 编排（候选6 拆分） =====
   序列：app.js（本文件）→ js/manage.js → js/content.js。工具函数挂全局供两域共用。*/
function _esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
var logInterval = null;
var _logCount = 0;
var _lastId = 0;
var _fetching = false;
var FUND_SKEL = '<div class="space-y-3"><div class="skel h-5 w-3/4"></div><div class="skel h-4 w-1/2"></div><div class="skel h-4 w-full"></div><div class="skel h-4 w-5/6"></div><div class="skel h-16 w-full"></div><div class="skel h-32 w-full"></div></div>';
function switchTab(name) {
  // 通用三 tab 切换：today / quality / logs；仅切 active + aria，不改 className 串（模板可自定义 tab 视觉）
  var tabs = { today: 'tab-today', quality: 'tab-quality', logs: 'tab-logs' };
  var btns = { today: 'tabBtn-today', quality: 'tabBtn-quality', logs: 'tabBtn-logs' };
  var prevActive = null;
  Object.keys(tabs).forEach(function(k) {
    var el = document.getElementById(tabs[k]);
    if (el && !el.classList.contains('hidden')) prevActive = k;
  });
  Object.keys(tabs).forEach(function(k) {
    var el = document.getElementById(tabs[k]);
    var b = document.getElementById(btns[k]);
    if (!el || !b) return;
    var on = (k === name);
    el.classList.toggle('hidden', !on);
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  // 系统日志 tab：切走停轮询，切到初始化轮询
  if (prevActive === 'logs' && name !== 'logs' && typeof cleanupLogs === 'function') cleanupLogs();
  if (name === 'logs' && typeof initLogs === 'function') initLogs();
}

function val(id) { return document.getElementById(id).value; }

function _upd(el, v) { if (el) el.textContent = v; }

function _color(v, dir) {
  if (dir === 'flat') return 'text-on-surface-variant';
  return dir === 'up' ? 'text-up' : 'text-down';
}
