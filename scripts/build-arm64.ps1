# ============================================================
# 交叉构建 ARM64 镜像并导出 tar（Windows PowerShell）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\build-arm64.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build-arm64.ps1 -Version 1.0.1
#   powershell -ExecutionPolicy Bypass -File scripts\build-arm64.ps1 -Version 1.0.1 -BuildUid 0
#
#   如果导入到极空间后挂载目录写入报 Permission denied，用 -BuildUid 0 重建。
# ============================================================
param(
    [string]$Version = "1.0.0",
    [string]$BuildUid = "10001"
)

$ErrorActionPreference = "Stop"

$Image = "openclaw-workbench:$Version"
$Output = "openclaw-workbench-$Version.tar"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> 检查 Docker 是否可用"
docker version --format '{{.Server.Version}}' | Out-Null
if (-not $?) { throw "无法连接 Docker。请确认 Docker Desktop 已启动。" }

Write-Host "==> 准备 buildx builder（已存在则跳过）"
docker buildx inspect owb-builder 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    docker buildx create --use --name owb-builder | Out-Null
}
docker buildx inspect --bootstrap | Out-Null

Write-Host "==> 构建 linux/arm64/v8 镜像：$Image（容器 UID=$BuildUid）"
docker buildx build `
    --platform linux/arm64/v8 `
    --build-arg "OWB_UID=$BuildUid" `
    --build-arg "OWB_GID=$BuildUid" `
    -t $Image `
    . `
    --load
if ($LASTEXITCODE -ne 0) { throw "构建失败。" }

Write-Host "==> 导出为 tar：$Output"
# 必须带 tag：极空间只接受有 tag 信息的 tar
docker save $Image -o $Output
if ($LASTEXITCODE -ne 0) { throw "导出失败。" }

$Size = [math]::Round((Get-Item $Output).Length / 1MB, 1)

Write-Host ""
Write-Host "完成。"
Write-Host "  镜像文件：$Root\$Output  ($Size MB)"
Write-Host "  镜像标签：$Image"
Write-Host ""
Write-Host "下一步：把这个 tar 上传到极空间，用「Docker → 镜像 → 导入镜像」导入。"
Write-Host "详细步骤见 docs/DEPLOY-NAS.md"
