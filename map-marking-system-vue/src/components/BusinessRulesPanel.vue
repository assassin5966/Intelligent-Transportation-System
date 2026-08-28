<template>
  <div class="modal-mask" v-if="open" @click.self="$emit('close')">
    <div class="modal rules-modal">
      <div class="modal-hd">
        <span>⚙ 业务规则配置 <span class="tag">保存即热重载</span></span>
        <span class="x" @click="$emit('close')">✕</span>
      </div>
      <div class="modal-bd">
        <div class="rules-loading" v-if="loading">配置读取中…</div>
        <div class="rules-error" v-else-if="error">
          <span>⚠ {{ error }}</span>
          <button class="tool-btn mini" @click="load">重试</button>
        </div>
        <template v-else>
          <div class="rules-file" v-if="meta.file">
            来源：{{ meta.file }} · 热重载：{{ meta.hot_reload ? '已启用' : '未启用' }}
          </div>
          <div class="rules-group" v-for="g in groups" :key="g.key">
            <div class="rules-group-hd">
              <span class="rules-group-title">{{ g.title }}</span>
              <span class="rules-group-desc">{{ g.desc }}</span>
            </div>
            <div class="rules-param" v-for="p in g.params" :key="g.key + '.' + p.key">
              <div class="rules-param-info">
                <span class="rules-param-label">{{ p.label }}</span>
                <span class="rules-param-desc">{{ p.desc }}</span>
              </div>
              <div class="rules-param-edit">
                <input
                  type="number"
                  :step="p.step || (p.type === 'int' ? 1 : 0.01)"
                  :min="p.min" :max="p.max"
                  v-model.number="p.editValue"
                  :class="{ dirty: isDirty(g, p), invalid: !isValid(p) }"
                  @input="touch(g, p)"
                />
                <span class="rules-default" v-if="p.default != null" @click="resetParam(g, p)"
                  title="点击恢复出厂默认值">默认 {{ p.default }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
      <div class="modal-ft">
        <button class="tool-btn" @click="resetAll" :disabled="loading || !!error">↺ 全部恢复默认</button>
        <button class="tool-btn" @click="$emit('close')">取消</button>
        <button class="tool-btn primary" :disabled="loading || !!error || saving || dirtyCount === 0" @click="save">
          {{ saving ? '保存中…' : (dirtyCount > 0 ? `保存 ${dirtyCount} 项修改` : '保存') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 业务规则配置面板 —— api(8).md §10（v0.9.0 新增）
 * ---------------------------------------------------------------------------
 * - GET  /api/config/business-rules 读取分组参数（§10.1）；
 * - PUT  /api/config/business-rules 仅提交修改项 { 分组: { 参数: 值 } }（§10.2），保存后热重载生效；
 * - 前端预校验：按元数据 type/min/max 校验，非法输入标红并禁用保存（后端同样 400 拦截）；
 * - 错误处理：400 无有效参数/类型范围非法（detail 含参数名）、500 规则文件不存在、网络异常；
 * - 加载状态：读取/保存期间禁用操作按钮。
 */
import { ref, computed, watch } from 'vue'
import { getBusinessRules, updateBusinessRules } from '../api/endpoints.js'
import { useToast } from '../composables/useToast.js'

const props = defineProps({ open: { type: Boolean, default: false } })
defineEmits(['close'])

const { push } = useToast()

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const meta = ref({ file: '', hot_reload: true })
const groups = ref([])

/** 仅提交被修改过的参数：{ 分组: { 参数: 值 } } */
const patch = computed(() => {
  const out = {}
  groups.value.forEach((g) => {
    g.params.forEach((p) => {
      if (isDirty(g, p) && isValid(p)) {
        if (!out[g.key]) out[g.key] = {}
        out[g.key][p.key] = p.type === 'int' ? parseInt(p.editValue, 10) : parseFloat(p.editValue)
      }
    })
  })
  return out
})

const dirtyCount = computed(() => Object.values(patch.value).reduce((s, o) => s + Object.keys(o).length, 0))

function isDirty(g, p) {
  return p.editValue !== p.value
}
function isValid(p) {
  const v = p.editValue
  const num = Number(v)
  if (v === '' || v === null || v === undefined || !Number.isFinite(num)) return false
  if (p.min != null && num < p.min) return false
  if (p.max != null && num > p.max) return false
  return true
}
function touch() { /* v-model 已更新 editValue，仅作占位 */ }
function resetParam(g, p) {
  p.editValue = p.default
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await getBusinessRules()
    meta.value = { file: res.file || '', hot_reload: !!res.hot_reload }
    groups.value = (res.groups || []).map((g) => ({
      ...g,
      params: (g.params || []).map((p) => ({ ...p, editValue: p.value }))
    }))
  } catch (e) {
    error.value = (e.detail || e.message || '读取业务规则失败')
    push('业务规则读取失败：' + error.value, 'error', 4000)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (dirtyCount.value === 0) return
  // 前端预校验（后端 §10.2 亦会以 400 拦截）
  for (const g of groups.value) {
    for (const p of g.params) {
      if (isDirty(g, p) && !isValid(p)) {
        push(`参数「${p.label}」取值非法（应为 ${p.type}，范围 ${p.min ?? '-∞'} ~ ${p.max ?? '∞'}）`, 'error', 4000)
        return
      }
    }
  }
  saving.value = true
  try {
    const res = await updateBusinessRules(patch.value)
    // 用响应中的最新 groups 回写（含更新后的 value）
    if (res && res.groups) {
      groups.value = res.groups.map((g) => ({
        ...g,
        params: (g.params || []).map((p) => ({ ...p, editValue: p.value }))
      }))
    }
    push(res?.message || '业务规则已保存，热重载已生效', 'success', 4000)
  } catch (e) {
    // 400 无有效参数/类型范围非法 / 500 规则文件不存在 / 0 网络异常
    push('业务规则保存失败：' + (e.detail || e.message || '请求失败'), 'error', 5000)
  } finally {
    saving.value = false
  }
}

function resetAll() {
  groups.value.forEach((g) => g.params.forEach((p) => { p.editValue = p.default }))
}

// 打开时拉取最新配置（每次打开都重新读取，避免脏数据）
watch(() => props.open, (v) => { if (v) load() })
</script>

<style scoped>
.rules-modal {
  width: min(640px, 92vw);
  max-height: 82vh;
  display: flex;
  flex-direction: column;
}
.rules-modal .modal-bd {
  overflow-y: auto;
  padding: 12px 16px;
}
.rules-file {
  font-size: 11px;
  color: #7f93b8;
  margin-bottom: 10px;
}
.rules-loading {
  text-align: center;
  color: #7f93b8;
  padding: 40px 0;
  font-size: 12px;
}
.rules-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: rgba(255, 107, 107, 0.1);
  border: 1px solid rgba(255, 107, 107, 0.3);
  color: #ff8a8a;
  font-size: 12px;
  margin: 20px 0;
}
.rules-group {
  margin-bottom: 14px;
  border: 1px solid rgba(0, 225, 255, 0.12);
  border-radius: 8px;
  overflow: hidden;
}
.rules-group-hd {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 12px;
  background: rgba(0, 225, 255, 0.05);
}
.rules-group-title {
  font-size: 13px;
  font-weight: 600;
  color: #d4e4ff;
}
.rules-group-desc {
  font-size: 11px;
  color: #7f93b8;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rules-param {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 12px;
  border-top: 1px solid rgba(0, 225, 255, 0.06);
}
.rules-param-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.rules-param-label {
  font-size: 12px;
  color: #d4e4ff;
  flex-shrink: 0;
}
.rules-param-desc {
  font-size: 10px;
  color: #7f93b8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rules-param-edit {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.rules-param-edit input {
  width: 96px;
  padding: 3px 8px;
  font-size: 12px;
  text-align: right;
  color: #d4e4ff;
  background: rgba(0, 225, 255, 0.06);
  border: 1px solid rgba(0, 225, 255, 0.2);
  border-radius: 4px;
  outline: none;
}
.rules-param-edit input:focus {
  border-color: rgba(0, 225, 255, 0.5);
}
.rules-param-edit input.dirty {
  border-color: rgba(255, 210, 63, 0.6);
}
.rules-param-edit input.invalid {
  border-color: rgba(255, 107, 107, 0.7);
  color: #ff8a8a;
}
.rules-default {
  font-size: 10px;
  color: #7f93b8;
  cursor: pointer;
  user-select: none;
}
.rules-default:hover {
  color: #00e1ff;
}
:deep(.modal-ft) {
  justify-content: flex-end;
}
</style>
