<template>
  <div class="card">
    <div class="card-hd">👮 警力分配</div>
    <div class="card-bd">
      <div class="field">
        <label>总警力</label>
        <input v-model="total" type="number" min="0" />
      </div>
      <button class="tool-btn" style="width:100%" @click="setTotal">设置总警力</button>

      <div class="field" style="margin-top:12px">
        <label>注册区域</label>
        <input v-model="form.id" placeholder="区域ID，如 r1" />
        <input v-model="form.name" placeholder="区域名称，如 北广场" style="margin-top:6px" />
        <input v-model="form.center_x" placeholder="center_x" style="margin-top:6px" />
        <input v-model="form.center_y" placeholder="center_y" style="margin-top:6px" />
        <input v-model="form.device_id" placeholder="关联设备ID" style="margin-top:6px" />
      </div>
      <button class="tool-btn" style="width:100%" @click="addRegion">注册区域</button>

      <div style="margin-top:10px">
        <div class="item" v-for="r in regions" :key="r.id">
          <span><span class="dot g"></span>{{ r.name }} <span style="color:#7f93b8">·{{ r.current_officers }}人</span></span>
          <span class="tool-btn danger" style="padding:2px 8px" @click="del(r.id)">删</span>
        </div>
      </div>

      <button class="tool-btn primary" style="width:100%;margin-top:10px" @click="optimize">⚙ 触发分配优化</button>

      <div v-if="planHint" style="margin-top:10px;color:#e6a23c;font-size:12px;line-height:1.5">{{ planHint }}</div>

      <div v-if="plan" style="margin-top:10px">
        <div class="kv"><span class="k">总警力</span><span class="v">{{ plan.total_officers }}</span></div>
        <div class="kv"><span class="k">覆盖评分</span><span class="v">{{ pct(plan.summary.coverage_score) }}</span></div>
        <div class="kv"><span class="k">效率评分</span><span class="v">{{ pct(plan.summary.efficiency_score) }}</span></div>
        <div class="kv"><span class="k">综合评分</span><span class="v good">{{ pct(plan.summary.overall_score) }}</span></div>
        <div class="score-bar"><i :style="{ width: pct(plan.summary.overall_score) }"></i></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useToast } from '../composables/useToast.js'
import {
  listRegions, registerRegion, deleteRegion, setTotalOfficers, getAllocation, optimizePolice, getPolicePlan
} from '../api/endpoints.js'

const { push } = useToast()
const regions = ref([])
const total = ref(20)
const form = ref({ id: '', name: '', center_x: 0.3, center_y: 0.4, device_id: '' })
const plan = ref(null)
const planHint = ref('')   // 方案区提示（空态/引导文案）

async function reload() {
  try { regions.value = await listRegions() } catch (e) { push('区域加载失败：' + (e.detail || e.message), 'warn') }
}
async function setTotal() {
  try { await setTotalOfficers(Number(total.value)); push('总警力已设为 ' + total.value, 'success') } catch (e) { push('失败：' + (e.detail || e.message), 'error') }
}
async function addRegion() {
  const f = form.value
  if (!f.id || !f.name || !f.device_id) { push('区域 ID / 名称 / 设备ID 为必填', 'warn'); return }
  try {
    await registerRegion({ id: f.id, name: f.name, center_x: Number(f.center_x), center_y: Number(f.center_y), device_id: f.device_id })
    push('区域「' + f.name + '」已注册', 'success'); await reload()
  } catch (e) { push('注册失败：' + (e.detail || e.message), 'error') }
}
async function del(id) {
  try { await deleteRegion(id); push('区域已删除', 'success'); await reload() } catch (e) { push('删除失败：' + (e.detail || e.message), 'error') }
}
async function optimize() {
  try {
    const r = await optimizePolice()
    plan.value = r
    planHint.value = ''
    push('优化完成，综合评分 ' + pct(r.summary.overall_score), 'success')
  } catch (e) {
    if (e.status === 400) planHint.value = '无法优化：请先注册警力区域并设置总警力（>0）'
    push('优化失败：' + (e.detail || e.message), 'error')
  }
}

/** 加载最近方案；此前从未优化过(404)时，若区域+总警力已就绪则自动优化一次 */
async function loadPlan() {
  try {
    plan.value = await getPolicePlan()
    planHint.value = ''
  } catch (e) {
    if (e.status === 404) await tryAutoOptimize()
    else planHint.value = '方案加载失败：' + (e.detail || e.message)
  }
}

async function tryAutoOptimize() {
  try {
    const alloc = await getAllocation()
    const hasRegions = regions.value.length > 0
    const hasTotal = Number(alloc?.total_officers) > 0
    if (hasRegions && hasTotal) {
      const r = await optimizePolice()
      plan.value = r
      planHint.value = ''
      push('区域与总警力已就绪，已自动生成分配方案', 'success')
    } else {
      planHint.value = hasRegions
        ? '暂无方案：请先设置总警力(>0)后点击「⚙ 触发分配优化」'
        : '暂无方案：请先注册警力区域并设置总警力(>0)，再点击「⚙ 触发分配优化」'
    }
  } catch (e) {
    planHint.value = e.status === 400
      ? '暂无方案：请先注册警力区域并设置总警力(>0)'
      : '暂无方案：' + (e.detail || e.message)
  }
}
function pct(v) { return Math.round((v || 0) * 100) + '%' }

onMounted(async () => {
  await reload()
  await loadPlan()
})
</script>
