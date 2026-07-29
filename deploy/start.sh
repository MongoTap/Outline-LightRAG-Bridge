#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  OutlineRAGBridge 启动脚本
#
#  用法:
#    ./start.sh              # 前台启动（调试用，Ctrl+C 停止）
#    ./start.sh -d           # 后台启动（守护进程模式）
#    ./start.sh -d -f        # 后台启动并跟踪日志
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── 颜色 ─────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 配置 ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="${SCRIPT_DIR}/bridge"
ENV_FILE="${SCRIPT_DIR}/.env"
LOG_DIR="${SCRIPT_DIR}/logs"
PID_FILE="${SCRIPT_DIR}/bridge.pid"

# 查找可用的 Python 可执行文件
# 优先级: venv/bin/python > portable-python/bin/python3 > 系统 python3
PYTHON_CMD=""
if [ -f "${SCRIPT_DIR}/venv/bin/python" ]; then
    PYTHON_CMD="${SCRIPT_DIR}/venv/bin/python"
elif [ -f "${SCRIPT_DIR}/portable-python/bin/python3" ]; then
    PYTHON_CMD="${SCRIPT_DIR}/portable-python/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD=$(command -v python3)
else
    log_error "未找到 Python 可执行文件！请先运行 deploy.sh 完成部署。"
    exit 1
fi

# 检查 bridge 包
if [ ! -d "${BRIDGE_DIR}" ]; then
    log_error "bridge 包未找到: ${BRIDGE_DIR}"
    exit 1
fi

# 检查 .env 文件
if [ ! -f "${ENV_FILE}" ]; then
    log_warn ".env 配置文件不存在，使用默认配置..."
    log_warn "建议创建 ${ENV_FILE} 并配置必要的环境变量。"
fi

# 检查是否已在运行
if [ -f "${PID_FILE}" ]; then
    OLD_PID=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
        log_error "服务已在运行（PID: ${OLD_PID}）"
        log_info "如需重启，请先执行: ${SCRIPT_DIR}/stop.sh"
        exit 1
    fi
    rm -f "${PID_FILE}"
fi

# ── 启动 ─────────────────────────────────────────────────────────────

# 创建日志目录
mkdir -p "${LOG_DIR}"

# 生成日志文件名（按日期）
LOG_FILE="${LOG_DIR}/bridge_$(date +%Y%m%d_%H%M%S).log"
# 保留最近的符号链接
ln -sf "${LOG_FILE}" "${LOG_DIR}/bridge_current.log"

# 解析参数
DAEMON=false
TAIL=false
while getopts "df" opt; do
    case $opt in
        d) DAEMON=true ;;
        f) TAIL=true ;;
        *) ;;
    esac
done

echo ""
log_info "OutlineRAGBridge 启动中..."
echo ""

if [ "${DAEMON}" = true ]; then
    # ── 后台守护进程模式 ────────────────────────────────────────────
    # 使用 nohup 确保进程在终端关闭后继续运行
    # 标准输出和错误都重定向到日志文件
    nohup "${PYTHON_CMD}" -m bridge \
        >> "${LOG_FILE}" 2>&1 &

    PID=$!
    echo "${PID}" > "${PID_FILE}"

    # 等待几秒检查进程是否正常启动
    sleep 2
    if kill -0 "${PID}" 2>/dev/null; then
        log_ok "服务已后台启动（PID: ${PID}）"
        log_info "日志文件: ${LOG_FILE}"
        log_info "PID 文件: ${PID_FILE}"

        # 检查服务端口是否正常监听
        sleep 1
        PORT=$(grep -oP 'BRIDGE_PORT=\K\d+' "${ENV_FILE}" 2>/dev/null || echo "9641")
        if command -v ss &>/dev/null; then
            if ss -tlnp | grep -q ":${PORT}"; then
                log_ok "服务端口 ${PORT} 正在监听"
            fi
        elif command -v netstat &>/dev/null; then
            if netstat -tlnp 2>/dev/null | grep -q ":${PORT}"; then
                log_ok "服务端口 ${PORT} 正在监听"
            fi
        fi

        if [ "${TAIL}" = true ]; then
            echo ""
            log_info "正在跟踪日志（Ctrl+C 停止跟踪，服务仍在后台运行）..."
            echo ""
            tail -f "${LOG_FILE}"
        fi
    else
        log_error "服务启动失败！查看日志: ${LOG_FILE}"
        tail -20 "${LOG_FILE}"
        rm -f "${PID_FILE}"
        exit 1
    fi
else
    # ── 前台调试模式 ────────────────────────────────────────────────
    log_info "前台模式启动（Ctrl+C 停止）..."
    echo ""
    "${PYTHON_CMD}" -m bridge 2>&1 | tee -a "${LOG_FILE}"
fi
