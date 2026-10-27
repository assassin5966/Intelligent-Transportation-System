/**
 * 大同古城「八门」点位数据（道路平面图 · 8 个城门点位）
 * 数据源：设备信息汇总表 —— 古城八门经纬度（基于高德地图 GCJ-02）
 *
 * 字段说明：
 *   id         点位编号（MEN-xxx，城门点位）
 *   desc       点位名称（和阳北门 / 和阳南门 / 永泰东门 / 永泰西门 /
 *                          清远南门 / 清远北门 / 武定西门 / 武定东门）
 *   type       类型：便道（人行道）| 卡口（车辆卡口）
 *   gate       所属城门：和阳门 / 永泰门 / 清远门 / 武定门
 *   direction  出入口方向：入口 / 出口 / 其他（沿用原设备数据中的方向语义）
 *   coord      经纬度（[经度, 纬度]），取自「设备信息汇总表」GCJ-02 坐标
 *   cross      该门垂直投影到最近一条古城框线边上的交会点 [经度, 纬度]；
 *              框线「进出标识」就画在这里（把框线与点位的交叉段画成闸口）
 *   edge       该交会点所在的框线边：东墙 / 西墙 / 南墙 / 北墙（决定进出箭头朝向、
 *              道路走向、大卡贴城墙内侧还是外侧）
 *   ioKind     进出标识的人工判定值（可选，'in' | 'out'）：
 *              仅当 direction 为「其他」无法映射时使用。武定西门在原设备表中为「其他」，
 *              按「一门一进一出」规律（同门的另一侧武定东门 = 出口）推定为 'in'（入口）；
 *              如需推翻只改这一个字段，direction 的原始取值保持不动。
 *   lastActive 最近活跃日期（合成，用于日期范围筛选演示）
 *   status     运行状态（兜底为 syncing，实际以后端 status 为准）
 *   typeLabel  计数类型：人行 / 车行
 *   count      实时计数（占位 0，实际以 WS stats 为准）
 *   category   点位分类
 *   camera_type 摄像头类型：vehicle / person
 *
 * 注意：地图点位实时状态（status 等）仍可被后端 /ws stats.devices 按相同 id 驱动；
 *       坐标一律以本表为准（表内为最新实测经纬度）。
 */
export const CITY_WALL_POINTS = [
  {"id": "MEN-HYN", "desc": "和阳北门", "type": "卡口", "gate": "和阳门", "direction": "入口", "coord": [113.312548, 40.096421], "edge": "东墙", "cross": [113.315134, 40.096421], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-HYS", "desc": "和阳南门", "type": "卡口", "gate": "和阳门", "direction": "出口", "coord": [113.312603, 40.090238], "edge": "东墙", "cross": [113.315134, 40.090238], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-YTE", "desc": "永泰东门", "type": "卡口", "gate": "永泰门", "direction": "入口", "coord": [113.307697, 40.085605], "edge": "南墙", "cross": [113.307697, 40.083292], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-YTW", "desc": "永泰西门", "type": "卡口", "gate": "永泰门", "direction": "出口", "coord": [113.296953, 40.085723], "edge": "南墙", "cross": [113.296953, 40.083292], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-QYS", "desc": "清远南门", "type": "卡口", "gate": "清远门", "direction": "入口", "coord": [113.292030, 40.088534], "edge": "西墙", "cross": [113.289814, 40.088534], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-QYN", "desc": "清远北门", "type": "卡口", "gate": "清远门", "direction": "出口", "coord": [113.291963, 40.099525], "edge": "西墙", "cross": [113.289814, 40.099525], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-WDW", "desc": "武定西门", "type": "卡口", "gate": "武定门", "direction": "其他", "ioKind": "in", "coord": [113.297094, 40.102528], "edge": "北墙", "cross": [113.297094, 40.103218], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
  {"id": "MEN-WDE", "desc": "武定东门", "type": "卡口", "gate": "武定门", "direction": "出口", "coord": [113.309088, 40.102003], "edge": "北墙", "cross": [113.309088, 40.103218], "lastActive": "2026-09-18", "status": "syncing", "typeLabel": "车行", "count": 0, "category": "古城城门车行卡口点位", "camera_type": "vehicle"},
]

export const CITY_WALL_GATES = ['和阳门', '永泰门', '清远门', '武定门']

/* --------------------------------------------------------------------------
 * 框线「进出标识」规则
 *
 * 画法：每个城门点位垂直投影到最近一条框线边上，在交会点画一个 进 / 出 徽标，
 *       徽标两侧各留一段高亮短边（`.io-jamb`），使框线在交会处看起来像开了一个
 *       闸口 —— 即"框线与点位交叉的地方"由空白段替换为进出标识。
 *
 * 判定：优先取点位上的 ioKind 人工判定值，其次 direction 字段映射 —— 入口→进 / 出口→出；
 *       两者都拿不到时返回 null，前端渲染为中性「待定」徽标。
 * ------------------------------------------------------------------------ */

/** 进/出标识文案 */
export const IO_LABEL = { in: '进', out: '出' }

/**
 * 出入口方向 → 进出标识类型。
 * @param {{direction?: string, ioKind?: string}} p 点位
 * @returns {'in'|'out'|null} 进 / 出 / 待定
 */
export function ioKindOf(p) {
  if (!p) return null
  if (p.ioKind === 'in' || p.ioKind === 'out') return p.ioKind
  const d = p.direction
  if (d === '入口') return 'in'
  if (d === '出口') return 'out'
  return null
}

/**
 * 进出标识箭头旋转角度（deg）。
 * 以字符 `▶`（基准朝东 = 0°）顺时针旋转：
 *   进 = 箭头指向城内（区域中心）；出 = 箭头指向城外。
 * @param {string} edge 框线边：东墙 / 西墙 / 南墙 / 北墙
 * @param {'in'|'out'} kind 进出标识类型
 */
export function ioArrowDeg(edge, kind) {
  const horizontal = edge === '北墙' || edge === '南墙'   // 南北墙是横线，箭头上下指
  if (kind === 'in') {
    if (!horizontal) return edge === '东墙' ? 180 : 0     // 东墙进 = 向西；西墙进 = 向东
    return edge === '北墙' ? 90 : -90                     // 北墙进 = 向南；南墙进 = 向北
  }
  if (!horizontal) return edge === '东墙' ? 0 : 180       // 东墙出 = 向东；西墙出 = 向西
  return edge === '北墙' ? -90 : 90                       // 北墙出 = 向北；南墙出 = 向南
}

/* --------------------------------------------------------------------------
 * 「古城四面图」分区布局规则（对应设备平面示意图）
 *
 * 示意图的排版规律：
 *   · 清远门（西墙）的文字块在图的左侧   → zone = left
 *   · 和阳门（东墙）的文字块在图的右侧   → zone = right
 *   · 永泰门（南墙）的文字块在图的底部   → zone = bottom
 *   · 武定门（北墙）的文字块在图的顶部   → zone = top
 *   · 同一城门再按"另一半轴"分两段：左/右墙分南段/北段，上/下墙分西段/东段；
 *   · 每段内 **车卡（卡口）在前、便道在后**，段内再按坐标推进。
 * -------------------------------------------------------------------------- */

/** 城门 → 布局方位 */
export const GATE_ZONE = { '清远门': 'left', '和阳门': 'right', '永泰门': 'bottom', '武定门': 'top' }

/** 方位绘制顺序（左 → 右 → 上 → 下） */
const _ZONE_ORDER = { left: 0, right: 1, top: 2, bottom: 3 }
/** 类型顺序：车卡（卡口）在前，便道在后 */
const _TYPE_ORDER = { '卡口': 0, '便道': 1 }
/** 默认区域中心（大同古城），未传 center 时兜底 */
const _DEF_CENTER = [113.3025, 40.09325]

/**
 * 求点位所属布局方位。
 * 优先按城门映射；城门缺失或为兜底名（如后端未给出门名）时，
 * 按"相对区域中心最近的一侧"就近归属，保证任何点位都有确定的通道。
 * @param {{gate?: string, coord?: number[]}} p 点位
 * @param {number[]} [center] 区域中心 [lng, lat]
 */
export function zoneOfPoint(p, center) {
  const z = p && GATE_ZONE[p.gate]
  if (z) return z
  const c = (center && center.length >= 2) ? center : _DEF_CENTER
  if (!p || !p.coord) return 'top'
  const dx = p.coord[0] - c[0]
  const dy = p.coord[1] - c[1]
  return Math.abs(dx) >= Math.abs(dy) ? (dx < 0 ? 'left' : 'right') : (dy < 0 ? 'bottom' : 'top')
}

/**
 * 「古城四面图」式分区排序：把点位排成"同方位 → 同段 → 同类型"的整齐段落，
 * 供 TipLayer 的 zone 布局按序填入左/右列与上/下行。
 * @param {Array} points 点位数组
 * @param {number[]} [center] 区域中心 [lng, lat]，用于切分"南/北段""西/东段"
 */
export function sortPointsForZoneLayout(points, center) {
  const c = (center && center.length >= 2) ? center : _DEF_CENTER
  const cx = c[0], cy = c[1]
  /** 段序号：左/右通道按纬度切北(0)/南(1)；上/下通道按经度切西(0)/东(1) */
  const segOf = (p, z) => {
    const co = p.coord || c
    return (z === 'left' || z === 'right') ? (co[1] >= cy ? 0 : 1) : (co[0] >= cx ? 1 : 0)
  }
  /** 段内推进方向：左/右从北到南（纬度降序）；上/下从西到东（经度升序） */
  const along = (p, z) => {
    const co = p.coord || c
    return (z === 'left' || z === 'right') ? -co[1] : co[0]
  }
  return (points || [])
    .map((p, i) => ({ p, i, z: zoneOfPoint(p, center) }))
    .sort((A, B) => {
      const za = _ZONE_ORDER[A.z] ?? 9
      const zb = _ZONE_ORDER[B.z] ?? 9
      if (za !== zb) return za - zb
      const sa = segOf(A.p, A.z), sb = segOf(B.p, B.z)
      if (sa !== sb) return sa - sb
      const ta = _TYPE_ORDER[A.p.type] ?? 2, tb = _TYPE_ORDER[B.p.type] ?? 2
      if (ta !== tb) return ta - tb
      const la = along(A.p, A.z), lb = along(B.p, B.z)
      if (la !== lb) return la - lb
      return A.i - B.i   // 同坐标（如同一卡口的两个编号）保持原始顺序
    })
    .map((x) => x.p)
}

/** 按展示优先级排序：城门顺序 → 类型（卡口>便道）→ 方向（入口>出口>其他） */
const _GATE_ORDER = ['和阳门', '永泰门', '清远门', '武定门']
const _DIR_ORDER = { '入口': 0, '出口': 1, '其他': 2 }
export function sortPointsByPriority(points) {
  return (points || []).slice().sort((a, b) => {
    const ga = _GATE_ORDER.indexOf(a.gate)
    const gb = _GATE_ORDER.indexOf(b.gate)
    if (ga !== gb) return (ga === -1 ? 99 : ga) - (gb === -1 ? 99 : gb)
    const ta = a.type === '卡口' ? 0 : 1
    const tb = b.type === '卡口' ? 0 : 1
    if (ta !== tb) return ta - tb
    return (_DIR_ORDER[a.direction] ?? 2) - (_DIR_ORDER[b.direction] ?? 2)
  })
}

/* --------------------------------------------------------------------------
 * 「道路走向 · 车辆行驶方向 · 大卡落位」规则（一律以城墙框线为基准）
 *
 * 1) 道路走向由所在城墙边决定：
 *      东墙 / 西墙（和阳门 / 清远门）→ 道路东西向（axis = 'ew'）
 *      南墙 / 北墙（永泰门 / 武定门）→ 道路南北向（axis = 'ns'）
 *
 * 2) 车辆行驶方向由「进 / 出」决定（进 = 驶向城内，出 = 驶离城外）：
 *      东墙  进 = 向西（进城）   出 = 向东
 *      西墙  进 = 向东（进城）   出 = 向西
 *      南墙  进 = 向北（进城）   出 = 向南
 *      北墙  进 = 向南（进城）   出 = 向北
 *
 * 3) rot = 断面示意图需要旋转的角度（CSS deg，正 = 顺时针）。
 *    示意图的默认状态是"车道自北向南、车头朝下（▼）"，旋转后箭头朝向即真实行驶方向：
 *      向南 → 0      向北 → 180     向东 → -90（图向右）     向西 → 90（图向左）
 *
 * 4) 大卡落位：8 张大卡不再是「以自身经纬度点位为锚」，而是统一挂靠在
 *    框线交会点 cross（即地图上 进/出 徽标所在处）上 —— 见下方「挂靠框线
 *    进出标识」锚点段落。卡片主体朝城外一侧展开（WALL_OUT_SIDE），
 *    与进出口保持固定位置关系，不遮挡框线与点位。
 *    是否城内/城外由 place（inner/outer）描述走向语义。
 * ------------------------------------------------------------------------ */

/** 城墙边 → 道路走向 / 进·出流向 / 框线内外语义 */
const WALL_RULE = {
  东墙: { axis: 'ew', inFlow: 'west', outFlow: 'east', place: 'outer' },
  西墙: { axis: 'ew', inFlow: 'east', outFlow: 'west', place: 'outer' },
  南墙: { axis: 'ns', inFlow: 'north', outFlow: 'south', place: 'inner' },
  北墙: { axis: 'ns', inFlow: 'south', outFlow: 'north', place: 'inner' }
}

/* --------------------------------------------------------------------------
 * 大数据卡「挂靠框线进出标识」锚点
 *
 * 设计诉求：大卡不再以自己的经纬度点位为锚，而是「挂靠在地图上已有的
 *   进 / 出 徽标（即框线交会点 cross）」上 —— 大卡与框线进出口建立固定的
 *   位置关系，卡片始终跟着进出口走。
 *
 * 落位算法：交会点 cross 沿所在墙边的【垂直法向】向城外偏 BADGE_CLEAR_PX 像素，
 *   即卡片边缘永远贴着徽标外侧、不叠在徽标上。
 *   · 偏移量以「像素」而非「米」给出：滚轮缩放时卡片与徽标按同一比例缩放，
 *     两者间距在视觉上恒定（若用米，缩小后间距会被压成 0，卡片糊在徽标上）。
 *   · 方向取垂直法向（东墙→向东…），不用 cross−中心 的对角方向：那会让近角门
 *     被斜着推出去，环看起来是"歪"的。
 * ------------------------------------------------------------------------ */

/** 大卡边缘与进出徽标之间的视觉间距（px，缩放前基准值） */
export const CARD_BADGE_CLEAR_PX = 18

/**
 * 交会点 cross 沿所在墙边的垂直法向，向城外偏移米数后的经纬坐标。
 * 像素间距 → 米 的换算由调用方按当前缩放级完成（见 metersPerPixelAt）。
 * @param {{cross?:number[], edge?:string}} p 点位（需带 cross、edge）
 * @param {number} offsetM 外推距离（米）
 * @returns {[number, number]|null} [经度, 纬度]
 */
export function anchorOutOfCross(p, offsetM) {
  if (!p || !Array.isArray(p.cross) || !isFinite(p.cross[0]) || !isFinite(p.cross[1])) return null
  const d = isFinite(offsetM) ? offsetM : 0
  if (d === 0) return [p.cross[0], p.cross[1]]
  const lat = p.cross[1]
  const mLat = d / 111320
  const mLng = d / (111320 * Math.cos((lat * Math.PI) / 180))
  const edge = p.edge || '东墙'
  let dl = 0
  let dn = 0
  if (edge === '东墙') dl = mLng
  else if (edge === '西墙') dl = -mLng
  else if (edge === '南墙') dn = -mLat
  else if (edge === '北墙') dn = mLat
  return [p.cross[0] + dl, p.cross[1] + dn]
}

/**
 * 某缩放级下 1 像素对应的米数（Web-Mercator）。
 * 用于把「视觉间距(px)」换算成地理外推距离(米)，使间距不随缩放变形。
 * @param {number} zoom 地图当前缩放级
 * @param {number} lat 目标纬度
 * @returns {number} 米/像素
 */
export function metersPerPixelAt(zoom, lat) {
  return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, zoom)
}

/** 城墙边 → 大卡在锚点的展开侧（永远朝城外，卡片主体不压框线） */
export const WALL_OUT_SIDE = { 东墙: 'right', 西墙: 'left', 南墙: 'bottom', 北墙: 'top' }

/** 方位字 */
export const FLOW_LABEL = { north: '北', east: '东', south: '南', west: '西' }
/** 方位 → 断面旋转角（CSS deg，顺时针为正） */
const FLOW_ROT = { south: 0, north: 180, east: -90, west: 90 }
/** 方位 → 罗盘指针角（指针基准朝上 = 北，顺时针为正） */
const FLOW_BEARING = { north: 0, east: 90, south: 180, west: -90 }
/** 对向方位 */
const FLOW_OPPOSITE = { north: 'south', south: 'north', east: 'west', west: 'east' }

/**
 * 罗盘角（0=北，顺时针为正）→ 方位字。
 * 用于推算"断面旋转后，图内左右两条便道分别朝向哪个方位"。
 */
function bearingLabel(deg) {
  const d = ((Math.round(deg) % 360) + 360) % 360
  if (d === 0) return '北'
  if (d === 90) return '东'
  if (d === 180) return '南'
  if (d === 270) return '西'
  return ''
}

/** 行车方向罗盘角（intersection_info.rotation：0北/90东/180南/270西）→ 方位 */
const ROT2FLOW = { 0: 'north', 90: 'east', 180: 'south', 270: 'west' }
/** 方位 → 道路走向轴（南北向行车 = ns，东西向 = ew） */
const FLOW_AXIS = { north: 'ns', south: 'ns', east: 'ew', west: 'ew' }

/**
 * 道路 / 落位信息（大卡断面渲染 + TipLayer 落位共用）。
 * 方向来源优先级：intersection_info.rotation（配置罗盘角）> 城墙边+进出几何推导；
 * 出入口标签优先级：intersection_info.badge > ioKindOf 几何推导。
 * @param {object} p 点位（需带 edge、direction 或 ioKind；可带 ist=路口配置）
 * @returns {{
 *   wall:string, kind:'in'|'out', kindLabel:string, dirLabel:string,
 *   axis:'ns'|'ew', flow:string, flowText:string, flowPhrase:string,
 *   oppositeText:string, rot:number, place:'inner'|'outer', placeLabel:string,
 *   intoCity:boolean
 * }}
 */
export function roadInfoOf(p) {
  const wall = (p && p.edge) || '东墙'
  const rule = WALL_RULE[wall] || WALL_RULE['东墙']
  const ist = (p && p.ist) || null
  // —— 行车方向：配置 rotation 优先（未配置/非法值回退几何推导）——
  let cfgRot = null
  if (ist && ist.rotation != null && ist.rotation !== '') {
    const n = ((Number(ist.rotation) % 360) + 360) % 360
    if (Number.isFinite(n) && ROT2FLOW[n] != null) cfgRot = n
  }
  let kind = ioKindOf(p) || 'in'      // 兜底按入口处理，保证卡片一定有明确方向
  const flow = cfgRot != null ? ROT2FLOW[cfgRot] : (kind === 'out' ? rule.outFlow : rule.inFlow)
  const rot = FLOW_ROT[flow]
  // 配置了 rotation 时，走向轴随行车方向（而非城墙边）；axis 供断面布局（ns=横放/ew=竖放）使用
  const axis = cfgRot != null ? FLOW_AXIS[flow] : rule.axis
  // —— 出入口标签：配置 badge 优先（入口/出口 直接改判 kind；其他值仅覆盖标签文案）——
  let dirLabel = kind === 'in' ? '入口' : '出口'
  if (ist && ist.badge === '入口') { kind = 'in'; dirLabel = '入口' }
  else if (ist && ist.badge === '出口') { kind = 'out'; dirLabel = '出口' }
  else if (ist && ist.badge) { dirLabel = String(ist.badge) }
  // 断面旋转后，图内左 / 右两条便道各自朝向的真实方位（左便道基准朝西 = -90°）
  const leftBearing = -90 + rot
  const sideText = [bearingLabel(leftBearing), bearingLabel(leftBearing + 180)]
  return {
    wall,
    kind,
    kindLabel: IO_LABEL[kind],
    dirLabel,
    axis,
    flow,
    flowText: FLOW_LABEL[flow],
    flowPhrase: '向' + FLOW_LABEL[flow],
    oppositeText: FLOW_LABEL[FLOW_OPPOSITE[flow]],
    rot,
    flowDeg: cfgRot != null ? cfgRot : FLOW_BEARING[flow],
    sideText,
    place: rule.place,
    placeLabel: rule.place === 'inner' ? '城墙框线内侧' : '城墙框线外侧',
    intoCity: kind === 'in'
  }
}

/* --------------------------------------------------------------------------
 * 设备坐标 → 古城框线投影（后端驱动点位）
 *
 * 后端 /api/devices 只提供设备经纬度；而大卡锚点、框线「进/出」徽标、大卡→框线
 * 连接线都依赖「交会点 cross + 所在墙边 edge」。这里把设备坐标垂直投影到古城
 * 框线多边形最近的一条边上，几何算出 cross / edge —— 不再依赖硬编码的城门点位表。
 * ------------------------------------------------------------------------ */

/**
 * 把点位垂直投影到多边形最近的一条边上，得到交会点与所在墙边。
 * @param {number[]} coord 设备经纬度 [经度, 纬度]
 * @param {Array<number[]>} polygon 古城框线多边形（自动首尾闭合）
 * @returns {{cross:[number,number], edge:'东墙'|'西墙'|'南墙'|'北墙'}|null}
 */
export function projectToWall(coord, polygon) {
  if (!Array.isArray(coord) || coord.length < 2) return null
  const poly = Array.isArray(polygon) && polygon.length >= 3 ? polygon : null
  if (!poly) return null
  const lng = Number(coord[0])
  const lat = Number(coord[1])
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null
  // 以目标点纬度做局部等距圆柱投影（米）：消除经度方向的 cos 缩放，
  // 保证「垂足」是真实几何意义上的垂直投影（否则东西向距离被拉长会选错边）。
  const kx = 111320 * Math.cos((lat * Math.PI) / 180)
  const ky = 110540
  const px = lng * kx
  const py = lat * ky
  let best = null
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i]
    const b = poly[(i + 1) % poly.length]
    if (!a || !b) continue
    const ax = a[0] * kx, ay = a[1] * ky
    const bx = b[0] * kx, by = b[1] * ky
    const dx = bx - ax, dy = by - ay
    const len2 = dx * dx + dy * dy
    if (!(len2 > 0)) continue
    let t = ((px - ax) * dx + (py - ay) * dy) / len2
    t = Math.max(0, Math.min(1, t))          // 垂足超出线段时取最近端点（就近投射到墙角）
    const cx = ax + t * dx, cy = ay + t * dy
    const d = Math.hypot(px - cx, py - cy)
    if (!best || d < best.d) best = { d, cx, cy, ex: dx, ey: dy }
  }
  if (!best) return null
  const cross = [best.cx / kx, best.cy / ky]
  // 以多边形顶点均值为框线中心，按边的走向判定所属墙面：
  //   近东西走向 → 北墙 / 南墙；近南北走向 → 东墙 / 西墙
  let cLng = 0, cLat = 0
  poly.forEach((p) => { cLng += p[0]; cLat += p[1] })
  cLng /= poly.length
  cLat /= poly.length
  let edge
  if (Math.abs(best.ex) >= Math.abs(best.ey)) {
    edge = cross[1] >= cLat ? '北墙' : '南墙'
  } else {
    edge = cross[0] >= cLng ? '东墙' : '西墙'
  }
  return { cross, edge }
}
