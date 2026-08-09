#!/bin/bash
# ============================================================
# 全接口模拟测试 - 覆盖业务后端(8000) + AI服务(8001) 所有 REST 端点
# 依赖: redis + backend + ai + mock_wvp(18080) 已启动
# ============================================================
set -u

BACKEND="http://localhost:8000"
AI="http://localhost:8001"
PASS=0
FAIL=0
ERRORS=()

# WVP 同步设备 ID (mock_wvp 提供)
VEHICLE_DEV="GB-34020000001320000001-34020000001320000001"
PERSON_DEV="GB-34020000001320000002-34020000001320000002"

check() {
  # $1=描述 $2=期望状态码前缀(如 2 4) $3=实际状态码 $4=响应片段
  local desc="$1" expect="$2" code="$3" body="$4"
  local prefix="${code:0:1}"
  if [ "$prefix" = "$expect" ] || ([ "$expect" = "2" ] && [ "$prefix" = "2" ]); then
    printf "  ✓ [%s] %s | body: %.80s\n" "$code" "$desc" "$body"
    PASS=$((PASS+1))
  else
    printf "  ✗ [%s] %s (期望 %sxx) | body: %.80s\n" "$code" "$desc" "$expect" "$body"
    FAIL=$((FAIL+1))
    ERRORS+=("[$code] $desc")
  fi
}

req() {
  # $1=method $2=url $3=data(optional) → 打印 "code|body"
  local method="$1" url="$2" data="${3:-}"
  local resp
  if [ -n "$data" ]; then
    resp=$(curl -s -w "\n%{http_code}" -X "$method" "$url" -H "Content-Type: application/json" -d "$data" 2>/dev/null)
  else
    resp=$(curl -s -w "\n%{http_code}" -X "$method" "$url" 2>/dev/null)
  fi
  local code=$(echo "$resp" | tail -1)
  local body=$(echo "$resp" | sed '$d')
  echo "${code}|${body}"
}

echo "============================================================"
echo "  全接口模拟测试  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

# ============ 1. 系统 / 实时统计 ============
echo ""
echo "─── 1. 系统 & 实时统计 ───"
r=$(req GET "$BACKEND/health"); check "GET /health" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/realtime"); check "GET /api/stats/realtime" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/devices"); check "GET /api/stats/devices" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/devices/$VEHICLE_DEV"); check "GET /api/stats/devices/{id}" 2 "${r%%|*}" "${r#*|}"

# ============ 2. 事件 (POST + GET 历史) ============
echo ""
echo "─── 2. 事件接收 & 历史查询 ───"
r=$(req POST "$BACKEND/api/events" '{"device_id":"TEST-CAM","event_type":"VehicleEnter","occurred_at":"2026-08-09T12:00:00+00:00"}'); check "POST /api/events VehicleEnter" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"TEST-CAM","event_type":"VehicleExit","occurred_at":"2026-08-09T12:00:01+00:00"}'); check "POST /api/events VehicleExit" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"TEST-CAM","event_type":"PersonEnter","occurred_at":"2026-08-09T12:00:02+00:00"}'); check "POST /api/events PersonEnter" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"TEST-CAM","event_type":"PersonExit","occurred_at":"2026-08-09T12:00:03+00:00"}'); check "POST /api/events PersonExit" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/events?limit=10"); check "GET /api/events?limit=10 (历史)" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"TEST-CAM","event_type":"VehicleCrash","occurred_at":"2026-08-09T12:00:04+00:00"}'); check "POST /api/events 非法event_type→400" 4 "${r%%|*}" "${r#*|}"

# ============ 3. 告警 ============
echo ""
echo "─── 3. 告警 ───"
r=$(req GET "$BACKEND/api/alerts?limit=10"); check "GET /api/alerts" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/alerts/anomaly" '{"device_id":"TEST-CAM","anomaly_type":"black_screen","phase":"onset"}'); check "POST /api/alerts/anomaly 黑屏" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/alerts/anomaly" '{"device_id":"TEST-CAM","anomaly_type":"flower_screen","phase":"recovery"}'); check "POST /api/alerts/anomaly 花屏恢复" 2 "${r%%|*}" "${r#*|}"

# ============ 4. 设备 CRUD + WVP ============
echo ""
echo "─── 4. 设备管理 & WVP ───"
r=$(req POST "$BACKEND/api/devices" '{"id":"manual_cam","name":"手动测试摄像头","stream_url":"rtsp://test/stream","line_coords":"0.1,0.4,0.9,0.4","anchor_coords":"0.5,0.9","count_only":"enter","camera_type":"vehicle"}'); check "POST /api/devices 注册(手动)" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices" '{"id":"bad_cam","name":"bad","stream_url":"rtsp://x","count_only":"Enter"}'); check "POST /api/devices 非法count_only→422" 4 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/devices"); check "GET /api/devices 列表" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/manual_cam/heartbeat"); check "POST /api/devices/{id}/heartbeat" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/sync"); check "POST /api/devices/sync WVP同步" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/wvp-webhook" '{"action":"online","device_id":"34020000001320000001"}'); check "POST /api/devices/wvp-webhook" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/devices/$VEHICLE_DEV/stream"); check "GET /api/devices/{id}/stream 流地址" 2 "${r%%|*}" "${r#*|}"

# snapshot 返回 image/jpeg, 用 -o 丢弃 body 只看 code
snap_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/api/devices/$VEHICLE_DEV/snapshot" 2>/dev/null)
check "GET /api/devices/{id}/snapshot 截帧" 2 "$snap_code" "(image/jpeg)"

# enable: 为 WVP 同步的车流设备配置计数线并启流
r=$(req POST "$BACKEND/api/devices/$VEHICLE_DEV/enable" '{"line_coords":"0.1,0.4,0.9,0.4","anchor_coords":"0.5,0.9","count_only":"enter","camera_type":"vehicle"}'); check "POST /api/devices/{id}/enable 启流" 2 "${r%%|*}" "${r#*|}"

r=$(req DELETE "$BACKEND/api/devices/manual_cam"); check "DELETE /api/devices/{id} 删除" 2 "${r%%|*}" "${r#*|}"

# ============ 5. 警力分配 ============
echo ""
echo "─── 5. 警力分配 ───"
r=$(req POST "$BACKEND/api/police/regions" '{"id":"R1","name":"北门区域","center_x":0.5,"center_y":0.5,"device_id":"'"$VEHICLE_DEV"'"}'); check "POST /api/police/regions 注册区域" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/regions"); check "GET /api/police/regions 区域列表" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/police/total" '{"total":50}'); check "POST /api/police/total 设置总警力" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/allocation"); check "GET /api/police/allocation 分配状态" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/police/optimize"); check "POST /api/police/optimize 触发优化" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/plan"); check "GET /api/police/plan 分配方案" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/police/regions/R1"); check "DELETE /api/police/regions/{id}" 2 "${r%%|*}" "${r#*|}"

# ============ 6. 时序预测 ============
echo ""
echo "─── 6. 时序预测 ───"
r=$(req GET "$BACKEND/api/prediction/health"); check "GET /api/prediction/health" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/prediction/predict"); check "POST /api/prediction/predict 预测" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/prediction/latest"); check "GET /api/prediction/latest 最新预测" 2 "${r%%|*}" "${r#*|}"

# ============ 7. AI 服务 ============
echo ""
echo "─── 7. AI 分析服务 (8001) ───"
r=$(req GET "$AI/health"); check "GET AI /health" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$AI/devices"); check "GET AI /devices 管道列表" 2 "${r%%|*}" "${r#*|}"
# 注册一个本地视频管道 (cv2 可直接读本地文件)
r=$(req POST "$AI/devices" '{"device_id":"ai_test","stream_url":"/app/data/test_50f.mp4","line":[[0.1,0.4],[0.9,0.4]],"anchor":[0.5,0.9],"count_only":null,"camera_type":"vehicle"}'); check "POST AI /devices 注册管道" 2 "${r%%|*}" "${r#*|}"
sleep 3
r=$(req GET "$AI/devices"); check "GET AI /devices 管道已运行" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$AI/devices/ai_test"); check "DELETE AI /devices/{id} 停止管道" 2 "${r%%|*}" "${r#*|}"

# ============ 8. 数据一致性校验 ============
echo ""
echo "─── 8. 数据一致性校验 ───"
r=$(req GET "$BACKEND/api/stats/realtime")
body="${r#*|}"
echo "  实时统计: $body"
r=$(req GET "$BACKEND/api/events?limit=20")
body="${r#*|}"
evcount=$(echo "$body" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
echo "  事件历史条数: $evcount"

# ============ 汇总 ============
echo ""
echo "============================================================"
echo "  测试完成: ✓ 通过 $PASS / ✗ 失败 $FAIL"
if [ ${#ERRORS[@]} -gt 0 ]; then
  echo "  失败项:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "============================================================"
[ "$FAIL" -eq 0 ]
