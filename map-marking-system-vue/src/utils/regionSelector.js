/**
 * regionSelector —— 基于 Canvas 的「区域框选」工具（纯 JS、零依赖）
 * --------------------------------------------------------------------------
 * 能力：
 *   1) 鼠标 / 触摸在覆盖层上拖拽即可框选矩形区域，实时描边 + 半透明填充覆盖；
 *   2) 覆盖层保持半透明，框选区域下方原本的内容（标识、文字、图形等）依然可见；
 *   3) 拖动时边框实时跟随，松开后保留最终矩形；
 *   4) 多次框选默认「叠加」保留所有历史区域（mode:'add'），亦可设 'replace' 每次清空；
 *   5) 支持外部驱动：setRegions(arr) 直接画出指定矩形。
 *
 * 代码风格沿用参考文件 111.js：匿名 IIFE 包裹、var win = window、内置轻量工具函数，
 * 最后挂全局或 module.exports，并作为 ESM 默认导出供 Vue 项目 import。
 * --------------------------------------------------------------------------
 */
const RegionSelector = (function (global) {
  'use strict';

  var win = global;
  var doc = win.document;

  // —— 默认配置：实例化时可按需覆盖（如换成项目主题色）——
  var DEFAULTS = {
    parent: null,                                  // 挂载父节点，为 null 时挂到 body
    strokeColor: 'rgba(0, 225, 255, 0.9)',        // 框选边框颜色（与主题强调色一致）
    fillColor: 'rgba(0, 225, 255, 0.12)',         // 半透明填充（覆盖层保持半透明，下方内容仍可见）
    lineWidth: 2,                                 // 边框线宽
    dash: [7, 4],                                 // 虚线样式：[实线长度, 间隔长度]
    mode: 'add',                                  // 多次框选默认行为：'add' 叠加 | 'replace' 每次清空
    minSize: 4                                    // 小于该尺寸的拖拽视为误触，不生成区域
  };

  /** 轻量对象合并 */
  function extend() {
    var out = {};
    for (var i = 0; i < arguments.length; i++) {
      var src = arguments[i];
      if (src) for (var k in src) if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
    }
    return out;
  }

  /** 两个矩形是否相交 */
  function overlap(a, b) {
    return !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y);
  }

  function RegionSelector(options) {
    this.opts = extend({}, DEFAULTS, options || {});
    this.parent = this.opts.parent || doc.body;
    this.canvas = null;
    this.ctx = null;
    this._w = win.innerWidth;
    this._h = win.innerHeight;
    this.regions = [];   // 已完成区域集合（屏幕坐标 {x,y,w,h}）
    this.drawing = false;
    this.start = null;
    this.current = null;
    this._bound = false;
  }

  /** 懒创建 canvas 覆盖层并自适应尺寸（含高分屏 dpr 处理） */
  RegionSelector.prototype._ensure = function () {
    if (this.canvas) return;
    var self = this;
    this.canvas = doc.createElement('canvas');
    this.canvas.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;z-index:9999;';
    this.parent.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this._resize();
    win.addEventListener('resize', function () { self._resize(); });
  };

  /** 按父容器尺寸 / 设备像素比重设画布 */
  RegionSelector.prototype._resize = function () {
    if (!this.canvas) return;
    var dpr = win.devicePixelRatio || 1;
    this._w = this.parent.clientWidth || win.innerWidth;
    this._h = this.parent.clientHeight || win.innerHeight;
    this.canvas.width = this._w * dpr;
    this.canvas.height = this._h * dpr;
    this.canvas.style.width = this._w + 'px';
    this.canvas.style.height = this._h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.render();
  };

  /** 开启框选：绑定指针事件，覆盖层开始捕获输入 */
  RegionSelector.prototype.enable = function () {
    this._ensure();
    this.canvas.style.pointerEvents = 'auto';
    var self = this;
    if (!this._bound) {
      this._onDown = function (e) { self._down(e); };
      this._onMove = function (e) { self._move(e); };
      this._onUp = function () { self._up(); };
      this.canvas.addEventListener('pointerdown', this._onDown);
      win.addEventListener('pointermove', this._onMove);
      win.addEventListener('pointerup', this._onUp);
      this._bound = true;
    }
  };

  /** 关闭框选 */
  RegionSelector.prototype.disable = function () {
    if (!this.canvas) return;
    this.canvas.style.pointerEvents = 'none';
    if (this._bound) {
      this.canvas.removeEventListener('pointerdown', this._onDown);
      win.removeEventListener('pointermove', this._onMove);
      win.removeEventListener('pointerup', this._onUp);
      this._bound = false;
    }
    this.drawing = false;
    this.current = null;
  };

  RegionSelector.prototype.toggle = function () {
    if (this._bound) this.disable(); else this.enable();
  };

  RegionSelector.prototype._down = function (e) {
    this.drawing = true;
    this.start = { x: e.clientX, y: e.clientY };
    this.current = { x: e.clientX, y: e.clientY, w: 0, h: 0 };
  };

  RegionSelector.prototype._move = function (e) {
    if (!this.drawing) return;
    var x = Math.min(this.start.x, e.clientX);
    var y = Math.min(this.start.y, e.clientY);
    var w = Math.abs(e.clientX - this.start.x);
    var h = Math.abs(e.clientY - this.start.y);
    this.current = { x: x, y: y, w: w, h: h };
    this.render();
  };

  RegionSelector.prototype._up = function () {
    if (!this.drawing) return;
    this.drawing = false;
    if (this.current && this.current.w >= this.opts.minSize && this.current.h >= this.opts.minSize) {
      if (this.opts.mode === 'replace') this.regions = [];
      this.regions.push(this.current);
    }
    this.current = null;
    this.render();
  };

  /** 绘制：虚线边框 + 半透明填充（保持下方内容可见） */
  RegionSelector.prototype.render = function () {
    if (!this.ctx) return;
    var ctx = this.ctx, o = this.opts;
    ctx.clearRect(0, 0, this._w, this._h);
    var all = this.current ? this.regions.concat([this.current]) : this.regions;
    var self = this;
    all.forEach(function (r) {
      // 半透明填充（覆盖层，下方内容仍可见）
      ctx.save();
      ctx.fillStyle = o.fillColor;
      ctx.fillRect(r.x, r.y, r.w, r.h);
      // 虚线边框
      ctx.strokeStyle = o.strokeColor;
      ctx.lineWidth = o.lineWidth;
      ctx.setLineDash(o.dash);
      ctx.lineJoin = 'round';
      ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
      ctx.restore();
    });
  };

  /** 清除全部区域 */
  RegionSelector.prototype.clear = function () {
    this._ensure();
    this.regions = [];
    this.current = null;
    this.render();
  };

  /** 导出当前所有区域（深拷贝，避免外部直接改内部状态） */
  RegionSelector.prototype.getRegions = function () {
    return this.regions.map(function (r) { return { x: r.x, y: r.y, w: r.w, h: r.h }; });
  };

  /** 用一组矩形替换当前显示区域（外部驱动：下拉选择区域后直接绘制） */
  RegionSelector.prototype.setRegions = function (arr) {
    this._ensure();
    this.regions = (arr || []).map(function (r) { return { x: r.x, y: r.y, w: r.w, h: r.h }; });
    this.render();
  };

  /** 追加一个区域（保留已有区域） */
  RegionSelector.prototype.addRegion = function (r) {
    this._ensure();
    this.regions.push({ x: r.x, y: r.y, w: r.w, h: r.h });
    this.render();
  };

  /** 彻底销毁：解绑事件并移除 canvas 覆盖层 */
  RegionSelector.prototype.destroy = function () {
    this.disable();
    if (this.canvas && this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
    this.canvas = this.ctx = null;
    this.regions = [];
    this.current = null;
  };

  // —— 暴露：优先 CommonJS（Vue 项目 import），否则挂全局（与 111.js 一致）——
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = RegionSelector;
  } else {
    win.RegionSelector = RegionSelector;
  }
  return RegionSelector;
})(typeof window !== 'undefined' ? window : this);

// ESM 默认导出（Vite 构建时 import RegionSelector from '...' 走这里）
export default RegionSelector;
