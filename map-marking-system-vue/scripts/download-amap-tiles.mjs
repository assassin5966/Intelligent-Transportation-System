#!/usr/bin/env node
/**
 * 高德地图离线瓦片下载器（Node 18+）
 *
 * 用途：把高德 JS API 2.0 的栅格瓦片（web / satellite）按指定城市与缩放区间
 *       下载到 public/tiles/{z}/{x}/{y}.png（或 .jpg），供内网离线部署使用。
 *
 * 用法：
 *   node scripts/download-amap-tiles.mjs --cities=datong --min-zoom=12 --max-zoom=18
 *   node scripts/download-amap-tiles.mjs --cities=datong,yungangshiku,xian --min-zoom=13 --max-zoom=16 --style=8
 *   node scripts/download-amap-tiles.mjs --cities=datong --style=6 --out=public/tiles-sat
 *   node scripts/download-amap-tiles.mjs --cities=datong --min-zoom=12 --max-zoom=18 --dark-style=7 --light-out=public/tiles --dark-out=public/tiles-dark
 *
 * 输出结构：
 *   {out}/{z}/{x}/{y}.png   （或 .jpg，style=6 卫星为 jpg）
 *
 * 选参：
 *   --cities=datong,yungangshiku,xian    城市列表（可多选）
 *   --min-zoom=12 --max-zoom=18           缩放区间（含两端）
 *   --style=8                            瓦片风格：8=路网矢量中文 | 7=无注记矢量 | 6=卫星
 *   --out=public/tiles                   输出目录（默认 public/tiles）
 *   --concurrency=8                      并发下载数（默认 8）
 *   --retries=3                          单瓦片失败重试次数（默认 3）
 *   --skip-existing                      跳过已存在（默认 true）
 *   --dry-run                            只打印计划不下载
 *   --host=auto                          主机子域 01/02/03/04（默认 auto 轮询）
 *
 * 注意：高德瓦片坐标系为 GCJ-02，Leaflet 默认按 WGS-84 显示，会产生约 50-500m 偏移。
 *       该脚本仅下载底图，不做坐标纠偏；如需与 WGS-84 标点严格对齐，请在 MapPanel 中
 *       引入 proj4leaflet 或使用 GCJ-02 的标点数据。
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import process from 'node:process'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = path.resolve(__dirname, '..')

// —— 城市定义（与 src/components/MapPanel.vue 中的 CITY 保持一致）——
// 三级金字塔（避免缩小后四周空白，同时控制瓦片总量）：
//   bboxRegion : 市域，用于 z <= regionMaxZoom（默认 11）
//   bboxWide   : 主城区，用于 regionMaxZoom < z <= wideMaxZoom（默认 14）
//   bbox       : 核心区，用于 z > wideMaxZoom（按 --pad 外扩）
const CITIES = {
  datong: {
    name: '大同·古城',
    bbox: [113.289814, 40.083292, 113.315134, 40.103218], // 古城，[w, s, e, n]
    bboxWide: [113.00, 39.90, 113.60, 40.30],             // 主城区（约 55×45km）
    bboxRegion: [112.60, 39.55, 114.20, 40.60],           // 大同市域
    center: [113.3025, 40.09325],
  },
  yungangshiku: {
    name: '大同·云冈石窟',
    bbox: [113.12589, 40.101345, 113.14589, 40.121345],
    bboxWide: [113.00, 39.90, 113.60, 40.30],
    bboxRegion: [112.60, 39.55, 114.20, 40.60],
    center: [113.13589, 40.111345],
  },
  xian: {
    name: '西安·雁塔',
    bbox: [108.9440, 34.2550, 108.9640, 34.2750],
    bboxWide: [108.75, 34.05, 109.15, 34.45],
    bboxRegion: [108.40, 33.60, 109.70, 34.80],
    center: [108.9540, 34.2650],
  },
}

// —— 屏幕铺满保障 ——
// 1920×1080 的可视区在 z15 约需 9×6 张瓦片；再乘 screenPad 留出拖动余量。
// 该范围随 zoom 变化（zoom 每 +1，地理跨度减半），保证用户无论缩放到哪一级，
// 当前视野内都有瓦片，不会出现四周空白。
const SCREEN = { dw: 0.10, dh: 0.065, refZoom: 15, pad: 1.7 }

function screenBbox(center, z, pad) {
  const [cx, cy] = center
  const scale = 2 ** (SCREEN.refZoom - z) * (pad || SCREEN.pad)
  const dw = (SCREEN.dw * scale) / 2
  const dh = (SCREEN.dh * scale) / 2
  return [cx - dw, cy - dh, cx + dw, cy + dh]
}

function unionBbox(a, b) {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]
}

// —— 按缩放级选择 bbox（三级金字塔 ∪ 屏幕铺满范围）——
function bboxForZoom(city, z, regionMaxZoom, wideMaxZoom, pad) {
  let box, layer
  if (z <= regionMaxZoom && city.bboxRegion) { box = city.bboxRegion; layer = 'region' }
  else if (z <= wideMaxZoom && city.bboxWide) { box = city.bboxWide; layer = 'wide' }
  else { box = padBbox(city.bbox, pad); layer = 'core' }
  // 与「铺满屏幕所需范围」取并集
  return { box: unionBbox(box, screenBbox(city.center, z)), layer }
}

// —— 高德瓦片 URL 子域轮询（提升并发）——
const HOSTS = ['1', '2', '3', '4']

// —— 解析命令行参数 ——
function parseArgs(argv) {
  const out = {
    cities: ['datong'],
    minZoom: 9,
    maxZoom: 18,
    style: 8,
    out: 'public/tiles',
    concurrency: 8,
    retries: 5,
    skipExisting: true,
    dryRun: false,
    host: 'auto',
    regionMaxZoom: 11, // z <= 该值用 bboxRegion（市域）
    wideMaxZoom: 14,   // regionMaxZoom < z <= 该值用 bboxWide（主城区）
    pad: 2,            // 核心区外扩倍数（1 = 不扩，2 = 四周各扩 1 个 bbox 宽）
  }
  for (const a of argv) {
    if (a === '--dry-run') { out.dryRun = true; continue }
    if (a === '--skip-existing') { out.skipExisting = true; continue }
    if (a === '--no-skip-existing') { out.skipExisting = false; continue }
    const m = a.match(/^--([^=]+)=(.*)$/)
    if (!m) continue
    const [, k, v] = m
    if (k === 'cities') out.cities = v.split(',').map(s => s.trim()).filter(Boolean)
    else if (k === 'min-zoom') out.minZoom = Number(v)
    else if (k === 'max-zoom') out.maxZoom = Number(v)
    else if (k === 'style') out.style = Number(v)
    else if (k === 'out') out.out = path.resolve(PROJECT_ROOT, v)
    else if (k === 'concurrency') out.concurrency = Math.max(1, Number(v))
    else if (k === 'retries') out.retries = Math.max(0, Number(v))
    else if (k === 'host') out.host = v
    else if (k === 'wide-max-zoom') out.wideMaxZoom = Number(v)
    else if (k === 'region-max-zoom') out.regionMaxZoom = Number(v)
    else if (k === 'pad') out.pad = Math.max(1, Number(v))
  }
  return out
}

// —— 按 pad 倍数外扩 bbox（四周各扩 (pad-1)/2 个 bbox 尺寸）——
function padBbox(bbox, pad) {
  if (pad <= 1) return bbox
  const [w, s, e, n] = bbox
  const dx = (e - w) * (pad - 1) / 2
  const dy = (n - s) * (pad - 1) / 2
  return [w - dx, s - dy, e + dx, n + dy]
}

// —— slippy map 瓦片坐标计算 ——
function lngLatToTile(lng, lat, z) {
  const n = 2 ** z
  const xt = Math.floor(((lng + 180) / 360) * n)
  const latRad = (lat * Math.PI) / 180
  const yt = Math.floor(
    ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n
  )
  return [xt, yt]
}

function bboxTileRange(bbox, z) {
  const [w, s, e, n] = bbox
  const [wX, nY] = lngLatToTile(w, n, z) // nw
  const [eX, sY] = lngLatToTile(e, s, z) // se
  return {
    xMin: Math.min(wX, eX),
    xMax: Math.max(wX, eX),
    yMin: Math.min(nY, sY),
    yMax: Math.max(nY, sY),
  }
}

// —— HTTP 拉瓦片（带重试 + 指数退避）——
async function fetchTile(url, retries) {
  let lastErr
  for (let i = 0; i <= retries; i++) {
    try {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 20000)
      const res = await fetch(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
          'Referer': 'https://www.amap.com/',
          'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        },
        signal: ctrl.signal,
      })
      clearTimeout(t)
      if (!res.ok) throw new Error('HTTP ' + res.status)
      const buf = Buffer.from(await res.arrayBuffer())
      // 高德对无数据区域（如海外、z>18 边缘）返回 179 字节透明 PNG；
      // 对限流 IP 也回 179 字节。两者区分：连续 2 次都 < 1024 字节 → 视为真空白，不再重试。
      if (buf.length < 1024) {
        if (i < Math.min(2, retries)) {
          // 疑似限流，再试 1-2 次
          const wait = 1500 * Math.pow(2, i) + Math.random() * 600
          await new Promise(r => setTimeout(r, wait))
          continue
        }
        // 仍 < 1024：跳过（不写入文件，也不抛错）
        return null
      }
      return buf
    } catch (err) {
      lastErr = err
      if (i === retries) break
      // 网络层异常：指数退避
      const wait = 1200 * Math.pow(2, i) + Math.random() * 600
      await new Promise(r => setTimeout(r, wait))
    }
  }
  throw lastErr
}

// —— 并发池（手写，不依赖第三方）——
function createPool(limit) {
  const queue = []
  let active = 0
  const results = { ok: 0, fail: 0, skipped: 0 }

  function next() {
    if (active >= limit || queue.length === 0) return
    active++
    const job = queue.shift()
    job().then(
      r => { if (r === 'ok') results.ok++; else if (r === 'skip') results.skipped++; active--; next() },
      () => { results.fail++; active--; next() }
    )
  }

  return {
    push(fn) { queue.push(fn); next() },
    results,
    get pending() { return active + queue.length },
  }
}

// —— 主流程 ——
async function main() {
  const opt = parseArgs(process.argv.slice(2))
  if (![6, 7, 8].includes(opt.style)) {
    console.error('❌ --style 仅支持 6/7/8'); process.exit(1)
  }
  const ext = opt.style === 6 ? 'jpg' : 'png'
  const baseUrl = (host, z, x, y) =>
    `https://webrd0${host}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=${opt.style}&x=${x}&y=${y}&z=${z}`

  fs.mkdirSync(opt.out, { recursive: true })

  // 1. 计算全部瓦片清单
  const plan = [] // { z, x, y, file, url }
  let hostIdx = 0
  for (const cityKey of opt.cities) {
    const city = CITIES[cityKey]
    if (!city) { console.error('⚠ 跳过未知城市：' + cityKey); continue }
    for (let z = opt.minZoom; z <= opt.maxZoom; z++) {
      // 三级金字塔（市域 → 主城区 → 核心区）∪ 屏幕铺满范围
      const { box, layer } = bboxForZoom(city, z, opt.regionMaxZoom, opt.wideMaxZoom, opt.pad)
      const { xMin, xMax, yMin, yMax } = bboxTileRange(box, z)
      for (let x = xMin; x <= xMax; x++) {
        for (let y = yMin; y <= yMax; y++) {
          const dir = path.join(opt.out, String(z), String(x))
          const file = path.join(dir, `${y}.${ext}`)
          plan.push({
            city: cityKey,
            z, x, y, file, layer,
            url: baseUrl(HOSTS[hostIdx++ % HOSTS.length], z, x, y),
          })
        }
      }
    }
  }

  // 2. 统计
  const totalKb = plan.length * 25 // 估算每瓦片 25KB
  console.log('—— 离线瓦片下载计划 ——')
  for (const cityKey of opt.cities) {
    const c = CITIES[cityKey]
    if (!c) continue
    const stat = { region: 0, wide: 0, core: 0 }
    const zr = { region: [], wide: [], core: [] }
    for (let z = opt.minZoom; z <= opt.maxZoom; z++) {
      const { box, layer } = bboxForZoom(c, z, opt.regionMaxZoom, opt.wideMaxZoom, opt.pad)
      const r = bboxTileRange(box, z)
      stat[layer] += (r.xMax - r.xMin + 1) * (r.yMax - r.yMin + 1)
      zr[layer].push(z)
    }
    const fmt = (a) => a.length ? `z${a[0]}-z${a[a.length - 1]}` : '—'
    console.log(`  · ${c.name}：共 ${stat.region + stat.wide + stat.core} 张`)
    console.log(`      市域   ${fmt(zr.region)}：${stat.region} 张`)
    console.log(`      主城区 ${fmt(zr.wide)}：${stat.wide} 张`)
    console.log(`      核心区 ${fmt(zr.core)}：${stat.core} 张（pad=${opt.pad}）`)
  }
  console.log(`  · 合计：${plan.length} 张 · 风格 style=${opt.style} · 输出 ${path.relative(PROJECT_ROOT, opt.out)}`)
  console.log(`  · 估算：~${(totalKb / 1024).toFixed(0)} MB（约 ${(totalKb / 1048576).toFixed(2)} GB）`)

  if (opt.dryRun) {
    console.log('\n(--dry-run 不执行下载)')
    return
  }

  // 3. 跳过已存在
  let toDownload = plan
  if (opt.skipExisting) {
    toDownload = plan.filter(p => !fs.existsSync(p.file))
    console.log(`  · 跳过已存在：${plan.length - toDownload.length} 张，待下载 ${toDownload.length} 张`)
  }

  // 4. 并发下载
  const pool = createPool(opt.concurrency)
  const start = Date.now()
  const tickInterval = setInterval(() => {
    const p = pool.results
    const done = p.ok + p.fail + p.skipped
    if (done === 0) return
    const elapsed = (Date.now() - start) / 1000
    const rate = done / elapsed
    const remain = (toDownload.length - done) / Math.max(rate, 0.01)
    process.stdout.write(
      `\r  · 进度 ${done}/${toDownload.length}  ✓${p.ok} ⊝${p.skipped} ✗${p.fail} · ${rate.toFixed(1)} 张/s · 预计剩余 ${remain.toFixed(0)}s   `
    )
  }, 500)

  for (const t of toDownload) {
    pool.push(async () => {
      const buf = await fetchTile(t.url, opt.retries)
      if (buf === null) return 'skip' // 空白区域（高德无数据）
      fs.mkdirSync(path.dirname(t.file), { recursive: true })
      fs.writeFileSync(t.file, buf)
      return 'ok'
    })
  }

  // 等待完成
  while (pool.results.ok + pool.results.fail + pool.results.skipped < toDownload.length) {
    await new Promise(r => setTimeout(r, 200))
  }
  clearInterval(tickInterval)

  const p = pool.results
  const elapsed = ((Date.now() - start) / 1000).toFixed(1)
  console.log(`\n\n✅ 完成：成功 ${p.ok}，跳过（无数据）${p.skipped}，失败 ${p.fail}，耗时 ${elapsed}s`)
  console.log(`📂 输出目录：${path.relative(PROJECT_ROOT, opt.out)}`)
  console.log(`\n如需在 MapPanel 引用本目录，请确保 .env 中存在：`)
  console.log(`  VITE_TILE_URL=/tiles/{z}/{x}/{y}.${ext}`)
  if (p.fail > 0) process.exit(1)
}

main().catch(err => {
  console.error('❌ 致命错误：', err)
  process.exit(1)
})
