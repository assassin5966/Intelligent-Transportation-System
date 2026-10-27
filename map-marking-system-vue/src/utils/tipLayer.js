/**
 * TipLayer —— 点位提示框（tip）DOM 层
 *
 * 职责：
 *   1. 在父容器（#container）内创建绝对定位的提示层，与地图同坐标系；
 *   2. 每个点位一张 tip 卡（HTML 由调用方提供）+ 一条 SVG 连接线；
 *   3. 连接线从 tip 卡边缘指向点位锚点（map.lngLatToContainer 投影坐标），
 *      且 tip 卡与锚点保持固定间距（gap），不遮挡点位本身；
 *   4. 卡片之间做碰撞避让（多轮两两分离）+ 视口裁剪，尽量互不重叠、不出界；
 *   5. 布局模式可切换（opts.layoutMode）：
 *        'auto' —— 自动避让（默认，原行为）；
 *        'zone' —— 分区固定布局：调用方给每张卡一个 zone（left/right/top/bottom），
 *                  TipLayer 把同 zone 的卡片排成"左/右竖直单列、上/下水平多行"的固定通道，
 *                  通道位置以 setFrame() 传入的区域屏幕矩形为基准，位置确定、不互相推挤；
 *   6. 连接线颜色支持主题切换（setLineColor）；
 *   7. rAF 节流：高频调用 update() 时只保留最后一帧，避免闪烁卡顿；
 *   8. 卡片入场/离场动画：opacity + scale 过渡，平滑显示隐藏；
 *   9. 视口裁剪（viewportCull，anchor 布局）：锚点完全移出视口的卡片自动隐藏
 *      （DOM/数据保留，回到视口自动恢复），避免缩放到局部时卡片堆在视口边缘。
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
    this.showLines = opts.showLines !== false    // 是否绘制点位→tip 连接线（含 pin 箭头）；false = 纯悬浮卡片，不画任何连线/箭头
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
    // —— 布局模式 ——
    //   'auto'（默认）—— 自动避让布局（原有算法，保持向后兼容）
    //   'zone'        —— 分区固定布局：每张卡按 item.zone（left/right/top/bottom）
    //                    进入固定通道，位置完全确定、不参与自动避让（对应"古城四面图"）
    this.layoutMode = opts.layoutMode || 'auto'
    this.zoneSpacing = opts.zoneSpacing ?? 12    // 同一通道内相邻卡片的间距（px）
    this._frame = opts.frame || null             // 区域屏幕矩形 { x, y, w, h }，zone 模式定位基准
    // —— 视口裁剪（anchor 布局专用）——
    //   锚点（如框线「进/出」徽标）完全移出视口的卡片自动隐藏：DOM 与数据保持挂载
    //   （属性不丢、实时数值刷新不中断），锚点回到视口内自动恢复显示。
    //   场景：地图放大到单个城门时，其余门的卡不再被"钳位回视口"堆在屏幕边缘。
    this.viewportCull = !!opts.viewportCull      // 默认关闭，由调用方按层开启
    this.cullMargin = opts.cullMargin ?? 40      // 边缘余量（px）：锚点越界在此距离内仍显示（徽标擦边可见）
    this._root = null
    this._lines = null
    this._cards = null
    this._items = [] // [{ id, x, y, html, el, line, pinArrow, w, h, rx, ry, side, entered }]
    this._rafId = 0  // rAF 节流 ID
    this._pendingItems = null // 待处理的 items（rAF 节流用）
    this._sideMemo = new Map() // 方向记忆（id → side）：跨布局保持卡片方位稳定，防止地图操作时上下跳变
    this.cardScale = 1         // 卡片整体缩放系数（随地图缩放级别联动：与古城框线屏幕尺寸成正比）
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
        zone: it.zone,   // 分区布局方位（left/right/top/bottom），必须随卡片一起带上
        // anchor 布局的落位方位（top/bottom/left/right）：由调用方按
        // "大卡贴城墙框线内侧还是外侧"给出，例如北墙门贴内侧 → 卡片落在点位下方
        place: it.place,
        el, line, pinArrow, w: 0, h: 0, rx: 0, ry: 0, side: 'top'
      }
    })
  }

  /**
   * 布局总入口：测量 → 定位（两种模式二选一）→ 应用位置 → 重绘连接线。
   *
   * 两种定位模式：
   *   layoutMode = 'auto'（默认）—— 自动避让布局：
   *       期望位置 → 智能方向选择 → 卡片碰撞避让(带引线长度上限) → 视口裁剪；
   *       适用于点位分散、数量不多的场景（设备编辑标点、点击详情卡等）。
   *   layoutMode = 'zone' —— 分区固定布局（对应"古城四面图"排版）：
   *       每张卡按 zone 进入固定通道，左/右为竖直单列、上/下为水平多行，
   *       位置完全确定、不参与自动避让，依托 setFrame(区域屏幕矩形) 紧贴区域外侧。
   *
   * 关键约束：
   *   - 卡边与点位锚点始终保持 ≥ gap 的间距，点位永不被遮挡；
   *   - auto 模式下碰撞避让每轮后将卡拉回"引线长度 ∈ [gap, maxLine]"内，确保连接线不过长；
   *   - 连接线样式由 `legacyMode` 控制：
   *       true  → 二次贝塞尔曲线 + 车道偏移（旧版，锚点在坐标点）；
   *       false → 4 方向 cubic Bezier 曲线（新版，锚点在点位圆圈正上方/正下方）。
   */
  _layout() {
    const root = this._root
    const W = root.clientWidth || root.parentNode?.clientWidth || 800
    const H = root.clientHeight || root.parentNode?.clientHeight || 600
    const items = this._items
    if (!items.length) return

    this._measure(items)
    if (this.layoutMode === 'zone') this._placeZoneLayout(W, H)
    else if (this.layoutMode === 'anchor') this._placeAnchorLayout(W, H)
    else this._placeAutoLayout(W, H)
    this._applyPositions()
    this._drawLines(W, H)
  }

  /**
   * 统一测量卡片尺寸（两种布局模式共用）。
   * 注意：外层 .tip-card 被 CSS `width: stretch; max-width: min(500px, 94vw)` 撑满，
   * 真实可见的是内层卡片（.cam-flow 等，自带 max-width），因此以内层尺寸为准，
   * 否则分区通道会按 500px 留位、卡片之间出现大片空白。
   */
  _measure(items) {
    const s = this.cardScale
    items.forEach((it) => {
      const inner = it.el.firstElementChild
      it.w = (inner && inner.offsetWidth ? inner.offsetWidth * s : (it.w || 300 * s))
      it.h = (inner && inner.offsetHeight ? inner.offsetHeight * s : (it.h || 120 * s))
    })
  }

  /**
   * 自动避让布局（原算法，保持默认行为不变）。
   * 期望位置 -> 智能方向选择 -> 卡片碰撞避让(带引线长度上限) -> 视口裁剪 -> Phase B 兜底分离。
   */
  _placeAutoLayout(W, H) {
    const gap = this.gap
    const items = this._items

    // —— 0) 智能方向预分配（按锚点在视口中的"象限位置"决定初次方向，避免全部堆叠在点位的同一边） ——
    // 规则：
    //   · 锚点偏左 → 卡片放右侧（side=right），避免溢出左边
    //   · 锚点偏右 → 卡片放左侧（side=left），避免溢出右边
    //   · 锚点靠近顶部 → 卡片放下方（side=bottom）
    //   · 锚点靠近底部 → 卡片放上方（side=top）
    //   · 中间区域 → 默认上方（side=top）
    //   · 当周围 60px 范围内已有同侧卡片时，反转方向形成交错
    items.forEach((it) => {
      it.w = (it.el.offsetWidth || 300) * this.cardScale
      it.h = (it.el.offsetHeight || 120) * this.cardScale

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
  }

  /**
   * 分区固定布局：按 item.zone 把卡片摆进「左 / 右 / 上 / 下」四条专用通道。
   *
   *   · zone='left'   → 清远门（西墙）：竖直单列，整列贴西墙 pin 的**左（西）侧**
   *   · zone='right'  → 和阳门（东墙）：竖直单列，整列贴东墙 pin 的**右（东）侧**
   *   · zone='top'    → 武定门（北墙）：水平多行，整组贴北墙 pin 的**上（北）侧**
   *   · zone='bottom' → 永泰门（南墙）：水平多行，整组贴南墙 pin 的**下（南）侧**
   *
   * 自适应规则（保证任何窗口尺寸下都摆得下、且互不压叠）：
   *   1) 左右通道优先放"城市外侧"，若某一侧外扩空间不足（区域占满视口），
   *      左右**一起**翻到内侧（贴墙内侧排开），保持画面左右对称；
   *   2) 上下通道同理：北/南外侧放得下就贴外，放不下就翻到城墙内侧；
   *   3) 上下两组行优先收进"左右两列之间的空走廊"（列数按走廊宽度自动收敛），
   *      这样它们与左右两列在横向上天然错开、左右两列可用满整屏高度；
   *   4) 走廊过窄放不下时，行才铺满整屏宽度，此时左右两列改按纵向避让上下两组行；
   *   5) 空间仍不够则继续降级：压缩通道内间距 → 退回整屏高度。
   *
   * 通道内的卡片顺序由调用方决定（MapPanel 用 sortPointsForZoneLayout 排好：
   * 同城门按"北/南""西/东"分段 → 段内车卡在前便道在后 → 段内按坐标推进）。
   * 位置完全由本方法算出、不做两两避让，因此任何一次重排结果都稳定可预期。
   *
   * @param {number} W 容器宽（container 像素）
   * @param {number} H 容器高（container 像素）
   */
  _placeZoneLayout(W, H) {
    const items = this._items
    const gap = this.gap
    const sp = this.zoneSpacing
    const f = this._frame || { x: 0, y: 0, w: W, h: H }
    const ZONES = ['left', 'right', 'top', 'bottom']
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

    // 1) 按方位分组（zone 非法/缺省时归入 top，保证卡片一定有位置）
    const G = { left: [], right: [], top: [], bottom: [] }
    items.forEach((it) => {
      if (ZONES.indexOf(it.zone) === -1) it.zone = 'top'
      G[it.zone].push(it)
    })
    /** 某组锚点在容器内的取值区间 */
    const span = (g, axis) => {
      let lo = Infinity, hi = -Infinity
      g.forEach((it) => {
        const v = axis === 'x' ? it.x : it.y
        if (v < lo) lo = v
        if (v > hi) hi = v
      })
      return [lo, hi]
    }

    // 2) 左 / 右通道：先定水平通道（它决定中部走廊宽度），竖直位置留到第 4 步
    const band = { left: null, right: null }   // { x, w, side }
    const colW = { left: 0, right: 0 }
    const ax = { left: [0, 0], right: [W, W] }
    ;['left', 'right'].forEach((k) => {
      if (!G[k].length) return
      colW[k] = Math.max(...G[k].map((i) => i.w))
      ax[k] = span(G[k], 'x')
    })
    const fitOutL = G.left.length ? ax.left[0] - 4 >= colW.left + gap : true
    const fitOutR = G.right.length ? W - 4 - ax.right[1] >= colW.right + gap : true
    const outsideLR = fitOutL && fitOutR   // 左右"外侧/内侧"必须一致，避免画面一边内一边外
    if (G.left.length) {
      const raw = outsideLR ? ax.left[0] - gap - colW.left : ax.left[1] + gap
      band.left = {
        x: clamp(Math.round(raw), 4, Math.max(4, W - 4 - colW.left)),
        w: colW.left,
        side: outsideLR ? 'left' : 'right'
      }
    }
    if (G.right.length) {
      const raw = outsideLR ? ax.right[1] + gap : ax.right[0] - gap - colW.right
      band.right = {
        x: clamp(Math.round(raw), 4, Math.max(4, W - 4 - colW.right)),
        w: colW.right,
        side: outsideLR ? 'right' : 'left'
      }
    }
    const corridorL = band.left ? band.left.x + band.left.w + sp : 6
    const corridorR = band.right ? band.right.x - sp : W - 6

    // 3) 上 / 下通道：水平多行（默认 2 行铺开，放不下自动减列加行）
    const hBand = { top: null, bottom: null }  // { x, y, w, h, side, inCorridor }
    ;['top', 'bottom'].forEach((k) => {
      const g = G[k]
      if (!g.length) return
      const rowW = Math.max(...g.map((i) => i.w))
      const rowH = Math.max(...g.map((i) => i.h))
      const corridorW = Math.max(1, corridorR - corridorL)

      // 优先把整组卡塞进「左右两列之间」的空走廊 —— 这样左右两列可以吃满整屏高度且互不重叠；
      // 走廊太窄时（连一列都放不下）才放开到整屏宽度，改为让左右两列纵向避让上下两组行。
      let cols = Math.min(g.length, Math.max(1, Math.ceil(g.length / 2)))
      while (cols > 1 && cols * rowW + (cols - 1) * sp > corridorW) cols--
      let inCorridor = cols * rowW + (cols - 1) * sp <= corridorW
      if (!inCorridor) {
        cols = Math.min(g.length, Math.max(1, Math.ceil(g.length / 2)))
        while (cols > 1 && cols * rowW + (cols - 1) * sp > W - 8) cols--
      }
      const rows = Math.ceil(g.length / cols)
      const totalW = cols * rowW + (cols - 1) * sp
      const totalH = rows * rowH + (rows - 1) * sp

      const bx = clamp(
        Math.round(inCorridor
          ? corridorL + (corridorW - totalW) / 2
          : f.x + f.w / 2 - totalW / 2),
        4, Math.max(4, W - 4 - totalW)
      )

      const [lo, hi] = span(g, 'y')
      // 外侧（北墙朝北、南墙朝南）放得下就贴外，否则翻到城墙内侧
      const fitOut = k === 'top' ? (lo - 4 >= totalH + gap) : (H - 4 - hi >= totalH + gap)
      const side = k === 'top' ? (fitOut ? 'top' : 'bottom') : (fitOut ? 'bottom' : 'top')
      const yRaw = side === 'top' ? lo - gap - totalH : hi + gap
      const by = clamp(Math.round(yRaw), 4, Math.max(4, H - 4 - totalH))
      hBand[k] = { x: bx, y: by, w: totalW, h: totalH, side, inCorridor }

      g.forEach((it, i) => {
        const r = Math.floor(i / cols)
        const c = i % cols
        it.side = side
        it.ux = 0
        it.uy = side === 'top' ? -1 : 1
        it.halfAlong = it.h / 2
        it.rx = Math.round(bx + c * (rowW + sp) + (rowW - it.w) / 2)
        it.ry = Math.round(by + r * (rowH + sp) + (rowH - it.h) / 2)
        it.ax = it.x
        // 卡片在 pin 上方 → 锚点取圆圈上方；在 pin 下方 → 锚点取圆圈下方（连线不穿过圆圈）
        it.ay = side === 'bottom' ? it.y + this.pinHeight : it.y - this.pinHeight
      })
    })

    // 4) 左 / 右通道竖直落位。
    //    若上下两组行都收在走廊内（与左右列横向天然错开）→ 左右列可用满整屏高度；
    //    否则（走廊过窄、行被迫铺满全宽）→ 左右列纵向避让上下两组行，保证不压叠。
    const bandsInCorridor =
      (hBand.top ? hBand.top.inCorridor : true) &&
      (hBand.bottom ? hBand.bottom.inCorridor : true)
    const vTop = bandsInCorridor ? 6 : (hBand.top ? hBand.top.y + hBand.top.h + sp : 6)
    const vBot = bandsInCorridor ? H - 6 : (hBand.bottom ? hBand.bottom.y - sp : H - 6)
    ;['left', 'right'].forEach((k) => {
      const g = G[k]
      const b = band[k]
      if (!g.length || !b) return
      const n = g.length
      const sumH = g.reduce((s, i) => s + i.h, 0)
      let avail = vBot - vTop
      let baseY = vTop
      if (avail < sumH + sp * (n - 1)) {
        // 走廊高度不足：间距压缩；连卡片本体都放不下则退回整屏高度
        if (avail < sumH) { avail = H - 12; baseY = 6 }
      }
      let step = sp
      if (sumH + sp * (n - 1) > avail) {
        step = Math.max(2, Math.floor((avail - sumH) / Math.max(1, n - 1)))
      }
      const totalH = sumH + step * (n - 1)
      let y = clamp(Math.round(baseY + (avail - totalH) / 2), 6, Math.max(6, H - 6 - totalH))
      g.forEach((it) => {
        it.side = b.side
        it.ux = b.side === 'left' ? -1 : 1
        it.uy = 0
        it.halfAlong = it.w / 2
        // 贴外时左对齐 / 贴内时右对齐：同一通道内所有卡片朝区域那一侧边缘齐平
        it.rx = b.side === 'left' ? b.x : b.x + b.w - it.w
        it.ry = y
        it.ax = it.x
        it.ay = it.y - this.pinHeight
        y += it.h + step
      })
    })
  }

  /**
   * 锚点布局（anchor）：每张卡片按 item.place 贴附在对应地图点位的指定一侧。
   * 适用于「初始化 tip 各卡归位到自身点位」场景：8 个城门点位天然分散在古城四至，
   * 贴附展示即可，无需分区走廊；关闭连接线时卡片即悬浮在点位旁，视觉与交互保持统一。
   *
   * item.place 由调用方按「大卡贴城墙框线内侧 / 外侧」决定：
   *   'top'    卡片在点位上方（南墙门贴框线内侧）
   *   'bottom' 卡片在点位下方（北墙门贴框线内侧）
   *   'left'   卡片在点位左侧（西墙门贴框线外侧）
   *   'right'  卡片在点位右侧（东墙门贴框线外侧）
   * 仅做视口裁剪 + 同侧卡片纵向去重，避免极端缩放下的重叠；方位不会被自动翻转。
   */
  _placeAnchorLayout(W, H) {
    const gap = this.gap
    const items = this._items
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))
    const placeGap = Math.max(14, Math.round(gap * 0.6))   // 卡片与点位圆圈的最小间距
    const cullM = this.cullMargin
    const visible = []   // 视口内（参与定位/去重）的卡片
    items.forEach((it) => {
      // —— 视口裁剪（viewportCull）：锚点完全移出视口 → 本轮跳过该卡 ——
      //    只跳过定位与去重（不拆 DOM、不删数据，_applyPositions 里同步隐藏/恢复）；
      //    不参与去重很关键：被裁掉的卡若仍以"钳位回视口"的矩形参与碰撞，会把可见卡挤走。
      if (this.viewportCull) {
        it.culled = !(it.x > -cullM && it.x < W + cullM && it.y > -cullM && it.y < H + cullM)
        if (it.culled) return
      } else if (it.culled) {
        it.culled = false
      }
      // 尺寸取「内层可见卡片」（.rp-card 等自带固定宽高）：外层 .tip-card 是 width:stretch 的容器，
      // 直接量 outer 会得到容器宽度，导致锚点定位/去重全部错位。
      const inner = it.el.firstElementChild
      it.w = (inner && inner.offsetWidth ? inner.offsetWidth * this.cardScale : (it.w || 300 * this.cardScale))
      it.h = (inner && inner.offsetHeight ? inner.offsetHeight * this.cardScale : (it.h || 120 * this.cardScale))
      const place = (it.place === 'bottom' || it.place === 'left' || it.place === 'right')
        ? it.place : 'top'
      it.side = place
      let x, y
      if (place === 'bottom') {
        x = it.x - it.w / 2
        y = it.y + placeGap
      } else if (place === 'left') {
        x = it.x - placeGap - it.w
        y = it.y - it.h / 2
      } else if (place === 'right') {
        x = it.x + placeGap
        y = it.y - it.h / 2
      } else {
        x = it.x - it.w / 2
        y = it.y - placeGap - it.h
      }
      x = clamp(x, 6, Math.max(6, W - it.w - 6))
      y = clamp(y, 6, Math.max(6, H - it.h - 6))
      it.rx = x
      it.ry = y
      it.ax = it.x
      // 连线起点：底部放置取圆圈正下方，其余取正上方（线条不穿过 pin 圆圈）
      it.ay = place === 'bottom' ? it.y + this.pinHeight : it.y - this.pinHeight
      visible.push(it)
    })
    // 轻量去重：按 y 排序后，若与已放置卡片矩形相交则优先向下推开，触底则水平错开
    // （仅对视口内的卡；被裁剪隐藏的卡不参与碰撞）
    const placed = visible.sort((a, b) => (a.ry - b.ry) || (a.rx - b.rx))
    for (let i = 1; i < placed.length; i++) {
      const cur = placed[i]
      for (let j = 0; j < i; j++) {
        const o = placed[j]
        const ox = Math.min(cur.rx + cur.w, o.rx + o.w) - Math.max(cur.rx, o.rx)
        const oy = Math.min(cur.ry + cur.h, o.ry + o.h) - Math.max(cur.ry, o.ry)
        if (ox > 0 && oy > 0) {
          const down = o.ry + o.h + gap
          if (down + cur.h <= H - 6) {
            cur.ry = down
          } else {
            const right = o.rx + o.w + gap
            if (right + cur.w <= W - 6) { cur.rx = right; cur.ry = o.ry }
            else cur.ry = clamp(down, 6, Math.max(6, H - cur.h - 6))
          }
        }
      }
    }
  }

  /** 应用最终位置（transform 提升性能，避免频繁触发回流） */
  _applyPositions() {
    const s = this.cardScale
    this._items.forEach((it) => {
      // 视口裁剪：被裁掉的卡保留 DOM 与数据（属性在页面中），仅不可见、不响应指针；
      // 回到视口后恢复显示，位置/缩放由下方常规路径重设
      if (it.culled) {
        it.el.style.visibility = 'hidden'
        it.el.style.pointerEvents = 'none'
        if (it.line) it.line.style.display = 'none'
        if (it.pinArrow) it.pinArrow.style.display = 'none'
        return
      }
      it.el.style.visibility = ''
      it.el.style.pointerEvents = ''
      if (it.line) it.line.style.display = ''
      if (it.pinArrow) it.pinArrow.style.display = ''
      // transform-origin 必须是 0 0：rx/ry 已是"缩放后可视左上角"（_measure 用缩放尺寸算的布局），
      // 默认 origin(center) 会让可视位置整体偏移 W·(1-s)/2（500px 卡、s=0.38 时偏 155px，
      // 表现为西墙卡压住框线、东墙卡飘离锚点 190px 的不对称错位）
      it.el.style.transformOrigin = '0 0'
      it.el.style.transform = `translate(${Math.round(it.rx)}px, ${Math.round(it.ry)}px) scale(${s})`
    })
  }

  /** 重绘连接线（两种布局模式共用） */
  _drawLines(W, H) {
    const items = this._items

    // 关闭连接线：纯悬浮卡片模式（showLines=false）—— 隐藏整层 SVG，不绘制任何 path / 箭头
    if (!this.showLines) {
      if (this._lines) this._lines.style.display = 'none'
      return
    }
    if (this._lines) this._lines.style.display = ''

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
        if (it.culled) return   // 视口裁剪：隐藏的卡不画连接线（锚点/矩形均为陈旧值）
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
      if (it.culled) return   // 视口裁剪：隐藏的卡不画连接线/箭头（锚点/矩形均为陈旧值）
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
    // 兼容两种初始化 tip 卡结构：旧版 .cam-flow / 新版「四车道道路平面图」.rp-card
    this._cards.querySelectorAll('.cam-flow[data-id], .rp-card[data-id]').forEach((el) => {
      const id = el.getAttribute('data-id')
      if (id != null) cb(el, id)
    })
  }

  /** 动态调整 tip 卡与锚点的间距（点击拉远、关闭恢复） */
  setGap(g) {
    this.gap = g
    if (this._items.length) this._layout()
  }

  /**
   * 设置卡片整体缩放系数（随地图缩放级别联动，使大卡与古城框线等比放大缩小）。
   * 系数 = 2^(当前zoom − 城市基准zoom)：基准 zoom 时 = 1（设计尺寸），与框线 polygon 的
   * 屏幕尺寸严格成正比；放大时系数 > 1、缩小时 < 1。定位尺寸与 transform 同步乘此系数，
   * 卡片既跟着框线缩放，又不会与锚点/避让错位。
   * @param {number} f 缩放系数（非正/非有限值忽略）
   */
  setCardScale(f) {
    const v = Number(f)
    if (!Number.isFinite(v) || v <= 0) return
    if (v === this.cardScale) return
    this.cardScale = v
    // 仅重设 transform（定位尺寸在下一次 update/layout 时由 _measure 以新系数重算），轻量、可高频调用
    if (this._items.length) this._applyPositions()
  }

  /**
   * 设置"区域屏幕矩形"（container 像素坐标 { x, y, w, h }）。
   * zone 布局模式下，左/右/上/下四条卡片通道以此矩形为基准向外偏移，
   * 因此地图平移/缩放后需重新调用（MapPanel 在每次 update 前刷新），
   * 使四向卡片始终贴着区域边界外侧，而不是钉死在屏幕上。
   */
  setFrame(rect) {
    this._frame = (rect && Number.isFinite(rect.x) && Number.isFinite(rect.w)) ? rect : null
    if (this.layoutMode !== 'zone' || !this._items.length) return
    if (this._rafId) return   // 已有待执行的 update，复用那一帧即可，避免重复布局
    this._rafId = requestAnimationFrame(() => {
      this._rafId = 0
      this._layout()
    })
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
