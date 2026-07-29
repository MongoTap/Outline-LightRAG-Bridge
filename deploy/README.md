# OutlineRAGBridge 离线部署包

## 目录结构

```
deploy/
├── deploy.sh              # 一键部署脚本（在目标服务器上运行）
├── start.sh               # 启动脚本（自动适配各种 Python 环境）
├── stop.sh                # 停止脚本
├── download_python.sh     # Python 3.12 便携版下载工具（在能联网的机器上运行）
├── bridge.py              # 桥接服务主程序
├── requirements.txt       # Python 依赖清单
├── .env                   # 配置文件（部署前需修改）
├── README.md              # 本说明文件
├── packages/              # 预下载的 Python 依赖包（25 个 .whl 文件）
│   ├── fastapi-*.whl
│   ├── uvicorn-*.whl
│   ├── httpx-*.whl
│   └── ...
└── python/                # [可选] 存放 Python 3.12 便携版（见下文）
    └── cpython-3.12.13+20260610-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
```

## 前置条件

### 方案 A：目标服务器已安装 Python（推荐）

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
```

### 方案 B：目标服务器完全离线无 Python

用 `download_python.sh` 在有网络的机器上下载 **Python 3.12 便携版**（32MB），
然后拷贝到部署包的 `python/` 目录中，部署脚本会自动解压使用：

```bash
# 第 1 步：在能联网的机器上运行
chmod +x download_python.sh
./download_python.sh /path/to/deploy/python/

# 第 2 步：把整个 deploy 目录传到目标服务器
scp -r /path/to/deploy user@target-server:/tmp/

# 第 3 步：在目标服务器上执行部署
ssh user@target-server
cd /tmp/deploy
sudo ./deploy.sh
```

**便携版 Python**（[python-build-standalone](https://github.com/astral-sh/python-build-standalone)）特点：
- 预编译的二进制文件，无需编译
- 可在任意 Linux x86_64 上运行，不依赖系统 Python
- 自带 pip，包含 ssl、zlib 等常用模块
- 约 32MB（压缩后）

## 快速部署

```bash
# 1. 修改配置文件
vi .env
#   必须改：OUTLINE_WEBHOOK_SECRET = 随机密钥
#   按需改：LIGHTRAG_API_URL

# 2. 一键部署
sudo ./deploy.sh

# 3. 启动服务
./start.sh -d

# 4. 验证
curl http://localhost:9641/health
```

## 管理命令

```bash
# 启动（后台模式）
./start.sh -d

# 启动（后台并跟踪日志）
./start.sh -df

# 启动（前台调试模式，Ctrl+C 停止）
./start.sh

# 停止
./stop.sh

# 强制停止
./stop.sh -f

# 停止并清理日志
./stop.sh -c

# 查看日志
tail -f logs/bridge_current.log
```

## 部署脚本工作流程

`deploy.sh` 自动处理以下逻辑：

```
                    ┌─────────────────────────────┐
                    │   检查系统 Python 版本       │
                    │   python3.12 / python3.11    │
                    │   python3.10 / python3       │
                    └──────────┬──────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
    Python 3.10+         版本过低              无 Python
    直接使用              ┌────┴────┐           ┌────┴────┐
          │              继续执行   报错退出    检查 python/ 目录
          │                                    ┌────┴────┐
          │                              有便携版包    无便携版包
          │                              解压使用     提示运行
          │                                            download_
          │                                            python.sh
          ▼
    ┌─────┴──────┐
    │ 创建 venv   │ ← 如果有 venv 模块则创建虚拟环境
    │ 或直接使用  │ ← 便携版 Python 可能无 venv，直接使用
    │ 安装依赖    │
    │ 复制文件    │
    │ 注册服务    │
    └─────────────┘
```

## 自定义安装

```bash
# 安装到自定义目录
sudo ./deploy.sh /data/outline-bridge

# 指定运行用户
sudo DEPLOY_USER=myuser ./deploy.sh
```

## 配置 Outline Webhook

在 Outline 管理后台（Settings → Integrations → Webhooks）添加：
- **URL**: `http://<服务器IP>:9641/webhook`
- **Secret**: 与 `.env` 中 `OUTLINE_WEBHOOK_SECRET` 保持一致

## 防火墙

```bash
# Ubuntu (ufw)
sudo ufw allow 9641/tcp

# CentOS/RHEL (firewalld)
sudo firewall-cmd --add-port=9641/tcp --permanent
sudo firewall-cmd --reload
```
