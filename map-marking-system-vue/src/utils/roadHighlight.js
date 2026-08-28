/* ============================================================
 * [临时停用] 点位到点位路径高亮功能 —— 渲染器模块
 * ------------------------------------------------------------
 * 本文件已按需求临时整体注释（保留完整代码结构，未删除任何内容）。
 * 恢复方式：删除本文件第一行的注释开标记 与 最后一行的注释闭标记，
 *           即可恢复为普通 JS 模块。
 * 依赖说明：该模块由 src/components/MapPanel.vue 引用；
 *           配套的寻路链路 roadPath.js / roadNetwork.js 也已一并停用。
 * ============================================================
// 道路相邻连接高亮渲染器（数据驱动 + AMap.Object3D.MeshLine 3D 路标风格）
//
// 设计要点：
//   1) 数据驱动：点位数据中的 `neighbors` 字段定义相邻关系，渲染器只读取、不写死连接逻辑。
//      相邻关系由 buildAdjacencyEdges(points) 统一计算为去重无向边。
//   2) 3D 立体：AMap.Object3DLayer + AMap.Object3D.MeshLine，路面层 + 指引层双层，
//      通过 height 抬升形成「路面在下、指引在上」的层次与立体感。
//   3) 路标风格：指引层叠加流向箭头纹理（canvas 生成的 chevron），沿行进方向指示，
//      rAF 驱动 textureOffset 实现流光；主路（含卡口）更亮更宽更高，辅路更淡，形成差异化。
//   4) 可配置：base/guide/mainBase/mainGuide 四套样式（线宽/颜色/透明度/高度/单位），
//      styleFor(edge,a,b) 钩子支持按路段自定义；flowSpeed 控制流光速度。
//   5) 交互：叠加透明 Polyline 命中层，点击触发 onSelect（弹距离 InfoWindow），不阻塞地图操作。
//   兼容：AMap.Object3D 不可用时降级为 Polyline 双层叠加（保留路标观感与交互）。

// —— 默认配置（外部可通过 getRoadHighlight({ config: {...} }) 覆盖任意字段）——
const DEFAULT_CONFIG = {
  // 路面层（宽、暗、低透明 → 沥青路感）
  base:  { width: 7,   color: '#0c3450', opacity: 0.45, height: 16, unit: 'px' },
  // 指引层（窄、亮、箭头纹理 → 导航路标）
  guide: { width: 4,   color: '#00d2ff', opacity: 0.95, height: 28, unit: 'px', arrow: true },
  // 主路差异样式（端点含卡口 → 更亮更宽更高）
  mainBase:  { width: 9,   color: '#0e4060', opacity: 0.5,  height: 20, unit: 'px' },
  mainGuide: { width: 5,   color: '#36e0ff', opacity: 1,    height: 36, unit: 'px', arrow: true },
  flowSpeed: 0.5,        // textureOffset 每帧增量（箭头流动速度）
  zIndex: 9800,
  // 路段差异化样式钩子：默认按是否含卡口区分主/辅路
  styleFor(edge, a, b) {
    const isMain = a.type === '卡口' || b.type === '卡口'
    return isMain
      ? { base: this.mainBase, guide: this.mainGuide }
      : { base: this.base, guide: this.guide }
  }
}

// —— 经纬度距离（米）——
function haversineM(a, b) {
  const R = 6371000
  const rad = Math.PI / 180
  const lat1 = a[1] * rad, lat2 = b[1] * rad
  const dLat = lat2 - lat1
  const dLng = (b[0] - a[0]) * rad
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(s))
}

// —— 流向箭头纹理（透明底 + 朝右 chevron，沿路径 U 方向重复）——
let _arrowTex = null
function getArrowTexture() {
  if (_arrowTex) return _arrowTex
  if (typeof document === 'undefined') return null
  const w = 64, h = 32
  const cv = document.createElement('canvas')
  cv.width = w; cv.height = h
  const ctx = cv.getContext('2d')
  ctx.clearRect(0, 0, w, h)
  ctx.strokeStyle = '#ffffff'
  ctx.lineWidth = 7
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  // 在 U 方向平铺两个朝右的 chevron，使箭头沿行进方向重复出现
  for (const cx of [16, 48]) {
    ctx.beginPath()
    ctx.moveTo(cx - 11, 7)
    ctx.lineTo(cx + 9, h / 2)
    ctx.lineTo(cx - 11, h - 7)
    ctx.stroke()
  }
  _arrowTex = cv
  return _arrowTex
}

// —— 数据驱动：由点位 neighbors 计算去重无向边 ——
//   渲染器只消费此函数输出，绝不在内部写死任何连接关系。
export function buildAdjacencyEdges(points) {
  const byId = Object.fromEntries((points || []).map((p) => [p.id, p]))
  const seen = new Set()
  const edges = []
  for (const p of points || []) {
    const ns = p.neighbors || []
    for (const nid of ns) {
      const q = byId[nid]
      if (!q) continue // 邻居 id 不存在则跳过
      // 用 id 字典序生成稳定去重键，保证每条相邻边只渲染一次
      const key = p.id < nid ? `${p.id}|${nid}` : `${nid}|${p.id}`
      if (seen.has(key)) continue
      seen.add(key)
      edges.push({ id: key, a: p, b: q })
    }
  }
  return edges
}

class RoadHighlight {
  constructor(opts = {}) {
    this.config = Object.assign({}, DEFAULT_CONFIG, opts.config || {})
    this._map = null
    this._layer = null
    this._use3D = false
    this._groups = []      // [{ meshes:[], hit, info }]
    this._guides = []      // 指引层 mesh（用于流光动画）
    this._animating = false
    this._flowOffset = 0
    this._rafId = 0
    this._onSelect = opts.onSelect || null
    this._arrow = getArrowTexture()
  }

  // 装到地图上（创建 Object3DLayer；不可用时降级标志置位）
  mount(map) {
    this._map = map
    const M = window.AMap
    this._use3D = !!(M && M.Object3D && M.Object3D.MeshLine)
    if (this._use3D) {
      try {
        this._layer = new M.Object3DLayer({ zIndex: this.config.zIndex })
        map.add(this._layer)
      } catch (e) { this._use3D = false; this._layer = null }
    }
  }

  // 清空所有高亮
  clear() {
    this._stopFlow()
    for (const g of this._groups) {
      if (this._use3D && this._layer) {
        for (const m of g.meshes) { try { this._layer.remove(m) } catch (e) { } }
      } else if (this._map) {
        for (const m of g.meshes) { try { m.setMap(null) } catch (e) { } }
      }
      if (g.hit && this._map) { try { g.hit.setMap(null) } catch (e) { } }
    }
    this._groups = []
    this._guides = []
  }

  // 高亮一条相邻连接（a,b 为点位对象），返回 edge id 或 null
  highlightEdge(a, b) {
    if (!this._map || !a || !b || !a.coord || !b.coord) return null
    const M = window.AMap
    const path = [a.coord, b.coord]
    const lenMeters = haversineM(a.coord, b.coord)
    if (!(lenMeters > 0)) return null

    // 样式差异化（默认按主/辅路，可被 config.styleFor 覆盖）
    const st = (typeof this.config.styleFor === 'function')
      ? (this.config.styleFor({ a, b }, a, b) || {})
      : {}
    const baseStyle = st.base || this.config.base
    const guideStyle = st.guide || this.config.guide

    const info = {
      id: ++RoadHighlight._gid,
      descA: a.desc, descB: b.desc,
      coordA: a.coord, coordB: b.coord,
      distanceMeters: lenMeters,
      isMain: a.type === '卡口' || b.type === '卡口'
    }

    let meshes = []
    if (this._use3D) {
      // 3D：路面层 + 指引层（带箭头纹理、更高抬升）
      const base = this._makeMesh(path, baseStyle, lenMeters)
      const guide = this._makeMesh(path, guideStyle, lenMeters)
      meshes = [base, guide]
      this._layer.add(base)
      this._layer.add(guide)
      this._guides.push(guide)
    } else {
      // 降级：双层 Polyline（暗底 + 亮指引带方向箭头）
      const base = new M.Polyline({
        path, strokeColor: baseStyle.color, strokeWeight: baseStyle.width,
        strokeOpacity: baseStyle.opacity, lineCap: 'round', lineJoin: 'round',
        zIndex: this.config.zIndex, cursor: 'pointer'
      })
      base.setMap(this._map)
      const guide = new M.Polyline({
        path, strokeColor: guideStyle.color, strokeWeight: guideStyle.width,
        strokeOpacity: guideStyle.opacity, lineCap: 'round', lineJoin: 'round',
        zIndex: this.config.zIndex + 1, showDir: !!guideStyle.arrow, cursor: 'pointer'
      })
      guide.setMap(this._map)
      meshes = [base, guide]
    }

    // 透明命中层（可靠点击，不阻塞地图平移缩放）
    const hit = new M.Polyline({
      path,
      strokeColor: guideStyle.color,
      strokeOpacity: 0.01,
      strokeWeight: Math.max(baseStyle.width, guideStyle.width) + 8,
      lineCap: 'round', lineJoin: 'round',
      bubble: true, cursor: 'pointer',
      zIndex: this.config.zIndex - 100
    })
    hit.setMap(this._map)
    const onClick = () => {
      if (typeof this._onSelect === 'function') {
        try { this._onSelect(info) } catch (e) { // noop
        }
      }
    }
    hit.on('click', onClick)

    this._groups.push({ meshes, hit, info })
    if (!this._animating) this._startFlow()
    return info.id
  }

  // 构造一条 MeshLine
  _makeMesh(path, style, lenMeters) {
    const M = window.AMap
    const o = {
      path,
      color: style.color,
      opacity: style.opacity,
      width: style.width,
      height: style.height,
      unit: style.unit || 'px'
    }
    // 指引层叠加箭头纹理：纹理沿路径 U 方向重复，chevron 指向行进方向
    if (style.arrow && this._arrow) {
      o.texture = this._arrow
      // 让箭头密度与路段长度大致成正比（约每 80m 一个纹理周期 = 两个箭头）
      o.textureScale = Math.max(0.5, lenMeters / 80)
      o.textureOffset = 0
    }
    return new M.Object3D.MeshLine(o)
  }

  // 流光动画：rAF 循环推进纹理偏移，使箭头沿路径流动
  _startFlow() {
    if (this._animating) return
    this._animating = true
    const step = () => {
      this._flowOffset = (this._flowOffset + this.config.flowSpeed) % 1000
      for (const m of this._guides) {
        try { m.setOptions({ textureOffset: this._flowOffset }) }
        catch (e) { try { m.textureOffset = this._flowOffset } catch (e2) { // 静态箭头亦可
        } }
      }
      this._rafId = requestAnimationFrame(step)
    }
    this._rafId = requestAnimationFrame(step)
  }
  _stopFlow() {
    this._animating = false
    if (this._rafId) { try { cancelAnimationFrame(this._rafId) } catch (e) { } this._rafId = 0 }
  }

  // 卸载
  destroy() {
    this._stopFlow()
    this.clear()
    if (this._layer && this._map) { try { this._map.remove(this._layer) } catch (e) { } }
    this._layer = null
    this._map = null
    this._onSelect = null
  }

  // 当前激活的高亮条数
  get size() { return this._groups.length }

  // 暴露所有高亮信息（供面板查看 / 关闭）
  list() { return this._groups.map((g) => g.info) }
}
RoadHighlight._gid = 0

let _instance = null

export function getRoadHighlight(opts) {
  if (!_instance) _instance = new RoadHighlight(opts)
  return _instance
}

export { RoadHighlight }
*/
