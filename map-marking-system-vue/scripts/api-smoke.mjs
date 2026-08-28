/**
 * v0.9.0 Mock 后端冒烟测试（仅验证 mock.js 路由行为，不依赖浏览器）
 * 运行：node scripts/api-smoke.mjs
 */
import { mockRequest } from '../src/api/mock.js'

let pass = 0, fail = 0
async function t(name, fn) {
  try {
    await fn()
    pass++
    console.log('  ✅', name)
  } catch (e) {
    fail++
    console.log('  ❌', name, '→', e.message)
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed') }
function assertEq(a, b, msg) { if (a !== b) throw new Error(`${msg || 'assertEq'}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`) }

console.log('== v0.9.0 接口冒烟测试 ==')

await t('§3.2 设备列表返回 longitude/latitude', async () => {
  const devs = await mockRequest('GET', '/api/devices')
  assert(Array.isArray(devs) && devs.length >= 3, '设备数不足')
  devs.forEach((d) => {
    assert('longitude' in d && 'latitude' in d, d.id + ' 缺少经纬度字段')
    assert(!('lng' in d) && !('lat' in d), d.id + ' 仍含旧 lng/lat 字段')
  })
})

await t('§4.2 设备统计含 hour_* 与 person_flow_per_min', async () => {
  const stats = await mockRequest('GET', '/api/stats/devices')
  stats.forEach((s) => {
    for (const k of ['hour', 'hour_vehicle_in', 'hour_vehicle_out', 'hour_person_in', 'hour_person_out', 'person_flow_per_min', 'congested']) {
      assert(k in s, s.device_id + ' 缺少 ' + k)
    }
  })
})

await t('§4.5 历史报表：整日区间返回记录', async () => {
  const res = await mockRequest('GET', '/api/stats/hourly/history', null, { start_date: '2026-08-20', end_date: '2026-08-21' })
  assertEq(res.device_id, null, 'device_id 应为 null（全局）')
  assertEq(res.start, '2026-08-20:00', 'start 边界')
  assertEq(res.end, '2026-08-21:23', 'end 边界')
  assertEq(res.records.length, 48, '两整日应有 48 条')
  const r = res.records[0]
  for (const k of ['stat_date', 'hour', 'vehicle_in', 'vehicle_out', 'person_in', 'person_out']) {
    assert(k in r, '记录缺少 ' + k)
  }
})

await t('§4.5 历史报表：精确到小时 + 指定设备', async () => {
  const res = await mockRequest('GET', '/api/stats/hourly/history', null, { device_id: 'cam-gate-north', start_date: '2026-08-20:08', end_date: '2026-08-20:10' })
  assertEq(res.device_id, 'cam-gate-north', 'device_id')
  assertEq(res.records.length, 3, '闭区间 08-10 应有 3 条')
})

await t('§4.5 历史报表：非法时间 → 422', async () => {
  try {
    await mockRequest('GET', '/api/stats/hourly/history', null, { start_date: 'bad', end_date: '2026-08-21' })
    throw new Error('应抛 422')
  } catch (e) { assertEq(e.status, 422, '状态码') }
})

await t('§4.5 历史报表：开始晚于结束 → 422', async () => {
  try {
    await mockRequest('GET', '/api/stats/hourly/history', null, { start_date: '2026-08-22', end_date: '2026-08-20' })
    throw new Error('应抛 422')
  } catch (e) { assertEq(e.status, 422, '状态码') }
})

await t('§4.5 历史报表：设备不存在 → 404', async () => {
  try {
    await mockRequest('GET', '/api/stats/hourly/history', null, { device_id: 'nope', start_date: '2026-08-20', end_date: '2026-08-21' })
    throw new Error('应抛 404')
  } catch (e) { assertEq(e.status, 404, '状态码') }
})

await t('§10.1 读取业务规则：分组 + 参数元数据', async () => {
  const res = await mockRequest('GET', '/api/config/business-rules')
  assertEq(res.file, 'configs/business_rules.yaml', 'file')
  assertEq(res.hot_reload, true, 'hot_reload')
  assert(res.groups.length >= 6, '分组不足')
  res.groups.forEach((g) => {
    assert(g.key && g.title && Array.isArray(g.params), '分组结构不完整')
    g.params.forEach((p) => {
      for (const k of ['key', 'label', 'desc', 'type', 'default', 'value']) assert(k in p, p.key + ' 缺少 ' + k)
    })
  })
})

await t('§10.2 更新业务规则：合法修改 → ok + value 回写', async () => {
  const res = await mockRequest('PUT', '/api/config/business-rules', { prediction: { interval_minutes: 20 } })
  assertEq(res.status, 'ok', 'status')
  assert(res.message.includes('热重载'), 'message')
  const p = res.groups.flatMap((g) => g.params).find((x) => x.key === 'interval_minutes' && x.default === 15)
  assertEq(p.value, 20, '更新后 value')
})

await t('§10.2 更新业务规则：超范围 → 400', async () => {
  try {
    await mockRequest('PUT', '/api/config/business-rules', { prediction: { vehicle_person_max: 999 } })
    throw new Error('应抛 400')
  } catch (e) { assertEq(e.status, 400, '状态码'); assert(e.detail.includes('vehicle_person_max'), 'detail 应含参数名') }
})

await t('§10.2 更新业务规则：空请求体 → 400', async () => {
  try {
    await mockRequest('PUT', '/api/config/business-rules', { unknown_group: { a: 1 } })
    throw new Error('应抛 400')
  } catch (e) { assertEq(e.status, 400, '状态码') }
})

await t('§4.4 GET /api/stats/congestion 已废弃 → 404', async () => {
  try {
    await mockRequest('GET', '/api/stats/congestion')
    throw new Error('应抛 404')
  } catch (e) { assertEq(e.status, 404, '状态码') }
})

await t('§3.4 wvp-webhook 已删除 → 404', async () => {
  try {
    await mockRequest('POST', '/api/devices/wvp-webhook', {})
    throw new Error('应抛 404')
  } catch (e) { assertEq(e.status, 404, '状态码') }
})

await t('§3.5 stream 响应含 longitude/latitude', async () => {
  const res = await mockRequest('GET', '/api/devices/cam-gate-north/stream')
  assert('longitude' in res && 'latitude' in res, '缺少经纬度')
  assert(Number.isFinite(res.longitude), '经度非数值')
})

console.log(`\n结果：${pass} 通过 / ${fail} 失败`)
process.exit(fail ? 1 : 0)
