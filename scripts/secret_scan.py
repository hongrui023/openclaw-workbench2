#!/usr/bin/env python3
"""提交前扫描 —— 确保仓库里没有机密、没有私人数据、没有危险代码。

一次覆盖三件事：

  1. **机密扫描**：真实 Token、私钥、口令字面量。
  2. **私人数据扫描**：PDF、daily_notes.md、workbench_log.md、.env。
  3. **代码不变量扫描**：app/ 下不得出现任何删除 / 移动 / 重命名能力。
     这是设计契约，不是"暂时没写"——用语法树（ast）判断，
     所以注释和文档字符串里解释"为什么没有删除能力"不会被误报。

为什么不用 gitleaks / trufflehog：
  - 它们扫的是通用机密特征，而本项目要守的规矩更具体
    （"app/ 里不许有 unlink"、"literature/ 不许进仓库"），
    用现成工具反而要写更多胶水，还多一个依赖；
  - 本脚本零依赖，NAS 上直接能跑：python scripts/secret_scan.py

用法：
    python scripts/secret_scan.py                    # 扫整个工作区
    python scripts/secret_scan.py --staged           # 只扫 git 暂存区（提交前最后一道）
    python scripts/secret_scan.py --report out.txt   # 结果另存为 UTF-8 文件

退出码 0 = 干净；1 = 发现问题。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "var", "node_modules", ".pytest_cache", ".mypy_cache"}
BINARY_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".tar",
    ".whl", ".so", ".dll", ".exe", ".pyc", ".woff", ".woff2", ".ttf", ".mp4", ".sqlite",
}

# ---------------------------------------------------------------- 规则

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI 风格 API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("私钥文件内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 真实令牌", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{24,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
]

# 按【文件路径】判断的私人数据 / 机密文件
PRIVATE_PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PDF 文件", re.compile(r"(?i)\.pdf$")),
    ("生活记录数据", re.compile(r"(?i)(^|/)life_notes/")),
    ("文献数据", re.compile(r"(^|/)literature/")),
    ("日常记录文件", re.compile(r"(?i)(^|/)daily_notes\.md$")),
    ("任务日志文件", re.compile(r"(?i)(^|/)workbench_log\.md$")),
    ("真实环境文件（.env 不该进仓库）", re.compile(r"(^|/)\.env$")),
]

# 说明"这不是真值"的痕迹
PLACEHOLDER_HINTS = (
    "REPLACE_ME", "CHANGE_ME", "YOUR_", "EXAMPLE", "PLACEHOLDER", "<", ">",
    "${", "xxx", "XXX", "dummy", "DUMMY", "test-token", "fake", "换掉", "填", "留空",
)

# 明确禁止沿用的口令字面量。分片拼接是刻意的：
# 否则本文件自己就会命中这条规则（源码文本里不出现完整串）。
FORBIDDEN_LITERALS = [
    "dev-only-" + "password-please-change",
]

# app/ 下不允许出现的文件系统删除 / 移动 / 重命名能力。
# 分三组是因为"辨识度"不同：
#   - 模块级调用（os.remove / shutil.rmtree）：靠 import 别名追踪，最准；
#   - Path 实例方法（.unlink / .rmdir / .rename）：str 没有同名方法，不会误报；
#   - 刻意【不含】.replace()：str.replace 是最常见的字符串操作，
#     把 Path.replace 一并拦会淹没在误报里（os.replace 仍由第一组挡住）。
OS_DANGEROUS = {"remove", "unlink", "rmdir", "removedirs", "rename", "replace"}
SHUTIL_DANGEROUS = {"rmtree", "move"}
PATH_METHOD_DANGEROUS = {"unlink", "rmdir", "rename"}
# from os import remove 这类直接导入后裸调用
BARE_DANGEROUS = {
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename",
    "shutil.rmtree", "shutil.move",
}

PRIVATE_IP = re.compile(
    r"\b(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")\b"
)

# 这些文件里出现内网 IP 是本职（它们是模板/文档，教用户"这里填你的地址"）
IP_EXEMPT_PREFIXES = ("docs/",)
IP_EXEMPT_BASENAMES = {".env.example", "docker-compose.yml", "docker-compose.yaml"}

# 前端（app/static/）里绝不允许出现的东西
FRONTEND_FORBIDDEN = [
    (r"(?i)openclaw", "AI 服务名"),
    (r"\b18789\b", "AI 服务端口"),
    (r"(?i)deepseek", "后端模型供应商名"),
    (r"(?i)bearer", "认证头字样"),
    # 外链一律禁止；www.w3.org 是 XML 命名空间标识符，浏览器不会去请求它，属正常
    (r"https?://(?!127\.0\.0\.1|localhost|www\.w3\.org)", "外部网络地址"),
]

PY_COMMENT = re.compile(r"^\s*#")


class Finding:
    __slots__ = ("level", "path", "line", "message")

    def __init__(self, level: str, path: str, line: int, message: str) -> None:
        self.level = level          # "ERROR" | "WARN"
        self.path = path
        self.line = line
        self.message = message


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def iter_files() -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in BINARY_SUFFIXES:
                continue
            files.append(p)
    return files


def is_placeholder(line: str) -> bool:
    return any(hint in line for hint in PLACEHOLDER_HINTS)


def _docstring_lines(tree: ast.AST) -> set[int]:
    """收集所有文档字符串占用的行号，用于跳过"解释性文字"里的示例。"""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                start = first.value.lineno
                end = getattr(first.value, "end_lineno", start) or start
                lines.update(range(start, end + 1))
    return lines


def scan_python_ast(text: str, name: str, suffix: str) -> list[Finding]:
    """app/ 下的删除能力检测——用语法树，注释与文档字符串天然不会被误判。

    为什么必须追踪 import 别名而不是匹配方法名：
      `s.replace(a, b)` 是字符串操作，`os.replace(a, b)` 是文件覆盖。
      只看 `.replace` 会把前者全打成违规——那种检查会因为误报太多而
      被人关掉，等于没有。所以先建立 "本地名 -> 真实模块" 的映射，
      只在确认调用方是 os / shutil 时才判违规。
    """
    findings: list[Finding] = []
    if not name.startswith("app/") or suffix != ".py":
        return findings

    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError as exc:
        findings.append(
            Finding("ERROR", name, exc.lineno or 0, f"Python 语法错误，无法完成静态检查（{exc.msg}）")
        )
        return findings

    # 本地名 -> 真实全限定名。import shutil as sh => sh -> shutil
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def report(lineno: int, what: str) -> None:
        findings.append(
            Finding("ERROR", name, lineno, f"app/ 下出现了{what}（违反设计契约）")
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        if isinstance(func, ast.Name):
            # from os import remove; remove(...)
            real = modules.get(func.id)
            if real in BARE_DANGEROUS:
                report(node.lineno, f"删除/移动调用 {real}()")
            continue

        if not isinstance(func, ast.Attribute):
            continue

        owner = func.value
        if isinstance(owner, ast.Name):
            real = modules.get(owner.id, owner.id)
            if real == "os" and func.attr in OS_DANGEROUS:
                report(node.lineno, f"删除/移动调用 os.{func.attr}()")
                continue
            if real == "shutil" and func.attr in SHUTIL_DANGEROUS:
                report(node.lineno, f"删除/移动调用 shutil.{func.attr}()")
                continue

        # Path 实例方法：str 没有 unlink / rmdir / rename，误报率低
        if func.attr in PATH_METHOD_DANGEROUS:
            report(node.lineno, f"删除/移动调用 .{func.attr}()")

    return findings


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    name = rel(path)
    is_self = path.resolve() == SELF

    for label, pattern in PRIVATE_PATH_PATTERNS:
        if pattern.search(name):
            if name.endswith(".env.example"):
                continue
            findings.append(
                Finding("ERROR", name, 0, f"仓库里出现了不该提交的文件：{label}")
            )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    findings.extend(scan_python_ast(text, name, path.suffix))

    # 文档字符串占用的行（只有 .py 才有意义）
    doc_lines: set[int] = set()
    if path.suffix == ".py":
        try:
            doc_lines = _docstring_lines(ast.parse(text))
        except SyntaxError:
            doc_lines = set()

    ip_sensitive = (
        path.suffix in {".py", ".js", ".html", ".sh", ".ps1", ".yml", ".yaml", ".json", ".toml", ".ini"}
        and not name.startswith(IP_EXEMPT_PREFIXES)
        and path.name not in IP_EXEMPT_BASENAMES
    )

    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()

        # 1) 机密
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line) and not is_placeholder(stripped):
                findings.append(Finding("ERROR", name, idx, f"疑似真实机密（{label}）"))
                break

        # 2) 禁止沿用的口令字面量（扫描器自身除外：它必须写下这条规则）
        if not is_self:
            for literal in FORBIDDEN_LITERALS:
                if literal in line:
                    findings.append(
                        Finding("ERROR", name, idx, f"出现了明确禁止沿用的口令字面量：{literal}")
                    )

        # 3) 内网 IP —— 跳过注释与文档字符串（那里是示例）
        if ip_sensitive and PRIVATE_IP.search(line):
            if idx not in doc_lines and not PY_COMMENT.match(line) and not is_placeholder(stripped):
                findings.append(Finding("WARN", name, idx, "代码里写死了内网 IP 地址"))

        # 4) 前端硬约束
        if name.startswith("app/static/"):
            for pattern, label in FRONTEND_FORBIDDEN:
                if re.search(pattern, line):
                    findings.append(
                        Finding("ERROR", name, idx, f"前端资源里出现了{label}（违反零外链/零标识约束）")
                    )

    return findings


def staged_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return []
    return [ROOT / line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="openclaw-workbench 提交前扫描")
    parser.add_argument("--staged", action="store_true", help="只扫描 git 暂存区")
    parser.add_argument("--report", default="", help="把结果另存为 UTF-8 文件")
    args = parser.parse_args()

    targets = staged_files() if args.staged else []
    scope = "git 暂存区" if targets else "整个工作区"
    if not targets:
        targets = iter_files()

    findings: list[Finding] = []
    for path in targets:
        if path.is_file():
            findings.extend(scan_file(path))

    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"openclaw-workbench 提交前扫描 —— 范围：{scope}，共 {len(targets)} 个文件")
    lines.append("=" * 72)
    lines.append("")
    if not findings:
        lines.append("  [PASS] 未发现机密、私人数据或危险调用。")
    else:
        for f in sorted(findings, key=lambda x: (x.level != "ERROR", x.path, x.line)):
            where = f"{f.path}:{f.line}" if f.line else f.path
            tag = "ERROR" if f.level == "ERROR" else "WARN "
            lines.append(f"  [{tag}] {where}  {f.message}")
    lines.append("")
    lines.append("-" * 72)
    lines.append(f"结果：{len(errors)} 项错误，{len(warns)} 项警告")
    lines.append("仓库【不应提交】——请先处理上面的错误项。" if errors else "仓库可以提交。")
    lines.append("-" * 72)

    text = "\n".join(lines)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
