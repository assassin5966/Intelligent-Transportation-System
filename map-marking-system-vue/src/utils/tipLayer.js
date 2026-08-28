/**
 * TipLayer —— 点位提示框（tip）DOM 层
 *
 * 职责：
 *   1. 在父容器（#container）内创建绝对定位的提示层，与地图同坐标系；
 *   2. 每个点位一张 tip 卡（HTML 由调用方提供）+ 一条 SVG 连接线；
 *   3. 连接线从 tip 卡边缘指向点位锚点（map.lngLatToContainer 投影坐标），
 *      且 tip 卡与锚点保持固定间距（gap），不遮挡点位本身；
 *   4. 卡片之间做碰撞避让（多轮两两分离）+ 视口裁剪，尽量互不重叠、不出界；
 *   5. 连接线颜色支持主题切换（setLineColor）；
 *   6. rAF 节流：高频调用 update() 时只保留最后一帧，避免闪烁卡顿；
 *   7. 卡片入场/离场动画：opacity + scale 过渡，平滑显示隐藏。
 *
 * 使用：
 *   const tl = new TipLayer({ lineColor: '#2f9bff', gap: 26 })
 *   tl.mount(containerEl)
 *   tl.update([{ id, x, y, html }])   // x/y 为锚点容器像素坐标
 *   tl.setLineColor('#00e1ff')
 *   tl.destroy()
 */
export class TipLayer {
  constructor(opts = {}) {
    this.lineColor = opts.lineColor || '#00e1ff' // 连接线颜色
    this.lineOpacity = opts.lineOpacity ?? 0.8   // 连接线透明度（80%）
    this.strokeWidth = opts.strokeWidth ?? 2     // 连接线线宽（2px 实线）
    this.gap = opts.gap ?? 26                    // tip 卡与点位锚点的间距（px）
    this.maxLine = opts.maxLine ?? null          // 连接线最大长度（null=按 gap 推导），null 时取 max(150, gap+100)
    this.zIndex = opts.zIndex ?? 9600            // 提示层 z 值（高于点位 marker）
    this.minimumSpacing = opts.minimumSpacing ?? 12 // 卡片间最小间距（px），避让时保证 ≥ 此值
    // —— 可逆性开关：true = 旧版贝塞尔曲线（锚点在坐标点），false = 新版直角 Π 折线（锚点在点位圆圈正上方）
    this.legacyMode = !!opts.legacyMode          // 连接线样式开关；true 恢复原始行为
    this.pinHeight = opts.pinHeight ?? 34        // 点位 pin 视觉高度（圆圈直径），用于将锚点对齐到圆圈正上方
    this.arrowGap = opts.arrowGap ?? 10          // 新版箭头尖端与 tip 卡上沿的间距
    this.elbowClear = opts.elbowClear ?? 24      // 新版水平段位于 tip 卡上沿之上的偏移
    this.minUpLen = opts.minUpLen ?? 60          // 新版向上段最小长度，保证 elbow 视觉可见
    this._root = null
    this._lines = null
    this._cards = null
    this._items = [] // [{ id, x, y, html, el, line, pinArrow, w, h, rx, ry, side, entered }]
    this._rafId = 0  // rAF 节流 ID
    this._pendingItems = null // 待处理的 items（rAF 节流用）
    this._sideMemo = new Map() // 方向记忆（id → side）：跨布局保持卡片方位稳定，防止地图操作时上下跳变
  }

  /** 挂载到父容器（重复调用只移动位置） */
  mount(parent) {
    if (!parent) return
    if (this._root) {
      if (this._root.parentNode !== parent) parent.appendChild(this._root)
      return
    }
    const root = document.createElement('div')
    root.className = 'tip-overlay'
    root.style.zIndex = String(this.zIndex)
    const ns = 'http://www.w3.org/2000/svg'
    const svg = document.createElementNS(ns, 'svg')
    svg.setAttribute('class', 'tip-lines')
    svg.setAttribute('preserveAspectRatio', 'none')
    // 定义箭头 marker：orient=auto 沿 line 方向自动旋转；refX=10 让尖端对齐 line 末端；
    // markerUnits=userSpaceOnUse 保证箭头尺寸固定（不随 stroke-width 缩放），屏幕缩放/适配下始终清晰
    // 两端箭头：marker-end（末端，指向 tip）+ marker-start（起点，指向点位）
    this._markerEndId = 'tip-arrow-end-' + Math.random().toString(36).slice(2, 9)
    this._markerStartId = 'tip-arrow-start-' + Math.random().toString(36).slice(2, 9)
    const defs = document.createElementNS(ns, 'defs')
    // 末端箭头（指向 tip 卡）：尖端在右(x=10)，12px 与 2px 线宽比例协调、清晰可见
    const markerEnd = document.createElementNS(ns, 'marker')
    markerEnd.setAttribute('id', this._markerEndId)
    markerEnd.setAttribute('viewBox', '0 0 10 10')
    markerEnd.setAttribute('refX', '10')
    markerEnd.setAttribute('refY', '5')
    markerEnd.setAttribute('markerWidth', '12')
    markerEnd.setAttribute('markerHeight', '12')
    markerEnd.setAttribute('orient', 'auto')
    markerEnd.setAttribute('markerUnits', 'userSpaceOnUse')
    const arrowEnd = document.createElementNS(ns, 'path')
    arrowEnd.setAttribute('d', 'M0,0 L10,5 L0,10 L2.5,5 Z') // 尖端在右(x=10)，带凹底更锋利
    arrowEnd.setAttribute('fill', this.lineColor)
    arrowEnd.setAttribute('fill-opacity', String(this.lineOpacity))
    markerEnd.appendChild(arrowEnd)
    defs.appendChild(markerEnd)
    // 起点箭头（指向点位）：尖端在左(x=0)，orient=auto 反向
    const markerStart = document.createElementNS(ns, 'marker')
    markerStart.setAttribute('id', this._markerStartId)
    markerStart.setAttribute('viewBox', '0 0 10 10')
    markerStart.setAttribute('refX', '0')
    markerStart.setAttribute('refY', '5')
    markerStart.setAttribute('markerWidth', '12')
    markerStart.setAttribute('markerHeight', '12')
    markerStart.setAttribute('orient', 'auto')
    markerStart.setAttribute('markerUnits', 'userSpaceOnUse')
    const arrowStart = document.createElementNS(ns, 'path')
    arrowStart.setAttribute('d', 'M10,0 L0,5 L10,10 L7.5,5 Z') // 尖端在左(x=0)，带凹底更锋利
    arrowStart.setAttribute('fill', this.lineColor)
    arrowStart.setAttribute('fill-opacity', String(this.lineOpacity))
    markerStart.appendChild(arrowStart)
    defs.appendChild(markerStart)
    svg.appendChild(defs)
    this._defs = defs
    this._arrowEnd = arrowEnd
    this._arrowStart = arrowStart
    const cards = document.createElement('div')
    cards.className = 'tip-cards'
    root.appendChild(svg)
    root.appendChild(cards)
    parent.appendChild(root)
    this._root = root
    this._lines = svg
    this._cards = cards
  }

  /**
   * 更新提示层（rAF 节流版）。
   * - 高频调用时只保留最后一帧，避免频繁 reflow 导致闪烁；
   * - 若传入的 items 与上一次内容一致（id + html），只更新锚点位置与连线（轻量��；
   * - 若内容有变化，先重建卡片 DOM 再布局。
   */
  update(items) {
    if (!this._root) return
    this._pendingItems = items
    if (this._rafId) return
    this._rafId = requestAnimationFrame(() => {
      this._rafId = 0
      if (!this._pendingItems) return
      this._doUpdate(this._pendingItems)
      this._pendingItems = null
    })
  }

  /** 实际执行更新逻辑（由 rAF 回调） */
  _doUpdate(items) {
    const sameContent =
      this._items.length === items.length &&
      this._items.every((it, i) => it.id === items[i].id && it.html === items[i].html)
    if (sameContent) {
      this._items.forEach((it, i) => {
        it.x = items[i].x
        it.y = items[i].y
      })
    } else {
      this._rebuild(items)
    }
    this._layout()
  }

  /** 内容变化时重建全部卡片 DOM 与连接线 */
  _rebuild(items) {
    this._cards.innerHTML = ''
    this._lines.innerHTML = ''
    // 关键修复：innerHTML='' 会连带清掉 <defs>（两端箭头 marker 定义），
    // 导致 url(#tip-arrow-*) 引用失效、箭头消失 —— 每次重建后必须把 defs 挂回 SVG。
    if (this._defs) this._lines.appendChild(this._defs)
    const ns = 'http://www.w3.org/2000/svg'
    this._items = items.map((it) => {
      const el = document.createElement('div')
      el.className = 'tip-card tip-card-enter'
      el.innerHTML = it.html
      this._cards.appendChild(el)
      // 触发入场动画：下一帧移除 enter 类
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          el.classList.remove('tip-card-enter')
        })
      })
      // path 代替 polyline：支持二次贝塞尔曲线（车道偏移避让，避免线条交叉重叠），fill=none 避免默认填充，linejoin=round 拐角圆滑
      // 线宽 2px 实线 + 80% 透明度 + 末端箭头（marker-end 指向 tip）
      // pin 端箭头改用独立 <polygon>，确保始终朝下指向点位（不依赖路径切线方向，对 4 个 side 都正确）
      const line = document.createElementNS(ns, 'path')
      line.setAttribute('class', 'tip-line')
      line.setAttribute('stroke', this.lineColor)
      line.setAttribute('stroke-width', String(this.strokeWidth))
      line.setAttribute('stroke-opacity', String(this.lineOpacity))
      line.setAttribute('fill', 'none')
      line.setAttribute('stroke-linejoin', 'round')
      line.setAttribute('stroke-linecap', 'round')
      line.setAttribute('marker-end', `url(#${this._markerEndId})`)
      this._lines.appendChild(line)
      // pin 端独立箭头（始终朝下指向点位，不受 path 切线影响）
      const pinArrow = document.createElementNS(ns, 'polygon')
      pinArrow.setAttribute('class', 'tip-pin-arrow')
      pinArrow.setAttribute('fill', this.lineColor)
      pinArrow.setAttribute('fill-opacity', String(this.lineOpacity))
      this._lines.appendChild(pinArrow)
      return {
        id: it.id, x: it.x, y: it.y, html: it.html,
        el, line, pinArrow, w: 0, h: 0, rx: 0, ry: 0, side: 'top'
      }
    })
  }

  /**
   * 布局：期望位置 -> 智能方向选择 -> 卡片碰撞避让(带引线长度上限) -> 视口裁剪 -> 应用位置 -> 重绘连接线
   * 关键约束：
   *   - 卡边与点位锚点始终保持 ≥ gap 的间距，点位永不被遮挡；
   *   - 碰撞避让每轮后将卡拉回"引线长度 ∈ [gap, maxLine]"内，确保连接线不过长、布局平衡；
   *   - 连接线样式由 `legacyMode` 控制：
   *       true  → 二次贝塞尔曲线 + 车道偏移（旧版，锚点在坐标点）；
   *       false → 直角 Π 折线（新版，锚点在点位圆圈正上方，tip 卡位于连接线下方、不被遮挡）。
   */
  _layout() {
    const root = this._root
    const W = root.clientWidth || root.parentNode?.clientWidth || 800
    const H = root.clientHeight || root.parentNode?.clientHeight || 600
    const gap = this.gap
    const items = this._items
    if (!items.length) return

    // —— 0) 智能方向预分配（按锚点在视口中的"象限位置"决定初次方向，避免全部堆叠在点位的同一边） ——
    // 规则：
    //   · 锚点偏左 → 卡片放右侧（side=right），避免溢出左边
    //   · 锚点偏右 → 卡片放左侧（side=left），避免溢出右边
    //   · 锚点靠近顶部 → 卡片放下方（side=bottom）
    //   · 锚点靠近底部 → 卡片放上方（side=top）
    //   · 中间区域 → 默认上方（side=top）
    //   · 当周围 60px 范围内已有同侧卡片时，反转方向形成交错
    items.forEach((it) => {
      it.w = it.el.offsetWidth || 300
      it.h = it.el.offsetHeight || 120

      const distTop = it.y          // 锚点到顶部
      const distBot = H - it.y      // 锚点到底部
      const distLeft = it.x         // 锚点到左边
      const distRight = W - it.x    // 锚点到右边

      // —— 方向记忆优先：上次布局的 side 若在当前视口仍放得下，直接沿用 ——
      //    这是布局稳定性的核心：地图平移/缩放只改变锚点像素坐标，
      //    卡片方位不重新洗牌，杜绝"每次操作卡片上下左右跳变"。
      const memoSide = this._sideMemo.get(it.id)
      const memoFits =
        memoSide === 'top' ? distTop >= it.h + gap + 8 :
        memoSide === 'bottom' ? distBot >= it.h + gap + 8 :
        memoSide === 'right' ? distRight >= it.w + gap + 8 :
        memoSide === 'left' ? distLeft >= it.w + gap + 8 : false

      let preferSide = 'top'        // 中部默认放上方（历史默认）
      if (memoSide && memoFits) {
        preferSide = memoSide
      } else if (distTop < it.h + gap + 8) {
        // 顶部空间不足 → 改放下方
        preferSide = 'bottom'
      } else if (distBot < it.h + gap + 8) {
        // 底部空间不足 → 放上方
        preferSide = 'top'
      } else if (it.x < W * 0.25) {
        // 锚点偏左 → 放右侧（卡片向右伸，避免左边框挤压）
        preferSide = 'right'
      } else if (it.x > W * 0.75) {
        // 锚点偏右 → 放左侧
        preferSide = 'left'
      }

      it.side = preferSide
      it.ux = 0; it.uy = -1; it.halfAlong = it.h / 2

      // —— 按方向计算初始位置 ——
      let x, y
      if (preferSide === 'top') {
        x = it.x - it.w / 2
        y = it.y - gap - it.h
      } else if (preferSide === 'bottom') {
        x = it.x - it.w / 2
        y = it.y + gap
      } else if (preferSide === 'right') {
        x = it.x + gap
        y = it.y - it.h / 2
        it.ux = 1; it.uy = 0; it.halfAlong = it.w / 2
      } else { // left
        x = it.x - gap - it.w
        y = it.y - it.h / 2
        it.ux = -1; it.uy = 0; it.halfAlong = it.w / 2
      }

      // 视口约束（含 ±6px 内边距）
      if (x < 6) x = 6
      if (x + it.w > W - 6) x = Math.max(6, W - it.w - 6)
      if (y < 6) y = 6
      if (y + it.h > H - 6) y = Math.max(6, H - it.h - 6)

      it.rx = x
      it.ry = y
      it.idealX = x
      it.idealY = y
      it.idealSide = preferSide
      // 锚点像素坐标（连线起点）：
      //   legacyMode=true → 锚点在坐标点本身（旧行为）；
      //   top/left/right → 锚点在 pin 圆圈正上方（连线从圆圈顶部出发）；
      //   bottom → 锚点在 pin 圆圈正下方（否则连线从上方出发会纵向穿过圆圈）。
      it.ax = it.x
      it.ay = this.legacyMode
        ? it.y
        : (preferSide === 'bottom' ? it.y + this.pinHeight : it.y - this.pinHeight)
    })

    // —— 1.5) 邻近交错：把"挨得很近且方向相同的点位"反向，形成上下/左右交错 ——
    //   仅在首次布局（无方向记忆）时运行；有记忆后跳过，保证地图操作时方位稳定不反复洗牌。
    if (this._sideMemo.size === 0) {
      const itemsByXY = items.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x))
      itemsByXY.forEach((cur) => {
        for (const other of itemsByXY) {
          if (other === cur) continue
          const d = Math.hypot(cur.ax - other.ax, cur.ay - other.ay)
          if (d > 90) break   // 已排序，第一个 >90 后跳出
          if (other.idealSide === cur.idealSide) {
            // 反转 other 的方向（朝向距离自己较远的空间）
            if (cur.idealSide === 'top' || cur.idealSide === 'bottom') {
              other.idealSide = (cur.idealSide === 'top') ? 'bottom' : 'top'
            } else {
              other.idealSide = (cur.idealSide === 'left') ? 'right' : 'left'
            }
          }
        }
      })
    }
    // 同步最终方向：锚点位置（bottom 在 pin 下方）+ 外推向量 + 期望位置（碰撞避让起点）
    items.forEach((it) => {
      const s = it.idealSide
      it.side = s
      it.ay = this.legacyMode ? it.y : (s === 'bottom' ? it.y + this.pinHeight : it.y - this.pinHeight)
      if (s === 'top') { it.ux = 0; it.uy = -1; it.halfAlong = it.h / 2 }
      else if (s === 'bottom') { it.ux = 0; it.uy = 1; it.halfAlong = it.h / 2 }
      else if (s === 'right') { it.ux = 1; it.uy = 0; it.halfAlong = it.w / 2 }
      else { it.ux = -1; it.uy = 0; it.halfAlong = it.w / 2 }
      if (s === 'top') {
        it.rx = it.ax - it.w / 2
        it.ry = it.ay - gap - it.h
      } else if (s === 'bottom') {
        it.rx = it.ax - it.w / 2
        it.ry = it.ay + gap
      } else if (s === 'right') {
        it.rx = it.ax + gap
        it.ry = it.ay - it.h / 2
      } else {
        it.rx = it.ax - gap - it.w
        it.ry = it.ay - it.h / 2
      }
      if (it.rx < 6) it.rx = 6
      if (it.rx + it.w > W - 6) it.rx = Math.max(6, W - it.w - 6)
      if (it.ry < 6) it.ry = 6
      if (it.ry + it.h > H - 6) it.ry = Math.max(6, H - it.h - 6)
    })

    // 2) 碰撞避让：多轮两两分离 + 严重重叠时切换方向；每轮后用引线长度约束把卡拉回合理半径
    //    本次优化：maxLine 改为可配置（opts.maxLine），初始层显式传更大值以加长连线、更加醒目；
    //    未传时回退到旧推导式 max(150, gap+100)，保持向后兼容。
    //    minimumSpacing：卡片间最小间距（px），避让时保证间距 ≥ 此值（需求 #3）。
    //    当重叠深度 > threshold 时，将卡片切换到对侧（top↔bottom / left↔right），彻底打破同侧堆叠。
    const maxLine = this.maxLine ?? Math.max(150, gap + 100)   // 引线最大长度：足够长以拉开间距，又不至过长失焦
    const minSp = this.minimumSpacing                          // 卡片间最小间距
    const SWITCH_THRESHOLD = 14                                // 重叠深度超过此值时切换 side
    for (let pass = 0; pass < 25; pass++) {
      const sorted = items.slice().sort((a, b) => a.ry - b.ry)
      let moved = false
      for (let i = 0; i < sorted.length; i++) {
        for (let j = i + 1; j < sorted.length; j++) {
          const a = sorted[i]
          const b = sorted[j]
          // 边界框重叠检测（含 minimumSpacing 缓冲）
          const ox = Math.min(a.rx + a.w, b.rx + b.w) - Math.max(a.rx, b.rx) + minSp
          const oy = Math.min(a.ry + a.h, b.ry + b.h) - Math.max(a.ry, b.ry) + minSp
          if (ox > 1 && oy > 1) {
            // 优先沿"占用空间小的方向"推开；选错方向的卡片会被推到对侧（侧滑避免一路推到屏幕外）
            if (ox < oy) {
              // 水平推开（保证间距 ≥ minSp）
              const push = ox / 2 + minSp / 2
              if (b.ax >= a.ax) b.rx += push
              else b.rx -= push
            } else {
              // 垂直推开（保证间距 ≥ minSp）
              b.ry = Math.min(b.ry + oy + minSp, H - b.h - 4)
              // 轻微水平错开，降低视觉重叠
              if (Math.abs(b.ax - a.ax) < 30) {
                b.rx += (b.ax >= a.ax ? 1 : -1) * Math.min(20, ox / 3)
              }
            }
            // 重叠深度过大 → 将 b 切换到对侧，从根本上拉开（避免只在同侧推越推越挤）
            const overlapDepth = Math.min(ox, oy)
            if (overlapDepth > SWITCH_THRESHOLD && b.side === a.side) {
              const oldSide = b.side
              if (oldSide === 'top' || oldSide === 'bottom') {
                // 优先顶/底切换，仅在视口另一侧空间更大时执行；锚点同步跟随（bottom 在 pin 下方）
                const newSide = oldSide === 'top' ? 'bottom' : 'top'
                const nay = this.legacyMode ? b.y : (newSide === 'bottom' ? b.y + this.pinHeight : b.y - this.pinHeight)
                const ny = newSide === 'bottom' ? nay + gap : nay - gap - b.h
                if (ny >= 6 && ny + b.h <= H - 6 && Math.abs(ny - b.ry) > overlapDepth) {
                  b.ry = ny
                  b.side = newSide
                  b.ay = nay
                  b.ux = 0
                  b.uy = newSide === 'top' ? -1 : 1
                  b.halfAlong = b.h / 2
                }
              } else {
                const nx = oldSide === 'left' ? b.ax + gap : b.ax - gap - b.w
                const newSide = oldSide === 'left' ? 'right' : 'left'
                if (nx >= 6 && nx + b.w <= W - 6 && Math.abs(nx - b.rx) > overlapDepth) {
                  b.rx = nx
                  b.side = newSide
                  b.ux = newSide === 'right' ? 1 : -1
                  b.uy = 0
                  b.halfAlong = b.w / 2
                }
              }
            }
            moved = true
          }
          // 视口裁剪（在避让循环内即时约束）
          if (b.ry + b.h > H - 4) b.ry = H - b.h - 4
          if (b.ry < 4) b.ry = 4
          if (b.rx + b.w > W - 4) b.rx = W - b.w - 4
          if (b.rx < 4) b.rx = 4
          // 引线长度约束：将卡拉回 [gap, maxLine] 半径内（核心需求 #1/#3/#4）
          // 力度软化为 50%：与推开互相打架时让步，最终不重叠由下方 Phase B 兜底保证
          const cx = b.rx + b.w / 2
          const cy = b.ry + b.h / 2
          const proj = (cx - b.ax) * b.ux + (cy - b.ay) * b.uy
          const L = proj - b.halfAlong   // 锚点到卡最近边的实际距离（即连线长度）
          if (L > maxLine) {
            const corr = (L - maxLine) * 0.5
            b.rx -= b.ux * corr
            b.ry -= b.uy * corr
          } else if (L < gap) {
            const corr = (gap - L) * 0.5
            b.rx += b.ux * corr
            b.ry += b.uy * corr
          }
        }
      }
      if (!moved) break
    }

    // —— 2.5) Phase B 兜底分离：不重叠优先 ——
    //   主循环受 maxLine 引线约束，密集区域可能"推开 → 被拉回"残留重叠；
    //   此阶段移除 maxLine 约束，只做纯推开 + 视口裁剪，确保最终卡片互不重叠。
    //   连接线允许变长（Π 折线自动跟随卡片位置），可读性优先于连线紧凑。
    for (let pass = 0; pass < 30; pass++) {
      const sortedB = items.slice().sort((a, b) => a.ry - b.ry)
      let movedB = false
      for (let i = 0; i < sortedB.length; i++) {
        for (let j = i + 1; j < sortedB.length; j++) {
          const a = sortedB[i]
          const b = sortedB[j]
          const ox = Math.min(a.rx + a.w, b.rx + b.w) - Math.max(a.rx, b.rx) + minSp
          const oy = Math.min(a.ry + a.h, b.ry + b.h) - Math.max(a.ry, b.ry) + minSp
          if (ox > 1 && oy > 1) {
            if (ox < oy) {
              // 水平推开：沿锚点相对方向分离，各让一半
              const push = ox / 2 + minSp / 2
              const dir = b.ax >= a.ax ? 1 : -1
              b.rx += dir * push
              a.rx -= dir * push
            } else {
              // 垂直推开：a（上方）不动、b（下方）下移全量，更符合从上往下堆叠的阅读顺序
              b.ry += oy + minSp / 2
              // 轻微水平错开，避免视口高度不足时纯垂直堆叠挤爆
              if (Math.abs(b.ax - a.ax) < 40) {
                b.rx += (b.ax >= a.ax ? 1 : -1) * Math.min(24, ox / 3 + 6)
              }
            }
            movedB = true
          }
        }
      }
      // 每轮统一做视口裁剪（上下左右边界）
      items.forEach((it) => {
        if (it.ry + it.h > H - 4) it.ry = H - it.h - 4
        if (it.ry < 4) it.ry = 4
        if (it.rx + it.w > W - 4) it.rx = W - it.w - 4
        if (it.rx < 4) it.rx = 4
      })
      if (!movedB) break
    }

    // 3) 应用位置（transform 提升性能，避免频繁触发回流）
    items.forEach((it) => {
      it.el.style.transform = `translate(${Math.round(it.rx)}px, ${Math.round(it.ry)}px)`
    })

    // 4) 重绘连接线
    //    legacyMode = true  → 旧版贝塞尔曲线 + 车道偏移（保持原行为，便于一键恢复）
    //    legacyMode = false → 新版直角 Π 折线（锚点在点位圆圈正上方 → 水平段 → 短垂直段下落至 tip 卡上沿）
    //                        tip 卡始终位于连接线下方，箭头朝下指向 tip，不遮挡卡片内容。
    this._lines.setAttribute('viewBox', `0 0 ${W} ${H}`)
    this._lines.style.width = W + 'px'
    this._lines.style.height = H + 'px'

    if (this.legacyMode) {
      // —— 旧版：二次贝塞尔曲线 + 车道偏移避让 ——
      let gx = 0, gy = 0
      items.forEach((it) => { gx += it.ax; gy += it.ay })
      gx /= items.length
      gy /= items.length
      const sortedByX = items.slice().sort((a, b) => a.ax - b.ax)
      const laneOf = new Map()
      sortedByX.forEach((it, i) => laneOf.set(it, i))
      const BASE_OFFSET = 5
      const MAX_OFFSET  = 14
      items.forEach((it) => {
        const [ex, ey] = nearestPointOnRect(it.ax, it.ay, it.rx, it.ry, it.w, it.h)
        const dx = ex - it.ax
        const dy = ey - it.ay
        const len = Math.hypot(dx, dy) || 1
        const px = -dy / len
        const py = dx / len
        const mx = (it.ax + ex) / 2
        const my = (it.ay + ey) / 2
        const dotOut = (mx - gx) * px + (my - gy) * py
        const perpSign = dotOut >= 0 ? 1 : -1
        const lane = laneOf.get(it) || 0
        const laneSign = (lane % 2 === 0) ? 1 : -1
        const offsetMag = Math.min(MAX_OFFSET, BASE_OFFSET + Math.floor(lane / 2) * 2)
        const offset = perpSign * laneSign * offsetMag
        const cx = mx + px * offset
        const cy = my + py * offset
        it.line.setAttribute('d', `M ${it.ax},${it.ay} Q ${cx},${cy} ${ex},${ey}`)
      })
      return
    }

    // —— 新版：4 方向 cubic Bezier 曲线 ——
    //   几何：端点取卡片"朝向锚点"那一边上最靠近锚点的位置（向内收缩 18px），
    //         线更短更直，大幅减少横扫/穿越其他卡片造成的交叉缠绕；
    //   控制点两端各共享一坐标（与起点同 X / 与终点同 Y），产生水平/垂直切线 → 自然 S 曲线；
    //   pin 端切线指向卡片，末端切线指向卡片 → 末端箭头自动对齐卡片边缘。
    //   入场描绘动画只播一次（entered 标记）：后续布局（地图平移/缩放/旋转的重投影）
    //   只更新 path d，不再重放动画，消除"每次操作线条都被擦除重画"的闪烁。
    const arrowGap = this.arrowGap
    const pinArrowHalfW = 5              // pin 箭头三角形半宽（与 2px 线宽比例协调）
    const pinArrowH = 8                  // pin 箭头高度
    const clampV = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

    items.forEach((it) => {
      const ax = it.ax
      const ay = it.ay                         // 已按方向对齐（top/left/right 在圆圈上方，bottom 在圆圈下方）
      const r = it.rx, t = it.ry
      const w = it.w, h = it.h
      const s = it.side

      // —— pin 端箭头：apex 始终指向 pin 圆圈（上方锚点朝下 / 下方锚点朝上） ——
      if (s === 'bottom') {
        it.pinArrow.setAttribute(
          'points',
          `${ax},${ay - pinArrowH} ${ax - pinArrowHalfW},${ay} ${ax + pinArrowHalfW},${ay}`
        )
      } else {
        it.pinArrow.setAttribute(
          'points',
          `${ax},${ay + pinArrowH} ${ax - pinArrowHalfW},${ay} ${ax + pinArrowHalfW},${ay}`
        )
      }

      // —— 4 方向 cubic Bezier（端点向锚点一侧收缩，线短且不横扫） ——
      let d
      if (s === 'top') {
        // 卡片在锚点上方：终点在卡片下边上靠近锚点处
        const ex = clampV(ax, r + 18, r + w - 18)
        const ey = t + h + arrowGap
        const midY = (ay + ey) / 2
        d = `M ${ax},${ay} C ${ax},${midY} ${ex},${midY} ${ex},${ey}`
      } else if (s === 'bottom') {
        // 卡片在锚点下方：终点在卡片上边上靠近锚点处
        const ex = clampV(ax, r + 18, r + w - 18)
        const ey = t - arrowGap
        const midY = (ay + ey) / 2
        d = `M ${ax},${ay} C ${ax},${midY} ${ex},${midY} ${ex},${ey}`
      } else if (s === 'right') {
        // 卡片在锚点右侧：终点在卡片左边上靠近锚点处
        const ex = r - arrowGap
        const ey = clampV(ay, t + 14, t + h - 14)
        const midX = (ax + ex) / 2
        d = `M ${ax},${ay} C ${midX},${ay} ${midX},${ey} ${ex},${ey}`
      } else {
        // 卡片在锚点左侧（side='left'）：终点在卡片右边上靠近锚点处
        const ex = r + w + arrowGap
        const ey = clampV(ay, t + 14, t + h - 14)
        const midX = (ax + ex) / 2
        d = `M ${ax},${ay} C ${midX},${ay} ${midX},${ey} ${ex},${ey}`
      }
      it.line.setAttribute('d', d)

      // —— 入场描绘动画：只在卡片首次出现时播放一次 ——
      //    播完后移除 dash 设置，后续 d 更新不受旧长度裁剪、即时呈现（与卡片吸附同步）。
      if (!it.entered) {
        it.entered = true
        const totalLen = it.line.getTotalLength()
        it.line.style.strokeDasharray = String(totalLen)
        it.line.style.strokeDashoffset = String(totalLen)
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            it.line.style.strokeDashoffset = '0'
            // 过渡（.55s）播完后清掉 dash，避免后续 path 更新被旧总长截断
            setTimeout(() => {
              it.line.style.strokeDasharray = 'none'
              it.line.style.strokeDashoffset = '0'
            }, 620)
          })
        })
      }
    })

    // —— 记录本次最终方向：下次布局优先沿用，保持卡片方位跨布局稳定 ——
    this._sideMemo.clear()
    items.forEach((it) => this._sideMemo.set(it.id, it.side))
  }

  /** 主题切换：更新连接线颜色 + 两端箭头填充（含透明度） */
  setLineColor(color) {
    this.lineColor = color
    this._items.forEach((it) => {
      it.line.setAttribute('stroke', color)
      it.pinArrow.setAttribute('fill', color)
      it.pinArrow.setAttribute('fill-opacity', String(this.lineOpacity))
    })
    if (this._arrowEnd) {
      this._arrowEnd.setAttribute('fill', color)
      this._arrowEnd.setAttribute('fill-opacity', String(this.lineOpacity))
    }
  }

  /**
   * 实时刷新已渲染卡片内的数值文本（不重建 DOM，避免闪烁）。
   * @param {Function} cb (cardEl, id) => void  —— cardEl 为卡片根元素，id 为其 data-id
   */
  updateFlowText(cb) {
    if (!this._cards) return
    this._cards.querySelectorAll('.cam-flow').forEach((el) => {
      const id = el.getAttribute('data-id')
      if (id != null) cb(el, id)
    })
  }

  /** 动态调整 tip 卡与锚点的间距（点击拉远、关闭恢复） */
  setGap(g) {
    this.gap = g
    if (this._items.length) this._layout()
  }

  /** 销毁：取消 rAF + 移除提示层 DOM */
  destroy() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId)
      this._rafId = 0
    }
    this._pendingItems = null
    if (this._root && this._root.parentNode) {
      this._root.parentNode.removeChild(this._root)
    }
    this._root = null
    this._lines = null
    this._cards = null
    this._items = []
  }
}

/** 求点 (px,py) 到矩形 [rx,ry,rx+w,ry+h] 边界上的最近点（用于连接线端点） */
function nearestPointOnRect(px, py, rx, ry, w, h) {
  const cx = Math.max(rx, Math.min(px, rx + w))
  const cy = Math.max(ry, Math.min(py, ry + h))
  if (cx === px && cy === py) {
    const dl = px - rx
    const dr = rx + w - px
    const dt = py - ry
    const db = ry + h - py
    const m = Math.min(dl, dr, dt, db)
    if (m === dl) return [rx, py]
    if (m === dr) return [rx + w, py]
    if (m === dt) return [px, ry]
    return [px, ry + h]
  }
  return [cx, cy]
}
