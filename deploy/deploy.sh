#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  OutlineRAGBridge 一键部署脚本
#  适用于离线 Linux 环境（无互联网访问）
#
#  使用方法:
#    chmod +x deploy.sh
#    sudo ./deploy.sh              # 部署到默认目录 /opt/outline-rag-bridge
#    sudo ./deploy.sh /path/to     # 部署到指定目录
#
#  Python 支持三种模式（自动选择）:
#    1. 若系统中已安装 Python 3.12+ → 直接使用系统 Python
#    2. 若 deploy/python/ 目录下有便携版 Python → 解压使用
#    3. 以上都不满足 → 提示运行 download_python.sh 下载
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── 颜色输出 ─────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 配置 ─────────────────────────────────────────────────────────────

# 目标安装目录（默认为 /opt/outline-rag-bridge）
INSTALL_DIR="${1:-/opt/outline-rag-bridge}"

# 当前脚本所在目录（deploy 包的位置）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 服务运行的用户（默认使用当前非 root 用户，否则用 nobody）
if [ "$(id -u)" -eq 0 ]; then
    SUGGESTED_USER=$(logname 2>/dev/null || echo "nobody")
else
    SUGGESTED_USER=$(whoami)
fi
RUN_USER="${DEPLOY_USER:-$SUGGESTED_USER}"
RUN_GROUP=$(id -gn "$RUN_USER" 2>/dev/null || echo "nogroup")

# ── 前置检查 ─────────────────────────────────────────────────────────

echo ""
log_info "OutlineRAGBridge 部署脚本"
log_info "========================="
echo ""

# 检查是否为 root（建议但非必须）
if [ "$(id -u)" -ne 0 ]; then
    log_warn "建议以 root 用户运行此脚本（sudo ./deploy.sh）"
    log_warn "继续以非 root 用户运行..."
    echo ""
fi

# ═══════════════════════════════════════════════════════════════════════
#  Python 探测与安装
#  优先级: 系统 Python 3.12+ > 便携版 Python 3.12 > 提示下载
# ═══════════════════════════════════════════════════════════════════════

PYTHON_CMD=""
PYTHON_SOURCE=""

# ── 第 1 步：检查系统是否已安装 Python 3.12+ ─────────────────────
log_info "检查系统 Python..."
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v $cmd &>/dev/null; then
        PY_VERSION=$($cmd --version 2>&1 | grep -oP '\d+\.\d+')
        MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        # 需要 Python 3.10+
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_CMD=$(command -v $cmd)
            PYTHON_SOURCE="system"
            log_ok "发现系统 Python: $($PYTHON_CMD --version)"
            break
        fi
    fi
done

# ── 第 2 步：如果系统没有，检查便携版 Python 3.12 ────────────────
if [ -z "$PYTHON_CMD" ]; then
    log_warn "系统中未找到 Python 3.10+，检查便携版 Python..."

    # 查找 deploy/python/ 目录下的 cpython 压缩包
    PORTABLE_TGZ=""
    for f in "${SCRIPT_DIR}/python"/cpython-3.12*.tar.gz; do
        if [ -f "$f" ]; then
            PORTABLE_TGZ="$f"
            break
        fi
    done

    # 也检查 packages/ 目录（兼容旧结构）
    if [ -z "$PORTABLE_TGZ" ]; then
        for f in "${SCRIPT_DIR}"/cpython-3.12*.tar.gz; do
            if [ -f "$f" ]; then
                PORTABLE_TGZ="$f"
                break
            fi
        done
    fi

    if [ -n "$PORTABLE_TGZ" ]; then
        log_info "发现便携版 Python 压缩包: $(basename "${PORTABLE_TGZ}")"
        PORTABLE_DIR="${INSTALL_DIR}/portable-python"
        mkdir -p "${PORTABLE_DIR}"

        log_info "正在解压便携版 Python..."
        tar xzf "${PORTABLE_TGZ}" -C "${PORTABLE_DIR}" --strip-components=1
        log_ok "便携版 Python 解压完成"

        PYTHON_CMD="${PORTABLE_DIR}/bin/python3"
        if [ ! -f "$PYTHON_CMD" ]; then
            log_error "便携版 Python 解压后未找到 python3 可执行文件！"
            log_info "解压目录内容:"
            ls -la "${PORTABLE_DIR}/bin/" 2>/dev/null | head -20
            exit 1
        fi

        PYTHON_SOURCE="portable"
        log_ok "便携版 Python: $($PYTHON_CMD --version)"

        # 便携版 Python 自带 pip，无需额外安装 venv
        # 但它可能没有 venv 模块，需要特殊处理虚拟环境创建
    fi
fi

# ── 第 3 步：Python 不可用，给出提示 ──────────────────────────────
if [ -z "$PYTHON_CMD" ]; then
    echo ""
    log_error "╔══════════════════════════════════════════════════════════════╗"
    log_error "║  未找到 Python 3.10+！                                    ║"
    log_error "╠══════════════════════════════════════════════════════════════╣"
    log_error "║  方案一：在有网络的机器上运行 download_python.sh          ║"
    log_error "║          下载 Python 3.12 便携版，放到 python/ 目录       ║"
    log_error "║          然后重新运行此部署脚本。                          ║"
    log_error "║                                                          ║"
    log_error "║  方案二：在目标服务器上直接安装 Python                ║"
    log_error "║          Ubuntu/Debian:                                    ║"
    log_error "║            sudo apt-get install -y python3 python3-venv    ║"
    log_error "║                                                          ║"
    log_error "║  下载脚本: ${SCRIPT_DIR}/download_python.sh     ║"
    log_error "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    exit 1
fi

# ── 检查 venv/虚拟环境创建能力 ────────────────────────────────────
CAN_CREATE_VENV=true
if ! $PYTHON_CMD -c "import venv" &>/dev/null; then
    if [ "$PYTHON_SOURCE" = "portable" ]; then
        # 便携版 Python 可能没有 venv，我们直接使用它本身作为基础 Python
        log_warn "便携版 Python 未包含 venv 模块，将直接使用 portable Python 作为基础环境。"
        CAN_CREATE_VENV=false
    else
        log_warn "Python venv 模块不可用，尝试安装..."
        if command -v apt-get &>/dev/null && [ "$(id -u)" -eq 0 ]; then
            apt-get install -y python3-venv 2>/dev/null || true
            if $PYTHON_CMD -c "import venv" &>/dev/null; then
                log_ok "venv 模块安装成功"
            else
                log_warn "venv 模块安装失败，将直接使用系统 Python 作为基础环境。"
                CAN_CREATE_VENV=false
            fi
        else
            log_warn "venv 模块不可用，将直接使用系统 Python 作为基础环境。"
            CAN_CREATE_VENV=false
        fi
    fi
fi

# ── 检查 pip ────────────────────────────────────────────────────────
if ! $PYTHON_CMD -m pip --version &>/dev/null; then
    if [ "$PYTHON_SOURCE" = "portable" ]; then
        log_info "便携版 Python 自带 pip，配置中..."
        $PYTHON_CMD -m ensurepip --upgrade 2>/dev/null || true
    else
        log_warn "pip 不可用，尝试安装..."
        if command -v apt-get &>/dev/null && [ "$(id -u)" -eq 0 ]; then
            apt-get install -y python3-pip 2>/dev/null || true
        fi
    fi
fi

log_ok "Python: $($PYTHON_CMD --version)（来源: ${PYTHON_SOURCE}）"

# ── 安装过程 ─────────────────────────────────────────────────────────

echo ""
log_info "安装目标目录: ${INSTALL_DIR}"
log_info "运行用户: ${RUN_USER}"
echo ""

# 创建目标目录
mkdir -p "${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}/packages"

# 复制文件
log_info "复制文件到 ${INSTALL_DIR}..."

cp -r "${SCRIPT_DIR}/bridge"                "${INSTALL_DIR}/bridge"
cp "${SCRIPT_DIR}/requirements.txt"     "${INSTALL_DIR}/requirements.txt"
cp "${SCRIPT_DIR}/.env"                 "${INSTALL_DIR}/.env"
cp -r "${SCRIPT_DIR}/packages/"*        "${INSTALL_DIR}/packages/"

# 复制启动/停止脚本（方便后续使用）
cp "${SCRIPT_DIR}/start.sh"             "${INSTALL_DIR}/start.sh"
cp "${SCRIPT_DIR}/stop.sh"              "${INSTALL_DIR}/stop.sh"
chmod +x "${INSTALL_DIR}/start.sh"
chmod +x "${INSTALL_DIR}/stop.sh"

# 如果使用便携版 Python，也把它复制到安装目录
if [ "$PYTHON_SOURCE" = "portable" ] && [ -d "${PORTABLE_DIR:-}" ]; then
    # portable Python 已经解压到了 INSTALL_DIR/portable-python/，确保它存在
    if [ ! -d "${INSTALL_DIR}/portable-python" ]; then
        cp -r "${PORTABLE_DIR}" "${INSTALL_DIR}/portable-python"
    fi
    log_ok "便携版 Python 已复制到 ${INSTALL_DIR}/portable-python/"
fi

log_ok "文件复制完成"

# ── 创建 Python 虚拟环境并安装依赖 ────────────────────────────────

if [ "$CAN_CREATE_VENV" = true ]; then
    log_info "创建 Python 虚拟环境..."
    $PYTHON_CMD -m venv "${INSTALL_DIR}/venv"
    log_ok "虚拟环境创建完成"
    PIP_CMD="${INSTALL_DIR}/venv/bin/pip"
else
    log_info "跳过虚拟环境，直接使用基础 Python..."
    mkdir -p "${INSTALL_DIR}/venv"
    # 创建一个包装脚本指向基础 Python，使 start.sh 可以统一使用 venv/bin/python
    ln -sf "$PYTHON_CMD" "${INSTALL_DIR}/venv/bin/python" 2>/dev/null || true
    PIP_CMD="$PYTHON_CMD -m pip"
fi

# 离线安装依赖
log_info "从本地 packages 目录安装 Python 依赖..."
$PIP_CMD install \
    --no-index \
    --find-links "${INSTALL_DIR}/packages" \
    -r "${INSTALL_DIR}/requirements.txt" \
    -q 2>&1 | tail -3
log_ok "Python 依赖安装完成"

# 创建日志目录
mkdir -p "${INSTALL_DIR}/logs"

# 设置目录权限
chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}" 2>/dev/null || true

# ── 更新 start.sh 中的 Python 路径（便携版 Python 的情况） ──────
if [ "$PYTHON_SOURCE" = "portable" ]; then
    # 确保 start.sh 使用 portable Python
    log_info "配置 start.sh 使用便携版 Python..."
fi

# ── 注册 systemd 服务（可选） ──────────────────────────────────────

if command -v systemctl &>/dev/null; then
    echo ""
    log_info "检测到 systemd，是否注册为系统服务？[Y/n]"
    read -r ENABLE_SERVICE
    if [[ ! "$ENABLE_SERVICE" =~ ^[Nn] ]]; then
        SERVICE_FILE="/etc/systemd/system/outline-rag-bridge.service"
        cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=OutlineRAGBridge - Outline to LightRAG Document Sync Service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/start.sh
ExecStop=${INSTALL_DIR}/stop.sh
Restart=on-failure
RestartSec=10
StandardOutput=append:${INSTALL_DIR}/logs/bridge.log
StandardError=append:${INSTALL_DIR}/logs/bridge.log

[Install]
WantedBy=multi-user.target
EOF
        log_ok "systemd 服务文件已创建: ${SERVICE_FILE}"

        systemctl daemon-reload
        log_info "执行以下命令启动服务："
        log_info "  sudo systemctl enable outline-rag-bridge"
        log_info "  sudo systemctl start outline-rag-bridge"
        log_info "  sudo systemctl status outline-rag-bridge"
    fi
else
    echo ""
    log_warn "未检测到 systemd，跳过系统服务注册。"
    log_info "使用以下命令管理服务："
    log_info "  启动: ${INSTALL_DIR}/start.sh -d"
    log_info "  停止: ${INSTALL_DIR}/stop.sh"
fi

# ── 完成 ─────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo -e "  ${GREEN}OutlineRAGBridge 部署完成！${NC}"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  安装目录: ${INSTALL_DIR}"
echo "  Python来源: ${PYTHON_SOURCE}"
echo "  Python路径: ${PYTHON_CMD}"
echo "  启动脚本: ${INSTALL_DIR}/start.sh"
echo "  停止脚本: ${INSTALL_DIR}/stop.sh"
echo "  日志目录: ${INSTALL_DIR}/logs/"
echo ""
echo "  启动前请先编辑配置文件:"
echo "    vi ${INSTALL_DIR}/.env"
echo ""
echo "  确保配置 LightRAG API 地址和 Outline Webhook Secret:"
echo "    LIGHTRAG_API_URL=http://<lightrag-host>:9621"
echo "    OUTLINE_WEBHOOK_SECRET=<your-secret>"
echo ""
echo "  然后执行:"
echo "    ${INSTALL_DIR}/start.sh -d"
echo ""
echo "  在 Outline 后台配置 Webhook:"
echo "    URL: http://<bridge-host>:9641/webhook"
echo "    Secret: <your-secret>"
echo ""
