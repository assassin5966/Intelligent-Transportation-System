/**
 * API 全局配置
 * ---------------------------------------------------------------------------
 * 对应 frontend_api.md（v0.11.0）：
 *   - 业务后端默认端口 8000（设备管理 / 统计 / 告警 / 预测 / 警力 / 事件 / WS）
 *   - 【v0.11.0 变更】AI 分析服务（8001）**不对前端开放**：前端只连接业务后端 8000 的
 *     WebSocket(/ws) 获取全部实时数据（stats/alert/prediction/police_plan），不连接 AI 8001。
 * 前端所有 REST 请求与实时推送均发往后端 8000。
 */

// 业务后端基地址。优先级：
//   1. VITE_API_BASE 环境变量（跨域部署时显式指定）；
//   2. 同源地址（默认）：开发环境经 vite.config.js 代理转发到后端 8000（localhost/局域网 IP 均可用）；
//      生产同源部署（页面由后端/网关提供）时直接命中后端。
export const API_BASE = import.meta.env.VITE_API_BASE || location.origin

// AI 分析服务基地址（v0.11.0：AI 服务不对前端开放，前端不连接此地址）。
// 仅保留常量以兼容可能的内部/调试用途，正常前端链路不使用。
export const AI_BASE = import.meta.env.VITE_AI_BASE || 'http://172.16.168.9:8001'

// Bearer Token：文档 §2 未显式定义鉴权，此处按增强需求预留统一鉴权头。
// 留空则不附加 Authorization 头（兼容无鉴权后端）。
export const API_TOKEN = import.meta.env.VITE_API_TOKEN || ''

// 请求超时（毫秒）。文档 §6 预测可能耗时较长，给 30s。
export const REQUEST_TIMEOUT = 30000

// 是否启用内置 Mock 后端（全量替换，无需真实后端即可演示全部接口）。
// 设为 false 即直连真实后端。可用 ?mock=1 临时开启。
export const USE_MOCK =
  String(import.meta.env.VITE_USE_MOCK).toLowerCase() === 'true' ||
  new URLSearchParams(location.search).get('mock') === '1'

// 优雅降级开关：仅当真实接口调用【失败】时，自动回退到内置 Mock 数据。
// 成功响应不会被替换，因此完全不影响真实接口对接；
// 用于「后端挂了 / 网络异常 / 返回空体」时，保证页面列表/详情/表单交互仍可完整流畅运行。
//   - 默认关闭（接口不可用时不回退，直连真实后端；避免 mock 假数据伪装成真实数据）；
//   - 开启：VITE_MOCK_FALLBACK=true 或 URL 加 ?fallback=1；
//   - 强制关闭：?fallback=0。
const _qp = new URLSearchParams(location.search)
const _fb = _qp.get('fallback')
export const MOCK_FALLBACK =
  _fb === '0' ? false :
  _fb === '1' ? true :
  String(import.meta.env.VITE_MOCK_FALLBACK).toLowerCase() === 'true'

// 统一请求头：api(5).md §2.1 要求 JSON，§2.4 后端已配置 CORS。
export function buildHeaders() {
  const h = { 'Content-Type': 'application/json' }
  if (API_TOKEN) h['Authorization'] = 'Bearer ' + API_TOKEN
  return h
}
