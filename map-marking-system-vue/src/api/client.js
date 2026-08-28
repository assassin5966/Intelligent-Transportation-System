/**
 * 统一 API 客户端
 * ---------------------------------------------------------------------------
 * 职责（对应 api(5).md §2 通用约定 / §11 错误码）：
 *   1. 统一请求头（JSON + 可选 Bearer 鉴权）—— §2.1 / §2.4
 *   2. 统一超时（AbortController）—— 防止请求挂死
 *   3. 统一错误处理：错误响应为 { detail }（FastAPI 风格）—— §2.2
 *      状态码映射：400 参数错误 / 401 认证失败 / 403 无权限 /
 *                 404 资源不存在 / 409 冲突 / 5xx 服务端错误 / 504 网关超时
 *   4. 成功响应直接返回数据体（文档无 {code,data} 包装）—— §2.2
 *
 * 设计：所有接口函数（endpoints.js）只描述「路径 + 参数 + 返回类型」，
 * 真正的传输细节全部收敛于此，便于统一拦截与维护。
 */
import { API_BASE, REQUEST_TIMEOUT, buildHeaders, USE_MOCK, MOCK_FALLBACK } from './config.js'
import { useToast } from '../composables/useToast.js'

// Mock 模块按需动态加载：仅当 USE_MOCK / MOCK_FALLBACK 真正触发时才 import，
// 这样在不使用 Mock 时（如纯真实后端对接）可整体移除 mock.js 而不影响真实请求链路。
let _mockRequest = null
async function loadMockRequest() {
  if (!_mockRequest) {
    const m = await import('./mock.js')
    _mockRequest = m.mockRequest
  }
  return _mockRequest
}

// 期望非空的读接口：若真实接口返回 200 + 空体（部分异常后端会如此），同样回退到 Mock
// v0.9.0：移除 /api/stats/congestion（端点已废弃）；新增 hourly history 与业务规则
const EXPECT_NON_EMPTY = new Set([
  '/api/devices',
  '/api/stats/realtime',
  '/api/stats/devices',
  '/api/alerts',
  '/api/prediction/latest',
  '/api/police/regions',
  '/api/police/allocation',
  '/api/police/plan',
  '/api/events',
  '/api/config/business-rules'
])

// 已降级提示（整个会话仅提示一次，避免轮询刷屏）
let _fallbackWarned = false
async function fallbackToMock(method, path, body, query, err) {
  const fn = await loadMockRequest()
  if (!_fallbackWarned) {
    _fallbackWarned = true
    try {
      useToast().push('实时接口暂时不可用，已自动切换至本地模拟数据，页面交互不受影响', 'warn', 4200)
    } catch {}
    console.warn('[API] 接口调用失败，已回退至 Mock 数据：', method, path, err && err.message ? err.message : err)
  }
  return fn(method, path, body, query)
}

/** 统一异常类型：携带 HTTP 状态码与后端 detail 文案 */
export class ApiError extends Error {
  constructor(status, detail, url) {
    super(detail || `请求失败 (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.url = url
  }
}

/** 将 HTTP 状态码转为人话（供 Toast 展示） */
function describeStatus(status, detail) {
  switch (status) {
    case 400: return detail || '请求参数错误'
    case 401: return '认证失败：请检查 Token'
    case 403: return '无权限访问该资源'
    case 404: return detail || '资源不存在'
    case 409: return detail || '资源冲突（已存在）'
    case 422: return detail || '参数校验未通过'
    case 500: return '服务端内部错误'
    case 502: return detail || '网关错误（上游服务不可用）'
    case 503: return detail || '服务暂不可用（如 WVP 未启用）'
    case 504: return detail || '网关超时（如预测推理超时）'
    default: return detail || `请求失败 (${status})`
  }
}

/**
 * 核心请求方法
 * @param {string} method  GET/POST/PUT/DELETE
 * @param {string} path    API 路径，如 /api/devices
 * @param {object} [body]  请求体（仅 POST/PUT）
 * @param {object} [query] 查询参数对象（会被拼成 ?a=1&b=2，自动忽略 null/undefined）
 * @returns {Promise<any>} 解析后的响应数据体
 */
/**
 * 真实 HTTP 请求（不含 Mock 分支；超时 / 状态码映射 / {detail} 拦截逻辑保持一致）
 */
async function realRequest(method, path, body, query) {
  let url = API_BASE + path
  if (query) {
    const sp = new URLSearchParams()
    Object.entries(query).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') sp.append(k, String(v))
    })
    const qs = sp.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT)

  let res
  try {
    res = await fetch(url, {
      method,
      headers: buildHeaders(),
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal
    })
  } catch (e) {
    clearTimeout(timer)
    // 网络异常 / 超时（AbortController 触发）
    if (e.name === 'AbortError') {
      throw new ApiError(0, '网络请求超时，请检查后端是否可达')
    }
    throw new ApiError(0, '网络异常：' + (e.message || '无法连接后端'))
  }
  clearTimeout(timer)

  // 解析响应体：文档成功返回数据体，错误返回 { detail }
  let data = null
  const text = await res.text()
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }

  if (!res.ok) {
    const detail = (data && data.detail) || (typeof data === 'string' ? data : '')
    throw new ApiError(res.status, describeStatus(res.status, detail), url)
  }
  return data
}

/**
 * 核心请求方法
 * @param {string} method  GET/POST/PUT/DELETE
 * @param {string} path    API 路径，如 /api/devices
 * @param {object} [body]  请求体（仅 POST/PUT）
 * @param {object} [query] 查询参数对象
 * @returns {Promise<any>} 解析后的响应数据体
 */
export async function request(method, path, body, query) {
  // —— 全量 Mock 模式：完全由内存后端接管（USE_MOCK=true）——
  if (USE_MOCK) {
    const fn = await loadMockRequest()
    return fn(method, path, body, query)
  }

  // —— 真实请求；失败时按 MOCK_FALLBACK 优雅降级到 Mock 数据 ——
  let data
  try {
    data = await realRequest(method, path, body, query)
  } catch (e) {
    if (MOCK_FALLBACK) return fallbackToMock(method, path, body, query, e)
    throw e
  }

  // 读接口期望有数据却返回空体（异常后端的 200 + 空），同样回退
  if (MOCK_FALLBACK && method === 'GET' && (data === null || data === undefined) && EXPECT_NON_EMPTY.has(path)) {
    return fallbackToMock(method, path, body, query, new Error('empty response body'))
  }
  return data
}

// 便捷方法
export const get = (path, query) => request('GET', path, null, query)
export const post = (path, body) => request('POST', path, body)
export const put = (path, body) => request('PUT', path, body)
export const del = (path) => request('DELETE', path)
