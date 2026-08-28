<template>
  <div class="modal-mask" v-if="open" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-hd">
        <span>{{ mode === 'add' ? '➕ 添加摄像头设备' : '✎ 配置设备（计数线 / 锚点）' }}</span>
        <span class="x" @click="$emit('close')">✕</span>
      </div>
      <div class="modal-bd">
        <div class="field" v-if="mode === 'add'">
          <label>设备 ID<span class="req">*</span></label>
          <input v-model="f.id" placeholder="如 cam-001（全局唯一）" />
          <div class="hint">对应文档 device_id</div>
        </div>
        <div class="field">
          <label>设备名称<span class="req">*</span></label>
          <input v-model="f.name" :disabled="mode === 'edit'" placeholder="如 北门摄像头" />
        </div>
        <div class="field" v-if="mode === 'add'">
          <label>视频流地址<span class="req">*</span></label>
          <input v-model="f.stream_url" placeholder="rtsp://... 或 本地文件" />
        </div>
        <div class="field">
          <label>摄像头类型 camera_type</label>
          <select v-model="f.camera_type">
            <option value="">全部检测(null)</option>
            <option value="vehicle">仅机动车(vehicle)</option>
            <option value="person">仅人流/非机动车(person)</option>
          </select>
        </div>
        <div class="field">
          <label>计数方向 count_only</label>
          <select v-model="f.count_only">
            <option value="">双向计数(null)</option>
            <option value="enter">仅 Enter</option>
            <option value="exit">仅 Exit</option>
          </select>
        </div>
        <div class="field">
          <label>计数线 line_coords</label>
          <input v-model="f.line_coords" placeholder="x1,y1,x2,y2（归一化 0-1）" />
          <div class="hint">视频画面内归一化坐标（§2.3 / §3.1）</div>
        </div>
        <div class="field">
          <label>内侧锚点 anchor_coords</label>
          <input v-model="f.anchor_coords" placeholder="x,y（归一化 0-1）" />
        </div>
        <div class="field">
          <label>ROI 多边形 roi_coords</label>
          <input v-model="f.roi_coords" placeholder="x1,y1,x2,y2,…（≥3 顶点）" />
        </div>
        <div class="field">
          <label>拥堵阈值 max_vehicles</label>
          <input v-model="f.max_vehicles" type="number" min="0" placeholder="0 = 不启用拥挤判断" />
          <div class="hint">ROI 内最大车辆数，>0 时开启拥挤判断（§4.4）</div>
        </div>
        <div class="field" v-if="pos">
          <label>地图落点（前端维护）</label>
          <div class="kv"><span class="k">经度</span><span class="v">{{ pos.lng.toFixed(5) }}</span></div>
          <div class="kv"><span class="k">纬度</span><span class="v">{{ pos.lat.toFixed(5) }}</span></div>
          <div class="hint">文档设备模型无地理字段，落点仅前端用于地图定位</div>
        </div>
      </div>
      <div class="modal-ft">
        <button v-if="mode === 'edit'" class="tool-btn danger" @click="$emit('remove', f.id)">🗑 删除</button>
        <button class="tool-btn" @click="$emit('close')">取消</button>
        <button class="tool-btn primary" @click="onSave">保存</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  mode: { type: String, default: 'add' }, // 'add' | 'edit'
  device: { type: Object, default: null },
  pos: { type: Object, default: null }
})
const emit = defineEmits(['close', 'save', 'remove'])

const f = reactive({
  id: '', name: '', stream_url: '', camera_type: '', count_only: '',
  line_coords: '0.5,0.1,0.5,0.9', anchor_coords: '0.5,0.9', roi_coords: '', max_vehicles: 0
})

// 打开时从 device 回填（编辑模式）
watch(() => props.open, (v) => {
  if (!v) return
  const d = props.device || {}
  f.id = d.id || ''
  f.name = d.name || ''
  f.stream_url = d.stream_url || ''
  f.camera_type = d.camera_type || ''
  f.count_only = d.count_only || ''
  f.line_coords = d.line_coords || '0.5,0.1,0.5,0.9'
  f.anchor_coords = d.anchor_coords || '0.5,0.9'
  f.roi_coords = d.roi_coords || ''
  f.max_vehicles = d.max_vehicles || 0
})

function onSave() {
  emit('save', {
    mode: props.mode,
    id: f.id,
    payload: {
      id: f.id,
      name: f.name,
      stream_url: f.stream_url,
      camera_type: f.camera_type || null,
      count_only: f.count_only || null,
      line_coords: f.line_coords,
      anchor_coords: f.anchor_coords,
      roi_coords: f.roi_coords,
      max_vehicles: Number(f.max_vehicles) || 0
    },
    pos: props.pos
  })
}
</script>
