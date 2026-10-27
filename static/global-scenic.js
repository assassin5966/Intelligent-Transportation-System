/* 全局景区选项 (运维工具各页面右上角统一的查询条件)
 *
 * 数据来源: GET /api/intersections/scenic-options
 *   选项 = 基线(大同古城 / 云冈石窟 / 悬空寺)
 *        ∪ device_info.region   (设备信息表的「区域」)
 *        ∪ intersection_info.scenic (路口数据卡的「景区」)
 * 两处取值在库里各存各列 (region / scenic)，前端统一展示为「景区」。
 *
 * 选中值存 localStorage('ops.globalScenic')，跨页面保持。
 *
 * 页面可选的钩子 (不实现则只显示下拉，不做筛选):
 *   window.globalScenicApiBase()          -> 返回本页后端地址 (可选)
 *   window.applyGlobalScenic(value)       -> 按景区筛选本页数据 ('' = 全部)
 *   window.onScenicOptionsLoaded(options) -> 选项加载完成后填充页面内的景区下拉
 * 页面可用 API:
 *   window.getGlobalScenic()              -> 当前选中的景区 ('' = 全部)
 *   window.getGlobalScenicOptions()       -> 选项数组副本
 *   window.fillScenicSelect(sel, value, allLabel) -> 填充任意 select
 *   window.loadGlobalScenicOptions()      -> 重新拉取选项
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'ops.globalScenic';
  var FALLBACK = ['大同古城', '云冈石窟', '悬空寺'];
  var options = FALLBACK.slice();

  function apiBase() {
    if (typeof window.globalScenicApiBase === 'function') {
      try {
        var b = String(window.globalScenicApiBase() || '').replace(/\/+$/, '');
        if (b) return b;
      } catch (e) { /* ignore */ }
    }
    if (window.OPS_CONFIG && window.OPS_CONFIG.API_BASE) {
      return String(window.OPS_CONFIG.API_BASE).replace(/\/+$/, '');
    }
    return window.location.origin;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function current() {
    try { return localStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
  }

  function save(v) {
    try { localStorage.setItem(STORAGE_KEY, v || ''); } catch (e) { /* ignore */ }
  }

  /* 填充 select: 首项为「全部景区」(allLabel 传 null 表示不要"全部") */
  function fill(select, value, allLabel) {
    if (!select) return;
    var html = '';
    if (allLabel !== null) {
      html += '<option value="">' + esc(allLabel == null ? '全部景区' : allLabel) + '</option>';
    }
    options.forEach(function (o) {
      html += '<option value="' + esc(o) + '">' + esc(o) + '</option>';
    });
    select.innerHTML = html;
    if (value && options.indexOf(value) >= 0) select.value = value;
  }

  /* 填充页面内「景区」选择器 (编辑用): 保证当前值在选项里, 否则追加一项避免显示为空 */
  function fillEditorSelect(select, value, allLabel) {
    if (!select) return;
    fill(select, value, allLabel);
    if (value && options.indexOf(value) < 0) {
      var o = document.createElement('option');
      o.value = value;
      o.textContent = value;
      select.appendChild(o);
      select.value = value;
    }
  }

  function renderGlobal() {
    var sel = document.getElementById('globalScenic');
    if (!sel) return;
    var v = current();
    fill(sel, v, '全部景区');
    if (v && options.indexOf(v) < 0) {
      sel.value = '';
      save('');
    }
    var cnt = document.getElementById('globalScenicCount');
    if (cnt) cnt.textContent = options.length ? '(' + options.length + ')' : '';
  }

  function apply(v) {
    if (typeof window.applyGlobalScenic === 'function') {
      try { window.applyGlobalScenic(v || ''); } catch (e) {
        console.error('[global-scenic] applyGlobalScenic 执行失败', e);
      }
    }
  }

  window.onGlobalScenicChange = function (v) {
    save(v);
    apply(v || '');
  };

  window.getGlobalScenic = current;
  window.getGlobalScenicOptions = function () { return options.slice(); };
  window.fillScenicSelect = fillEditorSelect;

  window.loadGlobalScenicOptions = function () {
    return fetch(apiBase() + '/api/intersections/scenic-options', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var list = (data && Array.isArray(data.options)) ? data.options.filter(Boolean) : [];
        if (list.length) options = list;
      })
      .catch(function (e) {
        console.warn('[global-scenic] 景区选项加载失败, 使用内置基线:', e && e.message);
      })
      .then(function () {
        renderGlobal();
        if (typeof window.onScenicOptionsLoaded === 'function') {
          try { window.onScenicOptionsLoaded(options.slice()); } catch (e) {
            console.error('[global-scenic] onScenicOptionsLoaded 执行失败', e);
          }
        }
        apply(current());
      });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', window.loadGlobalScenicOptions);
  } else {
    window.loadGlobalScenicOptions();
  }
})();
