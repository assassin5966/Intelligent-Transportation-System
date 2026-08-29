<template>
  <div class="card">
    <div class="card-hd">👮 警力分配</div>
    <div class="card-bd">
      <div class="field">
        <label>总警力</label>
        <input v-model="total" type="number" min="0" />
      </div>
      <button class="tool-btn" style="width:100%" @click="setTotal">设置总警力</button>

      <button class="tool-btn primary" style="width:100%;margin-top:12px" @click="optimize">⚙ 触发分配优化</button>

      <div v-if="planHint" style="margin-top:10px;color:#e6a23c;font-size:12px;line-height:1.5">{{ planHint }}</div>

      <div v-if="plan" style="margin-top:10px">
        <div class="kv"><span class="k">总警力</span><span class="v">{{ plan.total_officers }}</span></div>
        <div class="kv"><span class="k">覆盖评分</span><span class="v">{{ pct(plan.summary.coverage_score) }}</span></div>
        <div class="kv"><span class="k">效率评分</span><span class="v">{{ pct(plan.summary.efficiency_score) }}</span></div>
        <div class="kv"><span class="k">综合评分</span><span class="v good">{{ pct(plan.summary.overall_score) }}</span></div>
        <div class="score-bar"><i :style="{ width: pct(plan.summary.overall_score) }"></i></div>

        <div v-if="plan.regions && plan.regions.length" class="alloc-sec">
          <div class="alloc-hd">区域警力</div>
          <div v-for="rg in plan.regions" :key="rg.region_id" class="kv">
            <span class="k" :title="rg.name">{{ rg.name }}</span>
            <span class="v">
              {{ rg.target_officers }}人
              <em v-if="rg.delta" class="alloc-delta" :class="rg.delta > 0 ? 'up' : 'down'">{{ rg.delta > 0 ? '+' + rg.delta : rg.delta }}</em>
            </span>
          </div>
        </div>

        <div v-if="plan.movements && plan.movements.length" class="alloc-sec">
          <div class="alloc-hd">调动方案</div>
          <div v-for="(mv, i) in plan.movements" :key="i" class="kv">
            <span class="k">{{ rname(mv.from_region) }} → {{ rname(mv.to_region) }}</span>
            <span class="v">{{ mv.count }}人</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useToast } from '../composables/useToast.js'
import { setTotalOfficers, getAllocation, optimizePolice, getPolicePlan } from '../api/endpoints.js'

const { push } = useToast()
const total = ref(20)
const plan = ref(null)
const planHint = ref('')   // 方案区提示（空态/引导文案）

async function setTotal() {
  try { await setTotalOfficers(Number(total.value)); push('总警力已设为 ' + total.value, 'success') } catch (e) { push('失败：' + (e.detail || e.message), 'error') }
}
async function optimize() {
  try {
    const r = await optimizePolice()
    plan.value = r
    planHint.value = ''
    push('优化完成，综合评分 ' + pct(r.summary.overall_score), 'success')
  } catch (e) {
    if (e.status === 400) planHint.value = '无法优化：请先在业务规则页注册警力区域并设置总警力（>0）'
    push('优化失败：' + (e.detail || e.message), 'error')
  }
}

/** 加载最近方案；此前从未优化过(404)时，若总警力已就绪则自动优化一次 */
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
    const hasTotal = Number(alloc?.total_officers) > 0
    if (hasTotal) {
      const r = await optimizePolice()
      plan.value = r
      planHint.value = ''
      push('总警力已就绪，已自动生成分配方案', 'success')
    } else {
      planHint.value = '暂无方案：请先设置总警力(>0)后点击「⚙ 触发分配优化」'
    }
  } catch (e) {
    planHint.value = e.status === 400
      ? '暂无方案：请先在业务规则页注册警力区域并设置总警力(>0)'
      : '暂无方案：' + (e.detail || e.message)
  }
}
function pct(v) { return Math.round((v || 0) * 100) + '%' }

/** 由 region_id 查区域名称（调动方案的 from/to 展示用） */
function rname(rid) {
  for (const rg of plan.value?.regions || []) if (rg.region_id === rid) return rg.name
  return rid
}

onMounted(async () => {
  await loadPlan()
})
</script>

<style scoped>
.alloc-sec {
  margin-top: 10px;
  border-top: 1px dashed rgba(120, 160, 220, .18);
  padding-top: 6px;
}
.alloc-hd {
  font-size: 12px;
  color: #7fd6ff;
  font-weight: 700;
  margin-bottom: 2px;
}
.alloc-delta {
  font-style: normal;
  font-size: 11px;
  margin-left: 4px;
  font-weight: 700;
}
.alloc-delta.up { color: #5fe39a; }
.alloc-delta.down { color: #ff8a8a; }
</style>
