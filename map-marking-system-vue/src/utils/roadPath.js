/* ============================================================
 * [临时停用] 点位到点位路径高亮功能 —— 寻路工具模块
 * ------------------------------------------------------------
 * 本文件已按需求临时整体注释（保留完整代码结构，未删除任何内容）。
 * 恢复方式：删除本文件第一行的注释开标记 与 最后一行的注释闭标记，
 *           即可恢复为普通 JS 模块。
 * 依赖说明：本模块依赖 src/data/roadNetwork.js（已一并停用）；
 *           当前未被任何运行时代码引用（属功能保留代码）。
 * ============================================================
// 路径查找工具：基于 roadNetwork 的 A* 寻路 + 最近节点吸附 + 路径拼接
import { ROAD_NODES, ROAD_ADJ, NODE_BY_ID } from '../data/roadNetwork.js'

// —— 经纬度距离（米）：Haversine 简化版（小范围内准确性足够）——
function haversineM(a, b) {
  const R = 6371000
  const rad = Math.PI / 180
  const lat1 = a[1] * rad, lat2 = b[1] * rad
  const dLat = lat2 - lat1
  const dLng = (b[0] - a[0]) * rad
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(s))
}

// 节点距离
function nodeDist(na, nb) {
  return haversineM(NODE_BY_ID[na].coord, NODE_BY_ID[nb].coord)
}

// —— 节点坐标列表（懒构，向 A* 热路径减负）——
const NODE_LIST = ROAD_NODES

// —— 找离 (lng,lat) 最近的节点 ——
export function nearestNode(coord) {
  let best = null
  let bestD = Infinity
  for (const n of NODE_LIST) {
    const d = haversineM(coord, n.coord)
    if (d < bestD) { bestD = d; best = n }
  }
  return { node: best, distance: bestD }
}

// —— A* 寻路：返回节点 ID 数组（含端点）；不可达时返回 null ——
export function findPathAStar(startId, endId) {
  if (startId === endId) return [startId]
  const open = new Set([startId])
  const came = new Map() // nodeId -> prev nodeId
  const g = new Map([[startId, 0]])
  const f = new Map([[startId, nodeDist(startId, endId)]])

  while (open.size > 0) {
    // 弹出最小 f 值（线性扫描足以，20 节点规模）
    let cur = null
    let bestF = Infinity
    for (const id of open) {
      const v = f.get(id)
      if (v < bestF) { bestF = v; cur = id }
    }
    if (cur === endId) {
      // 回溯
      const path = [cur]
      while (came.has(cur)) { cur = came.get(cur); path.unshift(cur) }
      return path
    }
    open.delete(cur)
    const neighbors = ROAD_ADJ.get(cur) || []
    for (const { neighbor } of neighbors) {
      const tentative = g.get(cur) + nodeDist(cur, neighbor)
      if (!g.has(neighbor) || tentative < g.get(neighbor)) {
        came.set(neighbor, cur)
        g.set(neighbor, tentative)
        f.set(neighbor, tentative + nodeDist(neighbor, endId))
        open.add(neighbor)
      }
    }
  }
  return null
}

// —— 起 / 终点都是任意 (lng,lat)：吸附到最近节点后寻路，返回：
//      { lnglats,      // 沿实际道路的多段折线 [lng,lat] 数组
//        lengthMeters, // 总长度（米）
//        roadNames,    // 经过的路段名（去重保序）
//        nodes,        // 节点 ID 数组
//        startNodeId,
//        endNodeId }
export function findPathAlongRoads(startCoord, endCoord, snapRadiusMeters = 1500) {
  const s = nearestNode(startCoord)
  const e = nearestNode(endCoord)
  if (!s.node || !e.node) return null
  // 不在吸附半径内直接拒绝（避免远郊凭空绘制道路）
  if (s.distance > snapRadiusMeters || e.distance > snapRadiusMeters) return null
  const nodeIds = findPathAStar(s.node.id, e.node.id)
  if (!nodeIds) return null
  // 拼接坐标：端点不用吸附节点坐标——直接用真实坐标，让线条"贴"到点上
  const lnglats = [startCoord]
  // 节点之间记录穿过的路段（用于 InfoWindow 文本）
  const segs = []
  for (let i = 0; i < nodeIds.length - 1; i++) {
    const a = NODE_BY_ID[nodeIds[i]]
    const b = NODE_BY_ID[nodeIds[i + 1]]
    // 这段的路名
    const edge = (ROAD_ADJ.get(a.id) || []).find((x) => x.neighbor === b.id)
    if (edge && edge.road) segs.push(edge.road)
    // 跳过第一个/最后一个节点的坐标，因为它们已经在端点被吸收
    if (i > 0) lnglats.push(a.coord)
    lnglats.push(b.coord)
  }
  lnglats.push(endCoord)
  // 总长度
  let total = 0
  for (let i = 1; i < lnglats.length; i++) total += haversineM(lnglats[i - 1], lnglats[i])
  // 路段名去重保序
  const seen = new Set()
  const roadNames = []
  for (const r of segs) if (!seen.has(r)) { seen.add(r); roadNames.push(r) }
  return {
    lnglats,
    lengthMeters: total,
    roadNames,
    nodes: nodeIds,
    startNodeId: s.node.id,
    endNodeId: e.node.id,
    startSnapDistance: s.distance,
    endSnapDistance: e.distance
  }
}

// —— 列出起点附近的前 K 个最近节点（用于"辐射路径"展示） ——
export function topKNearestNodes(coord, k = 4) {
  const list = ROAD_NODES
    .map((n) => ({ id: n.id, node: n, distance: haversineM(coord, n.coord) }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, k + 1) // 包含自身
  return list.length > 1 ? list.slice(1) : []
}
*/
