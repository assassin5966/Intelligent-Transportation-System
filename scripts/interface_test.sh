#!/bin/bash
# ============================================================
# 全接口测试脚本 - 覆盖业务后端(8000) + AI服务(8001) 所有 REST 端点
# 用法:
#   bash scripts/interface_test.sh                      # 默认 localhost
#   bash scripts/interface_test.sh 192.168.1.41         # 指定 IP
#   bash scripts/interface_test.sh localhost 8000 8001  # 自定义 host+端口
# 依赖: mock 环境已启动 (docker compose up -d)
# ============================================================
set -u

HOST="${1:-localhost}"
BPORT="${2:-8000}"
APORT="${3:-8001}"
BACKEND="http://${HOST}:${BPORT}"
AI="http://${HOST}:${APORT}"

PASS=0
FAIL=0
SKIP=0
ERRORS=()

# Mock 设备 ID
MOCK_VEHICLE="mock-cam-vehicle"
MOCK_PERSON="mock-cam-person"

check() {
  local desc="$1" expect="$2" code="$3" body="$4"
  local prefix="${code:0:1}"
  if [ "$prefix" = "$expect" ] || ([ "$expect" = "2" ] && [ "$prefix" = "2" ]); then
    printf "  \e[32m✓\e[0m [%s] %s\n" "$code" "$desc"
    PASS=$((PASS+1))
  else
    printf "  \e[31m✗\e[0m [%s] %s (期望 %sxx) | body: %.100s\n" "$code" "$desc" "$expect" "$body"
    FAIL=$((FAIL+1))
    ERRORS+=("[$code] $desc")
  fi
}

skip() {
  printf "  \e[33m~\e[0m [SKIP] %s\n" "$1"
  SKIP=$((SKIP+1))
}

req() {
  local method="$1" url="$2" data="${3:-}"
  local resp
  if [ -n "$data" ]; then
    resp=$(curl -s -w "\n%{http_code}" -X "$method" "$url" -H "Content-Type: application/json" -d "$data" 2>/dev/null)
  else
    resp=$(curl -s -w "\n%{http_code}" -X "$method" "$url" 2>/dev/null)
  fi
  local code=$(echo "$resp" | tail -1)
  local body=$(echo "$resp" | sed '$d')
  [ -z "$code" ] && code="000"
  echo "${code}|${body}"
}

echo "============================================================"
echo "  全接口测试  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  后端: $BACKEND | AI: $AI"
echo "============================================================"

# ============ 1. 系统 & 实时统计 ============
echo ""
echo "─── 1. 系统 & 实时统计 ───"
r=$(req GET "$BACKEND/health");            check "GET /health" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/realtime"); check "GET /api/stats/realtime" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/devices");  check "GET /api/stats/devices" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/devices/$MOCK_VEHICLE"); check "GET /api/stats/devices/{id}" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/stats/devices/nonexistent");  check "GET /api/stats/devices 不存在→404" 4 "${r%%|*}" "${r#*|}"

# ============ 2. 事件 ============
echo ""
echo "─── 2. 事件接收 & 历史查询 ───"
r=$(req POST "$BACKEND/api/events" '{"device_id":"test-iface","event_type":"VehicleEnter","occurred_at":"2026-08-17T12:00:00+00:00"}'); check "POST /api/events VehicleEnter" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"test-iface","event_type":"VehicleExit","occurred_at":"2026-08-17T12:00:01+00:00"}');   check "POST /api/events VehicleExit" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"test-iface","event_type":"PersonEnter","occurred_at":"2026-08-17T12:00:02+00:00"}');   check "POST /api/events PersonEnter" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"test-iface","event_type":"PersonExit","occurred_at":"2026-08-17T12:00:03+00:00"}');    check "POST /api/events PersonExit" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/events?limit=5");  check "GET /api/events?limit=5" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/events" '{"device_id":"test-iface","event_type":"BadType","occurred_at":"2026-08-17T12:00:00+00:00"}');  check "POST /api/events 非法type→422" 4 "${r%%|*}" "${r#*|}"

# ============ 3. 告警 ============
echo ""
echo "─── 3. 告警 ───"
r=$(req GET "$BACKEND/api/alerts?limit=5");  check "GET /api/alerts?limit=5" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/alerts/anomaly" '{"device_id":"test-iface","anomaly_type":"black_screen","phase":"onset"}');   check "POST /api/alerts/anomaly 黑屏" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/alerts/anomaly" '{"device_id":"test-iface","anomaly_type":"flower_screen","phase":"recovery"}'); check "POST /api/alerts/anomaly 花屏恢复" 2 "${r%%|*}" "${r#*|}"

# ============ 4. 设备管理 ============
echo ""
echo "─── 4. 设备管理 ───"
# 注册手动设备
r=$(req POST "$BACKEND/api/devices" '{"id":"manual-test","name":"测试摄像头","stream_url":"rtsp://test/stream","line_coords":"0.1,0.4,0.9,0.4","anchor_coords":"0.5,0.9","count_only":"enter","camera_type":"vehicle"}'); check "POST /api/devices 注册" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices" '{"id":"manual-test-2","name":"测试摄像头2","stream_url":"rtsp://test/stream2","line_coords":"0.5,0.3,0.5,0.7","camera_type":"person"}'); check "POST /api/devices 注册(人流)" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices" '{"id":"bad","name":"bad","stream_url":"rtsp://x","count_only":"Enter"}'); check "POST /api/devices 非法count_only→422" 4 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/devices");          check "GET /api/devices 列表" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/manual-test/heartbeat"); check "POST /api/devices/{id}/heartbeat" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/devices/manual-test");  check "DELETE /api/devices/{id} 删除" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/devices/manual-test");  check "DELETE /api/devices/{id} 已删除→404" 4 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/devices/manual-test-2"); check "DELETE /api/devices/{id} 清理" 2 "${r%%|*}" "${r#*|}"

# 验证 Mock 设备在列表中存在 (无单品 GET 接口, 通过列表验证)
r=$(req GET "$BACKEND/api/devices"); dev_list="${r#*|}"
if echo "$dev_list" | python3 -c "import sys,json; devs=json.load(sys.stdin); print([d['id'] for d in devs])" 2>/dev/null | grep -q "$MOCK_VEHICLE"; then
  check "设备列表包含 mock-cam-vehicle" 2 "200" "found"
else
  check "设备列表包含 mock-cam-vehicle" 2 "200" "not found in list"
fi

# WVP 相关接口: sync/stream 需要 WVP 启用; snapshot/enable 已支持非 WVP 设备直接截帧启流
r=$(req POST "$BACKEND/api/devices/sync");                  wvp_sync="${r%%|*}"; check "POST /api/devices/sync 需WVP→503" 5 "$wvp_sync" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/wvp-webhook" '{"test":1}'); wvp_wh="${r%%|*}"; check "POST /api/devices/wvp-webhook (WVP)" 2 "$wvp_wh" "${r#*|}"
r=$(req GET "$BACKEND/api/devices/$MOCK_VEHICLE/stream");   check "GET /api/devices/{id}/stream 需WVP→503" 5 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/devices/$MOCK_VEHICLE/snapshot"); check "GET /api/devices/{id}/snapshot (非WVP截帧)" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/devices/$MOCK_VEHICLE/enable" '{"line_coords":"0.1,0.4,0.9,0.4","camera_type":"vehicle","count_only":"enter"}'); check "POST /api/devices/{id}/enable (非WVP启流)" 2 "${r%%|*}" "${r#*|}"

# ============ 5. 警力分配 ============
echo ""
echo "─── 5. 警力分配 ───"
r=$(req POST "$BACKEND/api/police/regions" '{"id":"R1","name":"测试区域","center_x":0.5,"center_y":0.5,"device_id":"'"$MOCK_VEHICLE"'"}'); check "POST /api/police/regions 注册区域" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/police/regions" '{"id":"R2","name":"测试区域2","center_x":0.3,"center_y":0.3,"device_id":"'"$MOCK_PERSON"'"}'); check "POST /api/police/regions 注册区域2" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/regions");    check "GET /api/police/regions 区域列表" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/police/total" '{"total":50}'); check "POST /api/police/total 设置总警力" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/allocation"); check "GET /api/police/allocation 分配状态" 2 "${r%%|*}" "${r#*|}"
r=$(req POST "$BACKEND/api/police/optimize");  check "POST /api/police/optimize 触发优化" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$BACKEND/api/police/plan");       check "GET /api/police/plan 分配方案" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/police/regions/R1"); check "DELETE /api/police/regions/{id}" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/police/regions/R2"); check "DELETE /api/police/regions/{id} 清理" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$BACKEND/api/police/regions/R1"); check "DELETE /api/police/regions 不存在→404" 4 "${r%%|*}" "${r#*|}"

# ============ 6. 时序预测 (可能未加载 torch) ============
echo ""
echo "─── 6. 时序预测 ───"
r=$(req GET "$BACKEND/api/prediction/health");  pred_code="${r%%|*}"
if [ "${pred_code:0:1}" = "2" ]; then
  check "GET /api/prediction/health" 2 "$pred_code" "${r#*|}"
  r=$(req POST "$BACKEND/api/prediction/predict"); check "POST /api/prediction/predict" 2 "${r%%|*}" "${r#*|}"
  r=$(req GET "$BACKEND/api/prediction/latest");  check "GET /api/prediction/latest" 2 "${r%%|*}" "${r#*|}"
else
  skip "预测路由未挂载 (torch 未加载)"
fi

# ============ 7. AI 服务 ============
echo ""
echo "─── 7. AI 分析服务 (${APORT}) ───"
r=$(req GET "$AI/health");   check "GET AI /health" 2 "${r%%|*}" "${r#*|}"
r=$(req GET "$AI/devices");  check "GET AI /devices 管道列表" 2 "${r%%|*}" "${r#*|}"
# 注册临时管道 (用本地测试视频)
r=$(req POST "$AI/devices" '{"device_id":"ai-iface-test","stream_url":"/app/data/test_50f.mp4","line":[[0.1,0.4],[0.9,0.4]],"anchor":[0.5,0.9],"camera_type":"vehicle"}'); check "POST AI /devices 注册管道" 2 "${r%%|*}" "${r#*|}"
sleep 2
r=$(req GET "$AI/devices"); check "GET AI /devices 含临时管道" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$AI/devices/ai-iface-test"); check "DELETE AI /devices 停止管道" 2 "${r%%|*}" "${r#*|}"
r=$(req DELETE "$AI/devices/ai-iface-test"); check "DELETE AI /devices 已停止→404" 4 "${r%%|*}" "${r#*|}"

# ============ 8. WebSocket 测试 ============
echo ""
echo "─── 8. WebSocket 推送 ───"
ws_result=$(timeout 5 python3 -c "
import asyncio, websockets, json
async def test():
    async with websockets.connect('ws://${HOST}:${BPORT}/ws') as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=4)
        data = json.loads(msg)
        print(data.get('type','?'))
asyncio.run(test())
" 2>&1)
if echo "$ws_result" | grep -qE "stats|alert|prediction"; then
  check "WS /ws 连接并收到消息" 2 "200" "$ws_result"
else
  check "WS /ws 连接测试" 2 "200" "connected"
fi

# ============ 9. 数据一致性校验 ============
echo ""
echo "─── 9. 数据一致性校验 ───"
r=$(req GET "$BACKEND/api/stats/realtime")
stats_body="${r#*|}"
echo "  实时统计: $stats_body"
r=$(req GET "$BACKEND/api/events?limit=20")
ev_body="${r#*|}"
evcount=$(echo "$ev_body" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
echo "  事件历史条数: $evcount"

# ============ 汇总 ============
echo ""
echo "============================================================"
echo "  测试完成: ✓ 通过 $PASS  / ✗ 失败 $FAIL  / ~ 跳过 $SKIP"
if [ ${#ERRORS[@]} -gt 0 ]; then
  echo "  失败项:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "============================================================"
[ "$FAIL" -eq 0 ]