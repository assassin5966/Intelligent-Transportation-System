# ============================================================
# 智慧交管大屏前端 - 多阶段构建
#   阶段1: node 构建 Vite 产物 dist/
#   阶段2: nginx 托管 dist + 反向代理后端 API/WS
# 构建: docker compose -f docker-compose.yml up -d --build frontend
# ============================================================

FROM node:20-alpine AS build
WORKDIR /app
# 项目无 package-lock.json, 用 npm install 生成 (首次构建网络安装依赖)
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
