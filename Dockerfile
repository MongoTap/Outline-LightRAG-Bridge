# =============================================================================
#  OutlineRAGBridge — Docker 镜像
#  将 Outline Webhook 同步到 LightRAG 的桥接服务
#
#  构建：docker build -t outline-rag-bridge .
#  运行：docker run -d -p 9641:9641 \
#             -e LIGHTRAG_API_URL=http://192.168.1.100:9621 \
#             -e OUTLINE_WEBHOOK_SECRET=your-secret \
#             -v bridge-data:/app/data \
#             outline-rag-bridge
# =============================================================================
FROM python:3.12-slim

# Python 容器最佳实践
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# pip 镜像源（构建时可覆盖，例如 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 创建数据目录和普通用户（安全原则：不以 root 运行）
RUN mkdir -p /app/data && \
    addgroup --system --gid 1001 appuser && \
    adduser --system --uid 1001 --gid 1001 --no-create-home appuser && \
    chown appuser:appuser /app/data

WORKDIR /app

# apt 镜像源（构建时可覆盖，例如 --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn）
ARG APT_MIRROR=mirrors.aliyun.com

# 安装 curl（用于 HEALTHCHECK）
RUN sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（PIP_INDEX_URL 默认为阿里云镜像，构建时可覆盖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --index-url $PIP_INDEX_URL

# 复制应用（bridge/ Python 包）
COPY bridge/ ./bridge/

# ── 环境变量默认值（全部可通过 -e 覆盖） ──────────────────────────────────
ENV LIGHTRAG_API_URL=http://lightrag:9621
ENV OUTLINE_WEBHOOK_SECRET=""
ENV DB_PATH=/app/data/bridge.db
ENV BRIDGE_HOST=0.0.0.0
ENV BRIDGE_PORT=9641
ENV POLL_INTERVAL=2
ENV POLL_MAX_ATTEMPTS=60
ENV DELETE_RETRY_ATTEMPTS=2
ENV DELETE_RETRY_DELAY=3
ENV TASK_SCHEDULE_ENABLED=false
ENV TASK_SCHEDULE_START=00:00
ENV TASK_SCHEDULE_DURATION_MINUTES=480
ENV LOG_LEVEL=INFO

EXPOSE 9641

# 健康检查（每 30s 探测 /health 端点）
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl --fail http://localhost:${BRIDGE_PORT:-9641}/health || exit 1

# 以非 root 用户运行
USER appuser

CMD ["python", "-m", "bridge"]
