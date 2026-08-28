/**
 * 实时数据层 —— WebSocket 主通道 + REST 兜底（对齐 frontend_api.md v0.11.0）
 * ---------------------------------------------------------------------------
 * v0.11.0 关键变更（相对 v0.9.0）：
 *   - AI 分析服务（8001）**不对前端开放**：前端只连接业务后端 8000 的 /ws，
 *     不再连接 AI /ws（原 crossing_event / tracks / video_anomaly 链路移除）。
 *   - /ws 为唯一主通道，连接即推一次 `stats`，之后每 2 秒推一次；
 *     alert / prediction / police_plan 变化时即时推送。
 *   - `stats` 消息结构：{ type:'stats', data:<全局合计>, devices:<完整设备明细[]>, timestamp }；
 *     前端只需订阅此消息即可同时获得「总人流/总车流」(data) 与「各设备人流/车流/拥挤/经纬度」
 *     (devices)，devices 经 useDevices.applyWsDevices 写入本地状态供地图/卡片/图表消费。
 *   - REST 仅作低频兜底（WS 不可用时），不再承担核心实时刷新。
 *
 * 设计：
 *   - USE_MOCK 模式：不连真实 WebSocket，改用内置 MockSocket 周期推送与后端 /ws 同构的
 *     stats/alert/prediction/police_plan 消息，使 WS 主链路在无后端时也能完整运行与验证。
 *   - 真实模式：连真实 /ws；若 WS 不可用（连接失败/断开），则静默 REST 轮询兜底；
 *     WS 恢复后自动停止冗余轮询。
 *   - 断线指数退避自动重连，断连时友好提示，绝不抛出未捕获错误。
 */
import { reactive } from 'vue'
import {
  getRealtimeStats, listAlerts, getLatestPrediction, getPolicePlan, listEvents
} from '../api/endpoints.js'
import { API_BASE, USE_MOCK } from '../api/config.js'
import { useToast } from './useToast.js'
import { useDevices } from './useDevices.js'

const state = reactive({
  stats: null,        // RealtimeStats（后端 /ws stats.data）
  devices: [],        // 由 WS stats.devices 经 useDevices 落库（此处仅镜像引用，便于调试）
  alerts: [],         // Alert[]（后端 /ws alert）
  events: [],         // 越线事件历史（REST /api/events 低频兜底；前端大屏滚动列表用）
  prediction: null,   // 最近预测（后端 /ws prediction）
  policePlan: null,   // 最近警力方案（后端 /ws police_plan）
  wsBackend: false,
  restMode: false,    // WS 不可用时进入 REST 兜底轮询
  loading: false
})

let backendWs = null
let fallbackTimer = null
let retryB = 0
let stopped = false

function wsUrlOf(base) {
  return base.replace(/^http/, 'ws') + '/ws'
}

/* ===================== Mock WebSocket（USE_MOCK 模式） =====================
 * 模拟后端 /ws：连接即推 stats，之后每 2 秒推一次；周期性推 alert / 初始推 prediction / police_plan。
 * 复用 mock.js 的 mockWsStats / mockWsPrediction / mockWsPolicePlan 构造与后端同构的消息体。
 */
class MockSocket {
  constructor(url) {
    this.url = url
    this.readyState = 0
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    this._timers = []
    // 模拟异步连接建立；按需动态加载 mock 构造器（仅 USE_MOCK 模式触发，不污染真实链路打包）
    setTimeout(async () => {
      if (stopped) return
      try {
        const m = await import('../api/mock.js')
        if (stopped) return
        this.readyState = 1
        if (this.onopen) this.onopen()
        // 连接即推一次 stats
        this._emit(m.mockWsStats())
        // 每 2 秒推一次 stats（对齐文档「每 2 秒」）
        this._timers.push(setInterval(() => this._emit(m.mockWsStats()), 2000))
        // 初始推 prediction / police_plan
        this._emit(m.mockWsPrediction())
        this._emit(m.mockWsPolicePlan())
        // 周期性推告警（演示 alert 链路；含拥堵 + 视频异常两类）
        this._timers.push(setInterval(() => this._emit(this._buildAlert(m)), 9000))
      } catch (e) {
        if (this.onerror) this.onerror(e)
      }
    }, 60)
  }
  _emit(frame) {
    if (this.onmessage) {
      try { this.onmessage({ data: JSON.stringify(frame) }) } catch (e) { /* ignore */ }
    }
  }
  _buildAlert(m) {
    const frame = m.mockWsStats()
    const devs = frame.devices || []
    if (!devs.length) return null
    const d = devs[Math.floor(Math.random() * devs.length)]
    if (Math.random() > 0.5) {
      return {
        type: 'alert',
        data: {
          id: Date.now(),
          rule_id: 'congestion_' + d.device_id,
          level: d.congested ? 'critical' : 'warning',
          category: 'congestion',
          message: `设备 ${d.name} ${d.congested ? '拥堵' : '拥挤预警'} (综合拥挤度 ${d.congestion_score})`,
          created_at: new Date().toISOString(),
          phase: d.congested ? 'onset' : 'recovery',
          device_id: d.device_id,
          congestion_score: d.congestion_score,
          vehicle_congested: d.vehicle_congested,
          person_congested: d.person_congested
        }
      }
    }
    return {
      type: 'alert',
      data: {
        id: Date.now(),
        rule_id: 'video_black_screen',
        level: 'critical',
        category: 'video_anomaly',
        message: `设备 ${d.name} 检测到黑屏异常`,
        created_at: new Date().toISOString(),
        device_id: d.device_id,
        anomaly_type: 'black_screen',
        phase: 'onset'
      }
    }
  }
  close() {
    this.readyState = 3
    this._timers.forEach((t) => clearInterval(t))
    this._timers = []
    if (this.onclose) this.onclose()
  }
}

/** 后端 /ws 连接（真实 or Mock） */
function connectBackend() {
  if (USE_MOCK) {
    backendWs = new MockSocket(wsUrlOf(API_BASE))
  } else {
    try {
      backendWs = new WebSocket(wsUrlOf(API_BASE))
    } catch {
      scheduleReconnect()
      return
    }
  }
  backendWs.onopen = () => { state.wsBackend = true; state.restMode = false; retryB = 0 }
  backendWs.onmessage = (e) => {
    try {
      const msg = JSON.parse(typeof e.data === 'string' ? e.data : '')
      console.log('[WS] 收到消息===========》：', msg)
      dispatch(msg)
    } catch { /* 忽略非法帧 */ }
  }
  backendWs.onclose = () => { state.wsBackend = false; scheduleReconnect() }
  backendWs.onerror = () => { try { backendWs.close() } catch { } }
}

function scheduleReconnect() {
  if (stopped) return
  const { push } = useToast()
  const wait = Math.min(1000 * 2 ** retryB, 15000)
  retryB++
  if (retryB === 1) push('实时通道断开，正在重连…', 'warn')
  setTimeout(connectBackend, wait)
}

/** 按 WS 消息 type 分发（v0.11.0：stats / alert / prediction / police_plan） */
function dispatch(msg) {
  if (!msg || !msg.type) return
  switch (msg.type) {
    case 'stats':
      if (msg.data) state.stats = msg.data
      if (msg.devices) useDevices().applyWsDevices(msg.devices)
      break
    case 'alert': {
      if (!msg.data) break
      state.alerts.unshift(msg.data)
      if (state.alerts.length > 200) state.alerts.length = 200
      // 拥挤告警 onset/recovery 即时 Toast 提示（§5.2）
      if (msg.data.category === 'congestion' && msg.data.device_id) {
        try {
          const isRecovery = msg.data.phase === 'recovery'
          const score = msg.data.congestion_score != null ? ` (拥挤度 ${msg.data.congestion_score})` : ''
          useToast().push(
            `${msg.data.device_id}${score} ${isRecovery ? '拥堵已解除' : '检测到拥堵'}`,
            isRecovery ? 'success' : 'error',
            5000
          )
        } catch { }
      }
      break
    }
    case 'prediction':
      if (msg.data) state.prediction = msg.data
      break
    case 'police_plan':
      if (msg.data) state.policePlan = msg.data
      break
  }
}

/** REST 兜底：仅在真实模式且 WS 不可用时调用，静默失败不弹 Toast */
async function pollOnce() {
  try { state.stats = await getRealtimeStats() } catch { }
  try { state.alerts = await listAlerts(50) } catch { }
  try { state.prediction = await getLatestPrediction() } catch { }
  try { state.policePlan = await getPolicePlan() } catch { }
  try { state.events = await listEvents(50) } catch { }
  // v0.11.0：拥挤/小时数据随 WS stats.devices 返回；WS 不可用时用 REST 设备统计兜底
  try { await useDevices().refreshStats() } catch { }
}

// WS 不可用时才冗余轮询兜底；WS 正常时不轮询，避免双重刷新
function fallbackTick() {
  if (USE_MOCK) return
  if (state.wsBackend) { state.restMode = false; return }
  state.restMode = true
  pollOnce()
}

export function useRealtime() {
  function start() {
    stopped = false
    connectBackend()
    // 真实模式：首屏即时拉一次（让首帧不必等 WS），并以 6s 兜底轮询（仅在 WS 断开时生效）
    if (!USE_MOCK) {
      pollOnce()
      if (!fallbackTimer) fallbackTimer = setInterval(fallbackTick, 6000)
    }
  }
  function stop() {
    stopped = true
    if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null }
    try { backendWs && backendWs.close() } catch { }
  }
  /** 手动全量刷新（按钮触发） */
  async function refreshAll() {
    state.loading = true
    try { await pollOnce() } finally { state.loading = false }
  }
  return { state, start, stop, refreshAll }
}
