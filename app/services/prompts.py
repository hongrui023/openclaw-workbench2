"""提示词模板加载。

模板放在 prompts/ 目录，且以【只读挂载】接进容器。这样调提示词只需在
极空间文件管理器里编辑文本 + 重启容器，不用重建镜像。

占位符用 `{{key}}` 而不是 str.format 的 `{key}`：
提示词里会大量出现 JSON 示例（`{"type": "todo"}`），用 format 会被当占位符炸掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger

log = get_logger()

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class PromptTemplate:
    name: str
    body: str
    mtime: float


_cache: dict[str, PromptTemplate] = {}


def load(name: str, **values: object) -> str:
    """读取模板并填充占位符。

    带 mtime 缓存：改了 NAS 上的提示词文件后，重启容器即生效；
    如果不重启，下一次调用也会因为 mtime 变化而重新读取（更省事）。
    """
    template = _read(name)
    body = template.body
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        missing.append(key)
        return match.group(0)

    rendered = _PLACEHOLDER.sub(_replace, body)
    if missing:
        log.warning("提示词 %s 缺少占位符：%s", name, sorted(set(missing)))
    return rendered


def _read(name: str) -> PromptTemplate:
    path = Path(settings.prompts_dir) / f"{name}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise WorkbenchError(
            ErrorCode.INTERNAL,
            "提示词模板不可用，请检查 prompts 目录是否已挂载。",
            detail=f"prompt_missing:{name}",
        ) from exc

    cached = _cache.get(name)
    if cached is not None and cached.mtime == mtime:
        return cached

    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkbenchError(
            ErrorCode.INTERNAL,
            "提示词模板不可读。",
            detail=f"prompt_unreadable:{name}",
        ) from exc

    template = PromptTemplate(name=name, body=body, mtime=mtime)
    _cache[name] = template
    return template


def guardrail() -> str:
    """所有模型调用共用的系统提示（防注入声明）。"""
    return load("system_guardrail").strip()
