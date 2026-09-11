"""错误信息脱敏。

这是第 5 层防线（信息泄露）的实现。公网环境下，一条把
`http://192.168.1.20:18789` 打印回浏览器的错误信息，就等于把内网拓扑送出去。

规矩：任何要写进日志或（万不得已）返回给用户的外部异常字符串，都先过 scrub()。
"""

from __future__ import annotations

import re

RULES: list[tuple[re.Pattern[str], str]] = [
    # URL（http/https），常出现在 httpx 异常里
    (re.compile(r"https?://[^\s'\"<>)\]]+"), "<url-removed>"),
    # 裸 IPv4，可带端口
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "<host-removed>"),
    # 形如 ::1 / [::1]:8080 的 IPv6
    (re.compile(r"\[?[0-9a-fA-F:]{2,}:[0-9a-fA-F:]{2,}\]?(?::\d{1,5})?"), "<host-removed>"),
    # Bearer / token= / api_key=
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer <token-removed>"),
    (re.compile(r"(?i)\b(?:token|api[_-]?key|password|secret)\b\s*[=:]\s*\S+"), "<secret-removed>"),
    (re.compile(r"(?i)\bx-openclaw-[a-z-]+\s*[=:]\s*\S+"), "<header-removed>"),
    # 长得像不透明密钥的串（32 位以上连续十六进制或 base64-ish）
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<opaque-removed>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<opaque-removed>"),
    # 文件系统路径（可能暴露 NAS 目录结构）
    (re.compile(r"(?<![\w.])/(?:home|root|mnt|volume\d*|share|srv|etc|opt|usr|var)/[^\s'\"<>)\]]*"), "<path-removed>"),
    (re.compile(r"(?i)\b[A-Z]:\\\\?[^\s'\"<>)\]]*"), "<path-removed>"),
]


def scrub(text: object, *, limit: int = 600) -> str:
    """把外部异常字符串洗成可安全写日志的形态。"""
    if text is None:
        return ""
    out = str(text)
    for pattern, replacement in RULES:
        out = pattern.sub(replacement, out)
    out = out.replace("\r", " ").replace("\n", " ")
    if len(out) > limit:
        out = out[:limit] + "…(截断)"
    return out
