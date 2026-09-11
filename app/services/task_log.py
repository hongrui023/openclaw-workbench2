"""任务日志：life_notes/workbench_log.md

纪律（对应方案文档第 8 节）：
  - **只记事实，不记正文**。绝不把论文内容复制一份进来——那既浪费空间，
    也让"生活记录目录"意外变成了文献的第二个副本。
  - 纯追加，不重写文件。
  - 写日志失败不能影响主流程（比如目录只读），因此这里吞掉异常只记容器日志。
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from app.errors import WorkbenchError
from app.obs import get_logger, now
from app.security.paths import ROOT_LIFE_NOTES, guard

log = get_logger()

LOG_FILENAME = "workbench_log.md"
_HEADER = "# 工作台任务日志\n\n> 由 openclaw-workbench 自动追加。只记录任务事实，不记录文献正文。\n"

# 单进程单 worker，用进程内锁即可；不引入文件锁依赖（Windows 上 fcntl 不可用）
_lock = asyncio.Lock()


async def append_log(kind: str, lines: Iterable[str]) -> bool:
    """追加一条任务日志。返回是否成功（失败不抛异常）。"""
    body = "\n".join(f"- {line}" for line in lines if line)
    block = f"\n## {now().strftime('%Y-%m-%d %H:%M:%S')} · {kind}\n{body}\n"

    async with _lock:
        try:
            path = guard.write_path(ROOT_LIFE_NOTES, LOG_FILENAME)
            guard.create_text_if_absent(path, _HEADER)
            guard.append_text(path, block)
            return True
        except WorkbenchError as exc:
            log.warning("写任务日志失败：code=%s detail=%s", exc.code.value, exc.detail or "-")
            return False
        except Exception as exc:  # 日志永远不能拖垮业务
            log.warning("写任务日志异常：%s", type(exc).__name__)
            return False
