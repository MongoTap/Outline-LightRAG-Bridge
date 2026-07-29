#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  Python 3.12 便携版下载工具
#  在能联网的机器上运行此脚本，下载 Python 3.12 便携版
#  然后将下载的文件放到部署包的 python/ 目录中，部署时会自动使用
#
#  用法:
#    ./download_python.sh                    # 交互式下载
#    ./download_python.sh /path/to/deploy    # 直接下载到部署包目录
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

RELEASE_TAG="20260610"
PYTHON_VERSION="3.12.13"
FILENAME="cpython-${PYTHON_VERSION}+${RELEASE_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
DOWNLOAD_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE_TAG}/cpython-${PYTHON_VERSION}%2B${RELEASE_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
EXPECTED_SIZE="32.6 MB"

# ── 颜色 ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()  { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
log_info "Python ${PYTHON_VERSION} 便携版下载工具"
log_info "==================================="
echo ""
log_info "将从 GitHub Release 下载预编译的 Python ${PYTHON_VERSION}"
log_info "文件大小: ~${EXPECTED_SIZE}"
log_info "下载地址: ${DOWNLOAD_URL}"
echo ""

# 确定目标目录
TARGET_DIR="${1:-./python}"
if [ "$#" -eq 0 ]; then
    echo ""
    echo -n "下载到哪个目录？[默认: ./python] "
    read -r INPUT_DIR
    if [ -n "${INPUT_DIR}" ]; then
        TARGET_DIR="${INPUT_DIR}"
    fi
fi

mkdir -p "${TARGET_DIR}"
TARGET_FILE="${TARGET_DIR}/${FILENAME}"

if [ -f "${TARGET_FILE}" ]; then
    log_warn "${FILENAME} 已存在（${TARGET_DIR}）"
    echo -n "是否重新下载？[y/N] "
    read -r RE_DOWNLOAD
    if [[ ! "${RE_DOWNLOAD}" =~ ^[Yy] ]]; then
        log_info "跳过下载。"
        exit 0
    fi
fi

echo ""
log_info "正在下载 Python ${PYTHON_VERSION}（这可能需要几分钟）..."
echo ""

# 使用 curl 或 wget 下载
if command -v curl &>/dev/null; then
    curl -L --progress-bar -o "${TARGET_FILE}" "${DOWNLOAD_URL}"
elif command -v wget &>/dev/null; then
    wget --show-progress -O "${TARGET_FILE}" "${DOWNLOAD_URL}"
else
    log_error "未找到 curl 或 wget，请先安装其中一个。"
    exit 1
fi

echo ""
if [ -f "${TARGET_FILE}" ]; then
    ACTUAL_SIZE=$(du -h "${TARGET_FILE}" | cut -f1)
    log_ok "下载完成: ${TARGET_FILE}"
    log_info "文件大小: ${ACTUAL_SIZE}"
    echo ""
    log_info "将此文件放在部署包的 python/ 目录下，部署脚本会自动使用它。"
    log_info "例如: cp ${TARGET_FILE} /path/to/deploy/python/"
else
    log_error "下载失败！"
    log_info "请手动访问以下地址，下载后放到 ${TARGET_DIR}/ 目录："
    log_info "  ${DOWNLOAD_URL}"
    exit 1
fi
