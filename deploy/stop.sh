#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  OutlineRAGBridge 停止脚本
#
#  用法:
#    ./stop.sh               # 正常停止服务
#    ./stop.sh -f            # 强制停止（SIGKILL）
#    ./stop.sh -c            # 清理日志（停止后删除日志文件）
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
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 配置 ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="${SCRIPT_DIR}/bridge.pid"

# ── 解析参数 ─────────────────────────────────────────────────────────
FORCE=false
CLEAN=false
while getopts "fc" opt; do
    case $opt in
        f) FORCE=true ;;
        c) CLEAN=true ;;
        *) ;;
    esac
done

# ── 查找进程 ─────────────────────────────────────────────────────────

PID=""

# 优先从 PID 文件读取
if [ -f "${PID_FILE}" ]; then
    PID=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [ -n "${PID}" ] && ! kill -0 "${PID}" 2>/dev/null; then
        # PID 文件存在但进程已不存在
        log_warn "PID 文件中的进程（${PID}）已不存在，清理 PID 文件..."
        rm -f "${PID_FILE}"
        PID=""
    fi
fi

# 如果 PID 文件未找到，尝试从进程中查找
if [ -z "${PID}" ]; then
    PID=$(pgrep -f "python.*bridge.py" 2>/dev/null || echo "")
    if [ -n "${PID}" ]; then
        log_info "从进程列表中找到 bridge 进程（PID: ${PID}）"
    fi
fi

# ── 停止进程 ─────────────────────────────────────────────────────────

if [ -z "${PID}" ]; then
    log_warn "未找到运行中的 OutlineRAGBridge 服务。"
    rm -f "${PID_FILE}"
    exit 0
fi

echo ""
log_info "正在停止 OutlineRAGBridge（PID: ${PID}）..."

if [ "${FORCE}" = true ]; then
    # 强制停止（SIGKILL）
    log_warn "强制终止进程..."
    kill -9 "${PID}" 2>/dev/null || true
else
    # 正常停止（先发 SIGTERM，等待进程自行退出）
    log_info "发送 SIGTERM 信号..."
    kill -15 "${PID}" 2>/dev/null || true

    # 等待进程退出（最多 10 秒）
    WAIT_COUNT=0
    while kill -0 "${PID}" 2>/dev/null; do
        WAIT_COUNT=$((WAIT_COUNT + 1))
        if [ "${WAIT_COUNT}" -ge 10 ]; then
            log_warn "进程在 10 秒内未退出，发送 SIGKILL..."
            kill -9 "${PID}" 2>/dev/null || true
            break
        fi
        sleep 1
    done
fi

# 验证进程已停止
if kill -0 "${PID}" 2>/dev/null; then
    log_error "进程（${PID}）未能停止！"
    exit 1
fi

log_ok "服务已停止（PID: ${PID}）"

# 清理 PID 文件
rm -f "${PID_FILE}"

# 可选：清理日志
if [ "${CLEAN}" = true ]; then
    LOG_DIR="${SCRIPT_DIR}/logs"
    if [ -d "${LOG_DIR}" ]; then
        log_info "清理日志文件..."
        rm -rf "${LOG_DIR:?}/"*
        log_ok "日志文件已清理"
    fi
fi

echo ""
