#!/usr/bin/env bash
# ============================================================
# 交叉构建 ARM64 镜像并导出 tar（Linux / macOS / Git Bash）
#
# 为什么必须在电脑上构建：
#   极空间不开放 SSH，Docker 界面也没有构建入口，只接受带 tag 的 tar 导入。
#   所以"构建 → docker save → 手动上传 → 导入"是唯一稳定路径。
#
# 用法：
#   bash scripts/build-arm64.sh 1.0.0
#   bash scripts/build-arm64.sh 1.0.1 --uid 0     # 挂载目录写入报错时用
# ============================================================
set -euo pipefail

VERSION="${1:-1.0.0}"
UID_ARG="${2:-}"
UID_VALUE="10001"

if [[ "$UID_ARG" == "--uid" ]]; then
  UID_VALUE="${3:-0}"
fi

IMAGE="openclaw-workbench:${VERSION}"
OUTPUT="openclaw-workbench-${VERSION}.tar"

cd "$(dirname "$0")/.."

echo "==> 检查 Docker 是否可用"
docker version --format '{{.Server.Version}}' >/dev/null

echo "==> 准备 buildx builder（已存在则跳过）"
if ! docker buildx inspect owb-builder >/dev/null 2>&1; then
  docker buildx create --use --name owb-builder
fi
docker buildx inspect --bootstrap >/dev/null

echo "==> 构建 linux/arm64/v8 镜像：${IMAGE}（容器 UID=${UID_VALUE}）"
docker buildx build \
  --platform linux/arm64/v8 \
  --build-arg "OWB_UID=${UID_VALUE}" \
  --build-arg "OWB_GID=${UID_VALUE}" \
  -t "${IMAGE}" \
  . \
  --load

echo "==> 导出为 tar：${OUTPUT}"
# 必须带 tag：极空间只接受有 tag 信息的 tar
docker save "${IMAGE}" -o "${OUTPUT}"

SIZE=$(du -h "${OUTPUT}" | cut -f1)
echo
echo "完成。"
echo "  镜像文件：$(pwd)/${OUTPUT}  （${SIZE}）"
echo "  镜像标签：${IMAGE}"
echo
echo "下一步：把这个 tar 上传到极空间，用「Docker → 镜像 → 导入镜像」导入。"
echo "详细步骤见 docs/DEPLOY-NAS.md"
