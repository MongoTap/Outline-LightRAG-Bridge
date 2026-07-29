# OutlineRAGBridge — 离线 Docker 部署教程

将 Outline 文档变更通过 Webhook 实时同步到 LightRAG 的桥接服务。
本教程适用于**完全离线的局域网环境**。

---

## 部署架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        局域网（离线）                              │
│                                                                  │
│   ┌──────────────┐     POST /webhook      ┌──────────────────┐   │
│   │   Outline     │ ─────────────────────→ │  outline-rag-    │   │
│   │  (已有服务)    │    http://bridge-ip    │  bridge 容器      │   │
│   │   IP: a.b.c.d │    :9641/webhook       │                   │   │
│   └──────────────┘                         │  POST /documents/ │   │
│                                            │  text             │   │
│   ┌──────────────┐                         │  GET /track_status│   │
│   │   LightRAG    │ ←───────────────────── │  DELETE /documents│   │
│   │  (已有服务)    │    http://lightrag-ip  └────────┬──────────┘   │
│   │   IP: x.y.z.w │    :9621                       │              │
│   └──────────────┘                         ┌────────┴──────────┐   │
│                                            │   bridge.db        │   │
│                                            │  (Docker volume)   │   │
│                                            └───────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**核心接口：**
- **入站**（供 Outline 发送 Webhook）：容器 `9641` 端口，HTTP `POST /webhook`
- **出站**（连接 LightRAG）：通过环境变量 `LIGHTRAG_API_URL` 配置
- **健康检查**：`GET /health`
- **映射查询**：`GET /mappings`

---

## 部署前置条件

| 条件 | 说明 |
|---|---|
| **Linux 服务器** | x86_64 架构，已安装 Docker |
| **Docker 版本** | ≥ 20.10 |
| **已有服务** | 局域网中已有可用的 Outline 和 LightRAG 服务 |
| **离线镜像包** | `outline-rag-bridge-image.tar.gz`（已提前构建好，约 58MB） |

---

## 步骤一：导入镜像

```bash
# 在 tar.gz 文件所在目录执行
docker load < outline-rag-bridge-image.tar.gz

# 验证镜像已导入
docker images outline-rag-bridge
# 输出示例：
# REPOSITORY               TAG       IMAGE ID       SIZE
# outline-rag-bridge       latest    246e7e4f0301   253MB
```

---

## 步骤二：配置环境变量

启动容器前，需要知道两个信息：

1. **LightRAG 服务器的 IP 地址和端口**（例如 `192.168.1.10:9621`）
2. **一个 Webhook 密钥**（自行生成一个随机字符串，例如 `my-strong-secret-2024`）

| 配置项 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `LIGHTRAG_API_URL` | **必填** | LightRAG 服务的 IP:端口 | `http://192.168.1.10:9621` |
| `OUTLINE_WEBHOOK_SECRET` | **必填** | Webhook HMAC 密钥，须与 Outline 后台一致 | `my-strong-secret-2024` |

> **提示**：
> - LightRAG 和 bridge 在同一台机器时，Linux 下用 `http://host.docker.internal:9621` 不保证可用，建议直接用宿主机内网 IP
> - LightRAG 在另一台服务器时，确保 bridge 服务器的 9641 端口在局域网内可达（防火墙放行）

---

## 步骤三：启动容器

### 方式 A：直接 docker run（推荐）

```bash
# 请将以下值替换为你的实际配置
docker run -d \
  --name outline-rag-bridge \
  --restart unless-stopped \
  -p 9641:9641 \
  -e LIGHTRAG_API_URL="http://192.168.1.10:9621" \
  -e OUTLINE_WEBHOOK_SECRET="my-strong-secret-2024" \
  -v bridge-data:/app/data \
  outline-rag-bridge:latest
```

参数说明：
- `-d`：后台运行
- `--restart unless-stopped`：容器崩溃或服务器重启时自动拉起
- `-p 9641:9641`：将宿主机 9641 端口映射到容器 9641 端口
- `-e`：配置环境变量（见上表）
- `-v bridge-data:/app/data`：持久化 SQLite 数据库文件（映射记录）

### 方式 B：docker-compose

创建 `docker-compose.yml`：
```yaml
services:
  outline-rag-bridge:
    image: outline-rag-bridge:latest
    container_name: outline-rag-bridge
    restart: unless-stopped
    ports:
      - "9641:9641"
    environment:
      - LIGHTRAG_API_URL=http://192.168.1.10:9621
      - OUTLINE_WEBHOOK_SECRET=my-strong-secret-2024
      - DB_PATH=/app/data/bridge.db
    volumes:
      - bridge-data:/app/data

volumes:
  bridge-data:
```

启动：
```bash
docker compose up -d
```

---

## 步骤四：配置 Outline Webhook

1. 登录 Outline 管理后台 → **设置** → **集成** → **Webhook**
2. 点击 **创建 Webhook**，填写：
   - **名称**：`bridge-sync`（自定义）
   - **URL**：`http://<bridge-所在服务器IP>:9641/webhook`
     - bridge 和 Outline 在同一台机器：`http://localhost:9641/webhook`
     - bridge 和 Outline 在不同机器：`http://<bridge服务器的局域网IP>:9641/webhook`
   - **密钥**：填入 `OUTLINE_WEBHOOK_SECRET` 中设置的值（本例：`my-strong-secret-2024`）
   - **事件**：勾选 `documents.create`、`documents.update`、`documents.delete`
3. 点击 **保存**

> **注意**：如果 Outline 在 Docker 中运行（常见），它默认禁止向私有 IP 发送 Webhook。需要在 Outline 的 `docker-compose.yml` 中添加环境变量后重启：
> ```yaml
> environment:
>   - ALLOWED_PRIVATE_IP_ADDRESSES=172.22.0.1,172.17.0.1,192.168.0.0/16
> ```

---

## 步骤五：验证部署

```bash
# 1. 检查容器状态和健康检查
docker ps --filter name=outline-rag-bridge
docker inspect --format='{{json .State.Health.Status}}' outline-rag-bridge
# 预期输出："healthy"

# 2. 测试健康端点
curl http://localhost:9641/health
# 预期输出：
# {"status":"ok","lightrag_api":"http://192.168.1.10:9621","timestamp":"..."}

# 3. 查看初始映射表（应为空）
curl http://localhost:9641/mappings
# 预期输出：{"mappings":[]}

# 4. 确认运行用户为非 root
docker exec outline-rag-bridge whoami
# 预期输出：appuser

# 5. 测试 LightRAG 连通性
docker exec outline-rag-bridge curl -sf http://192.168.1.10:9621/health
# 如果返回正常，说明 LightRAG 连接成功

# 6. 查看容器日志
docker logs outline-rag-bridge
```

---

## 全流程测试

在 Outline 中创建一个文档，验证 Webhook → Bridge → LightRAG 链路：

```bash
# 1. 观察桥接器日志
docker logs -f outline-rag-bridge &

# 2. 通过 Outline API 创建一个文档（将 TOKEN 替换为你的 Outline API Key）
curl -s -X POST http://localhost:8080/api/documents.create \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ol_api_你的API密钥" \
  -d '{
    "title": "测试文档",
    "text": "# 测试文档\n\n这是一个测试，验证文档能同步到 LightRAG。",
    "collectionId": "你的集合ID",
    "publish": true
  }'

# 3. 预期日志输出：
#    收到 Webhook: event=documents.create doc_id=xxx title=测试文档
#    处理文档创建: outline_doc_id=xxx title=测试文档
#    文本已插入 LightRAG，track_id=insert_xxx

# 4. 验证 LightRAG 中已有文档
curl http://localhost:9621/documents | python3 -m json.tool
```

---

## 所有配置项参考

| 变量 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `LIGHTRAG_API_URL` | `http://lightrag:9621` | **是** | LightRAG API 基础地址 |
| `OUTLINE_WEBHOOK_SECRET` | `""` | **是** | Webhook HMAC-SHA256 密钥，须与 Outline 后台一致 |
| `DB_PATH` | `/app/data/bridge.db` | 否 | SQLite 数据库路径，应在 volume 挂载目录内 |
| `BRIDGE_PORT` | `9641` | 否 | 容器内部监听端口 |
| `POLL_INTERVAL` | `2` | 否 | LightRAG 异步处理轮询间隔（秒） |
| `POLL_MAX_ATTEMPTS` | `60` | 否 | 最大轮询次数（默认最长等 120 秒） |
| `DELETE_RETRY_ATTEMPTS` | `3` | 否 | 删除文档时最大重试次数 |
| `DELETE_RETRY_DELAY` | `5` | 否 | 删除重试间隔（秒） |
| `LOG_LEVEL` | `INFO` | 否 | 日志级别：`DEBUG`/`INFO`/`WARNING`/`ERROR` |

---

## 常用操作

### 停止容器
```bash
docker stop outline-rag-bridge
```

### 启动已停止的容器
```bash
docker start outline-rag-bridge
```

### 查看实时日志
```bash
docker logs -f outline-rag-bridge
```

### 更新容器（保留数据）
```bash
docker stop outline-rag-bridge
docker rm outline-rag-bridge
# 加载新版本镜像...
docker load < new-version-image.tar.gz
# 使用同样的 -v bridge-data:/app/data 启动，数据自动保留
docker run -d --name outline-rag-bridge ... -v bridge-data:/app/data ... outline-rag-bridge:latest
```

### 备份数据
```bash
# bridge.db 存储在 Docker volume 中
# 查看 volume 位置
docker volume inspect bridge-data
# 备份
tar czf bridge-backup-$(date +%Y%m%d).tar.gz /var/lib/docker/volumes/bridge-data/_data/
```

---

## 故障排查

| 问题 | 原因 | 解决办法 |
|---|---|---|
| 容器状态 `unhealthy` | LightRAG 不可达或启动中 | 检查 `LIGHTRAG_API_URL` 是否正确 |
| Outline 未发送 Webhook | Outline 容器禁止访问私有 IP | 在 Outline 环境变量中添加 `ALLOWED_PRIVATE_IP_ADDRESSES` |
| Webhook 返回 401 | 密钥不匹配 | 检查 `OUTLINE_WEBHOOK_SECRET` 与 Outline 后台是否一致 |
| 容器启动后立即退出 | 端口被占用或配置错误 | `docker logs outline-rag-bridge` 查看错误信息 |
| 文档创建后日志无反应 | Webhook URL 配置错误 | 检查 Outline Webhook URL 中的 IP 和端口 |
