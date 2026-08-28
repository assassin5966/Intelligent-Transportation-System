/**
 * 全局 Toast 提示（单例）
 * ---------------------------------------------------------------------------
 * 所有接口异常 / 成功反馈统一通过此处弹出，避免散落的 alert()。
 * 用法：const { push } = useToast(); push('删除成功', 'success')
 */
import { reactive } from 'vue'

// 模块级单例：所有组件共享同一提示栈
const state = reactive({ list: [] })
let seq = 0

/**
 * 弹出一条提示
 * @param {string} msg   文案
 * @param {'info'|'success'|'warn'|'error'} [type]
 * @param {number} [duration] 毫秒，默认 3200
 */
function push(msg, type = 'info', duration = 3200) {
  const id = ++seq
  state.list.push({ id, msg, type })
  setTimeout(() => {
    const i = state.list.findIndex((t) => t.id === id)
    if (i >= 0) state.list.splice(i, 1)
  }, duration)
}

export function useToast() {
  return { toasts: state.list, push }
}
