# ============================================================
# WVP-GB28181-pro 2.7.4 标准化 Dockerfile (arm64 兼容)
# 多阶段构建: maven 编译 -> JRE 运行
# build: docker build -t wvp:2.7.4 -f deploy/wvp/wvp/Dockerfile /tmp/wvp-src
# ============================================================

# ---- 阶段1: maven 编译源码 ----
FROM maven:3.9-eclipse-temurin-21 AS builder
WORKDIR /build

# 先复制 pom.xml 单独下载依赖 (利用 docker 层缓存, 加速重复 build)
COPY pom.xml .
RUN mvn dependency:go-offline -Dmaven.test.skip=true -Dgit.skip=true || true

# 复制全部源码 (含 .git, git-commit-id-maven-plugin 需要读取 git 信息)
COPY . .
RUN mvn clean package -Dmaven.test.skip=true

# ---- 阶段2: 运行环境 ----
FROM eclipse-temurin:21-jre
WORKDIR /app
RUN mkdir -p /app/config /app/logs

# 复制编译好的 jar (finalName 含时间戳, 用通配符匹配)
COPY --from=builder /build/target/wvp-pro-*.jar /app/wvp.jar

# WVP 管理端口 + SIP 信令端口
EXPOSE 18080 5060

# 启动: 用 docker profile (application-docker.yml, 环境变量驱动配置)
# 参数通过 docker-compose environment 配置 (WVP_HOST/ZLM_HOST/REDIS_HOST/数据库等)
ENTRYPOINT ["java", "-Xms512m", "-Xmx1024m", \
            "-XX:+HeapDumpOnOutOfMemoryError", \
            "-jar", "/app/wvp.jar", \
            "--spring.profiles.active=docker"]
