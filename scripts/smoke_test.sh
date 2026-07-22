#!/bin/bash
# 冒烟测试: 用 Redis 启动后端, 验证 /health 与 /api/stats/realtime (无需 MySQL)
set -u
echo "=== 清理旧测试容器 ==="
docker rm -f sctest-redis sctest-backend 2>/dev/null || true
docker network rm sctest-net 2>/dev/null || true

echo "=== 创建网络 + 启动 Redis ==="
docker network create sctest-net
docker run -d --name sctest-redis --network sctest-net redis:7-alpine

echo "=== 启动后端 (仅 Redis, 无 MySQL) ==="
docker run -d --name sctest-backend --network sctest-net -p 18000:8000 \
  -e REDIS_URL=redis://sctest-redis:6379/0 \
  -e PYTHONUNBUFFERED=1 \
  smart-city-platform:latest \
  uvicorn app.backend.main:app --host 0.0.0.0 --port 8000

echo "=== 等待后端启动 (12s) ==="
sleep 12

echo "=== GET /health ==="
curl -s -w " [HTTP %{http_code}]\n" http://localhost:18000/health

echo "=== GET /api/stats/realtime (Redis 实时统计) ==="
curl -s -w " [HTTP %{http_code}]\n" http://localhost:18000/api/stats/realtime

echo "=== GET /api/alerts (需 MySQL, 预期 500/报错) ==="
curl -s -w " [HTTP %{http_code}]\n" http://localhost:18000/api/alerts

echo "=== 后端日志 (最后 15 行) ==="
docker logs --tail 15 sctest-backend 2>&1

echo "=== 清理 ==="
docker rm -f sctest-redis sctest-backend 2>/dev/null || true
docker network rm sctest-net 2>/dev/null || true
echo "=== SMOKE_TEST_DONE ==="
