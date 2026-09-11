#!/usr/bin/env python3
"""部署前静态校验 —— 在本机就能查完的部分，先查完。

对应交付清单里的第 7、8、9、17 项：

  7.  Dockerfile 检查（构建前的静态审查；真机构建见 docs/DEPLOY-NAS.md）
  8.  ARM64 架构检查（逐个依赖查 PyPI 有没有 aarch64 轮子）
  9.  Compose 配置检查（端口、挂载、危险选项）
  17. 公网暴露前端口检查（确认 18789 不会出现在任何对外映射里）

这些检查【不需要 Docker】，所以在你没有 Docker 的电脑上也能跑。
真机构建与运行测试必须在 NAS 上做，那部分脚本不做，也不假装做了。

用法：
    python scripts/check_deploy.py
    python scripts/check_deploy.py --report out.txt   # 结果另存 UTF-8（终端乱码时用）
    python scripts/check_deploy.py --offline          # 跳过联网的 PyPI 检查

退出码 0 = 全部通过；1 = 有错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FINDINGS: list[tuple[str, str]] = []   # (level, message)


def ok(msg: str) -> None:
    FINDINGS.append(("PASS", msg))


def bad(msg: str) -> None:
    FINDINGS.append(("ERROR", msg))


def warn(msg: str) -> None:
    FINDINGS.append(("WARN", msg))


def section(title: str) -> None:
    FINDINGS.append(("SECTION", title))


# ----------------------------------------------------------------- 工具

def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


BARE_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")


def collect_list_block(text: str, key: str) -> list[str]:
    """取 `key:` 下方的 `- item` 列表项（只看第一处）。"""
    items: list[str] = []
    key_indent = -1
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if key_indent < 0:
            if BARE_KEY.match(raw.strip()) and raw.strip().startswith(f"{key}:"):
                key_indent = indent
            continue
        if indent <= key_indent:
            break
        if raw.strip().startswith("- "):
            items.append(raw.strip()[2:].strip())
    return items


def collect_map_block(text: str, key: str) -> dict[str, str]:
    """取 `key:` 下面的 `K: V` 映射（只看第一处）。"""
    out: dict[str, str] = {}
    key_indent = -1
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        body = raw.strip()
        if key_indent < 0:
            if body.startswith(f"{key}:"):
                key_indent = indent
            continue
        if indent <= key_indent:
            break
        if body.startswith("- "):
            continue
        m = BARE_KEY.match(body)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def unquote(s: str) -> str:
    return s.strip().strip('"').strip("'")


# ----------------------------------------------------------------- 第 7 项

def check_dockerfile() -> None:
    section("7. Dockerfile 静态审查")
    text = read("Dockerfile")

    base = re.search(r"^FROM\s+(\S+)", text, re.M)
    if not base:
        bad("Dockerfile 里找不到 FROM")
    elif base.group(1).startswith("python:3.12-slim"):
        ok(f"基础镜像 {base.group(1)}（Debian slim，PyMuPDF 在 musl 上容易出问题，故不用 alpine）")
    else:
        warn(f"基础镜像为 {base.group(1)}，请确认它在 linux/arm64 上有对应镜像")

    if re.search(r"^USER\s+(?!root)\S+", text, re.M):
        user = re.search(r"^USER\s+(\S+)", text, re.M).group(1)
        ok(f"以非 root 身份运行（USER {user}）")
    else:
        bad("Dockerfile 没有设置非 root 的 USER —— 容器会以 root 运行")

    if re.search(r"^EXPOSE\s+8080\b", text, re.M):
        ok("EXPOSE 8080")
    else:
        bad("EXPOSE 端口不是 8080")

    if "18789" in text:
        bad("Dockerfile 里出现了 18789 —— 工作台不应感知 AI 服务的端口")
    else:
        ok("Dockerfile 里不含 18789")

    cmd = re.search(r"^CMD\s+(.*)$", text, re.M)
    if cmd and "--no-server-header" in cmd.group(1):
        ok("CMD 带 --no-server-header（避免 uvicorn 把实现细节交给公网）")
    else:
        bad("CMD 缺少 --no-server-header")

    if re.search(r"COPY\s+\.\s", text):
        bad("Dockerfile 里出现 `COPY . `  —— 会把 .env、data/ 一起打进镜像")
    else:
        ok("没有 `COPY . `，只按需复制目录")

    if re.search(r"(curl|wget)\s+https?://", text):
        warn("Dockerfile 里有从网络下载的命令，请确认来源可信")
    else:
        ok("没有运行期的外部下载命令")

    for needed in ("app", "prompts", "scripts", "requirements.txt"):
        if f"COPY {needed}" in text:
            ok(f"复制了 {needed}")
        else:
            warn(f"未在 Dockerfile 里看到 COPY {needed}")


# ----------------------------------------------------------------- 第 8 项

# 只取行首的包名：后面的版本约束形态太多（>=1,<2 / ==1.2.* / ; python_version），
# 逐个解析不划算，而这里本来就只关心"是哪个包"。
REQ_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)")


def parse_requirements() -> list[str]:
    pkgs: list[str] = []
    for line in read("requirements.txt").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = REQ_NAME.match(line)
        if m:
            pkgs.append(m.group(1))
    return pkgs


def pypi_arch_report(pkg: str) -> tuple[str, str]:
    """返回 (状态, 说明)。状态 ∈ ok / warn / fail。"""
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openclaw-workbench-check"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return "warn", f"无法联网查询 PyPI（{type(exc).__name__}），ARM64 轮子未验证"

    version = data.get("info", {}).get("version", "?")
    files = data.get("urls", []) or []
    names = [f.get("filename", "") for f in files]
    kinds = {f.get("packagetype", "") for f in files}

    # ★ 必须区分 macOS ARM64 与 Linux ARM64 —— 两者完全不通用。
    #   只找 "arm64" 会命中 macOS 的轮子，让人误以为 NAS 上也能装。
    pure = [n for n in names if n.endswith("-none-any.whl")]
    linux_arm = [
        n for n in names
        if ("aarch64" in n or "arm64" in n)
        and ("manylinux" in n or "musllinux" in n or "_linux_" in n or "linux_" in n)
    ]
    mac_arm = [n for n in names if ("aarch64" in n or "arm64" in n) and "macos" in n]

    if pure:
        return "ok", f"{pkg} {version}：纯 Python 轮子（与架构无关）"
    if linux_arm:
        return "ok", f"{pkg} {version}：有 Linux ARM64 轮子（{linux_arm[0]}）"
    if mac_arm:
        return "warn", (
            f"{pkg} {version}：只找到 macOS ARM64 轮子（{mac_arm[0]}），"
            "没有 Linux aarch64 轮子 —— 极空间上可能装不上，务必先确认"
        )
    if "sdist" in kinds and not any(n.endswith(".whl") for n in names):
        return "fail", f"{pkg} {version}：只有源码包，ARM64 上安装需要编译工具链"
    shown = ", ".join(names[:3]) if names else "无 wheel"
    return "warn", f"{pkg} {version}：未找到 Linux ARM64 轮子（现有：{shown}）"


def check_arm64(offline: bool) -> None:
    section("8. ARM64 架构检查（依赖轮子）")
    pkgs = parse_requirements()
    ok(f"运行时依赖共 {len(pkgs)} 个：{', '.join(pkgs)}")

    if offline:
        warn("--offline：跳过 PyPI 轮子检查")
    else:
        for pkg in pkgs:
            status, msg = pypi_arch_report(pkg)
            if status == "ok":
                ok(msg)
            elif status == "fail":
                bad(msg)
            else:
                warn(msg)

    df = read("Dockerfile")
    if re.search(r"\b(apt-get|apk)\s+.*\b(gcc|build-essential|make|python3-dev)\b", df):
        warn("Dockerfile 里安装了编译工具链 —— 说明有依赖需要现场编译，拖慢构建")
    else:
        ok("镜像内不安装编译工具链（前提是上面所有依赖都有 ARM64 轮子）")


# ----------------------------------------------------------------- 第 9 项

ALLOWED_CONTAINER_MOUNTS = {"/data/literature", "/data/life_notes", "/app/prompts"}

MOUNT_RE = re.compile(r":(/[^:]+?)(?::(?:ro|rw))?$")


def strip_comments(text: str) -> str:
    """去掉整行注释，再拿去做"危险项"检查。

    必须这么做：本项目 compose 顶部刻意写了一段"本文件没有出现哪些危险项"
    的说明，那些字面（privileged / cap_add / docker.sock）若参与匹配，
    检查会 100% 误报——而一个总在误报的检查，最后一定会被人无视。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def check_compose() -> None:
    section("9. docker-compose 配置检查")
    text = strip_comments(read("docker-compose.yml"))

    # --- 端口 ---
    ports = [unquote(p) for p in collect_list_block(text, "ports")]
    if not ports:
        bad("compose 里没有 ports（工作台需要对外提供 8080）")
    for p in ports:
        if "18789" in p:
            bad(f"ports 里出现了 AI 服务端口：{p}")
        elif re.fullmatch(r"[\w${}:.\-]*8080:8080", p) or p.endswith(":8080"):
            ok(f"对外映射 {p}（容器内 8080）")
        else:
            warn(f"端口映射 {p} 不在预期内（预期形如 8080:8080）")

    # --- 挂载 ---
    vols = [unquote(v) for v in collect_list_block(text, "volumes")]
    if not vols:
        bad("compose 里没有 volumes")
    for v in vols:
        if "docker.sock" in v:
            bad(f"挂载了 Docker socket：{v} —— 等于把整台 NAS 的控制权交出去")
            continue
        m = MOUNT_RE.search(v)
        if not m:
            warn(f"无法解析的挂载项：{v}")
            continue
        container_path = m.group(1)
        if container_path in ALLOWED_CONTAINER_MOUNTS:
            ok(f"挂载 {container_path}（在白名单内）")
        else:
            bad(f"挂载了白名单外的容器路径：{container_path}")

    # --- 危险选项 ---
    for danger, why in (
        ("privileged", "特权模式"),
        ("cap_add", "额外内核能力"),
        ("network_mode", "自定义网络模式（host 会绕过端口隔离）"),
        ("pid:", "共享 PID 命名空间"),
        ("ipc:", "共享 IPC 命名空间"),
        ("devices:", "直接映射宿主设备"),
        ("/var/run/docker.sock", "Docker socket"),
    ):
        if danger in text:
            bad(f"compose 里出现了 {danger}（{why}）")
    if not any(d in text for d in ("privileged", "cap_add", "network_mode", "devices:")):
        ok("没有 privileged / cap_add / network_mode / devices")

    # --- 应当存在的加固项 ---
    if "no-new-privileges" in text:
        ok("启用了 no-new-privileges")
    else:
        warn("未启用 no-new-privileges")
    if "mem_limit" in text:
        ok("设置了内存上限（4GB NAS 上与其他容器共存更重要）")
    else:
        warn("未设置 mem_limit")
    if "healthcheck" in text:
        ok("配置了 healthcheck")
    else:
        warn("未配置 healthcheck")

    # --- 环境变量与模板对齐 ---
    env_block = collect_map_block(text, "environment")
    example = read(".env.example")
    example_keys = set()
    for line in example.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        example_keys.add(line.split("=", 1)[0].strip())

    for key, value in env_block.items():
        if key not in example_keys and "${" not in key:
            warn(f"compose 引用了环境变量 {key}，但 .env.example 里没有它")
    ok(f"compose 共引用 {len(env_block)} 个环境变量，已与 .env.example 比对")


# ----------------------------------------------------------------- 第 17 项

def check_public_exposure() -> None:
    section("17. 公网暴露前端口检查")

    compose = strip_comments(read("docker-compose.yml"))
    ports = [unquote(p) for p in collect_list_block(compose, "ports")]
    published = sorted(
        p.split(":")[0] for p in ports if re.fullmatch(r"[\w${}.\-]*:\d+", p)
    )
    if any("18789" in p for p in published):
        bad("对外发布的端口里包含 18789")
    else:
        ok(f"对外发布端口：{', '.join(published) or '(无)'} ；不含 18789")

    # 前端是唯一会走到公网的静态资源，必须零外链、零服务标识
    static_dir = ROOT / "app" / "static"
    offenders: list[str] = []
    for f in static_dir.rglob("*"):
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for pattern in (r"(?i)openclaw", r"\b18789\b", r"(?i)deepseek"):
            if re.search(pattern, text):
                offenders.append(f"{f.relative_to(ROOT).as_posix()} 命中 {pattern}")
    if offenders:
        for o in offenders:
            bad(f"前端资源里出现服务标识：{o}")
    else:
        ok("前端资源不含 AI 服务的名称 / 端口 / 厂商")

    external = re.compile(r"https?://(?!127\.0\.0\.1|localhost|www\.w3\.org)")
    hits: list[str] = []
    for f in static_dir.rglob("*"):
        if f.is_file() and external.search(f.read_text(encoding="utf-8", errors="replace")):
            hits.append(f.relative_to(ROOT).as_posix())
    if hits:
        bad(f"前端存在外部资源引用（穿透按流量计费，外链会持续消耗）：{', '.join(hits)}")
    else:
        ok("前端零外链（无 CDN、无在线字体、无第三方脚本）")

    # 反代场景：不能依赖 SSE / WebSocket
    if re.search(r"(?i)\b(websocket|text/event-stream|EventSource)\b", read("app/static/app.js")):
        bad("前端使用了 WebSocket / SSE —— 穿透网关会缓冲或切断长连接")
    else:
        ok("任务进度用短轮询（穿透网关下最稳）")


# ----------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="openclaw-workbench 部署前静态校验")
    parser.add_argument("--report", default="", help="把结果另存为 UTF-8 文件")
    parser.add_argument("--offline", action="store_true", help="跳过联网检查")
    args = parser.parse_args()

    check_dockerfile()
    check_arm64(args.offline)
    check_compose()
    check_public_exposure()

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("openclaw-workbench 部署前静态校验")
    lines.append("=" * 72)
    lines.append("")
    errors = warns = 0
    for level, msg in FINDINGS:
        if level == "SECTION":
            lines.append("")
            lines.append(msg)
            lines.append("-" * len(msg))
        elif level == "PASS":
            lines.append(f"  [PASS ] {msg}")
        elif level == "WARN":
            warns += 1
            lines.append(f"  [WARN ] {msg}")
        else:
            errors += 1
            lines.append(f"  [ERROR] {msg}")

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"结果：{errors} 项错误，{warns} 项警告")
    if errors:
        lines.append("请先处理上面的错误项。")
    else:
        lines.append("静态校验通过。真机构建 / 启动 / 挂载测试仍需在 NAS 上完成。")
    lines.append("=" * 72)

    text = "\n".join(lines)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
