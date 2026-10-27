/**
 * 全局 Toast 提示（单例）
 * ---------------------------------------------------------------------------
 * 所有接口异常 / 成功反馈统一通过此处弹出，避免散落的 alert()。
 * 用法：const { push } = useToast(); push('删除成功', 'success')
 *
 * 防刷屏：
 *   - 同「文案 + 类型」在 DEDUPE_WINDOW 内重复触发只保留第一条（实时告警会持续推送同一条消息）；
 *   - 同时最多展示 MAX_VISIBLE 条，超出丢弃最早的，避免堆叠遮挡地图。
 */
import { reactive } from 'vue'

// 模块级单例：所有组件共享同一提示栈
const state = reactive({ list: [] })
let seq = 0

const MAX_VISIBLE = 3       // 同屏最多条数
const DEDUPE_WINDOW = 2500  // 同文案同类型的去重时间窗（毫秒）

/**
 * 弹出一条提示
 * @param {string} msg   文案
 * @param {'info'|'success'|'warn'|'error'} [type]
 * @param {number} [duration] 毫秒，默认 3200
 */
function push(msg, type = 'info', duration = 3200) {
  const now = Date.now()
  const dup = state.list.find((t) => t.msg === msg && t.type === type)
  if (dup && now - dup.at < DEDUPE_WINDOW) return
  const id = ++seq
  state.list.push({ id, msg, type, at: now })
  if (state.list.length > MAX_VISIBLE) state.list.splice(0, state.list.length - MAX_VISIBLE)
  setTimeout(() => {
    const i = state.list.findIndex((t) => t.id === id)
    if (i >= 0) state.list.splice(i, 1)
  }, duration)
}

export function useToast() {
  return { toasts: state.list, push }
}
