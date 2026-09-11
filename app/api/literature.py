"""文献分析路由。

关键设计：**目标解析在提交前同步完成**，而不是丢进后台任务里再失败。
这样"文件名打错"能立刻得到反馈，而不是等半分钟后在任务状态里看到一个错误。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Query

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.models import AnalyzeRequest
from app.obs import get_logger
from app.security.auth import require_session
from app.security.paths import ROOT_LITERATURE, guard
from app.services import literature, task_log
from app.services.jobs import Job, manager
from app.services.openclaw import client as openclaw_client

log = get_logger()
router = APIRouter(
    prefix="/api/literature",
    tags=["literature"],
    dependencies=[Depends(require_session)],
)


@router.get("/files")
async def list_files() -> dict[str, object]:
    """列出 literature 下的 PDF。只在用户打开页面/点击刷新时调用，不做任何后台扫描。"""
    entries = guard.list_literature()
    return {
        "total": len(entries),
        "pending": sum(1 for e in entries if not e.has_markdown),
        "files": [
            {
                "name": entry.name,
                "size_kb": round(entry.size_bytes / 1024, 1),
                "mtime": entry.mtime,
                "has_markdown": entry.has_markdown,
            }
            for entry in entries
        ],
    }


@router.post("/analyze")
async def analyze(payload: AnalyzeRequest) -> dict[str, object]:
    # 1) 目标解析（同步，立刻给出明确错误）
    targets = literature.resolve_targets(payload.scope, payload.files)

    # 2) 资源闸门：4GB 内存 + OpenClaw 也在同一台机器上，串行是硬约束
    if manager.has_active("literature"):
        raise WorkbenchError(ErrorCode.BUSY)

    if payload.scope == "all":
        todo = [name for name in targets if not guard.exists(ROOT_LITERATURE, f"{os.path.splitext(name)[0]}.md")]
        if todo and not payload.force:
            targets = todo
        if not targets:
            return {
                "ok": True,
                "nothing_to_do": True,
                "message": "literature 中的 PDF 都已生成对应 Markdown，无需重复分析。"
                "如需重做，请勾选「强制重新分析」。",
            }

    force = bool(payload.force)
    scope_label = "全部 PDF" if payload.scope == "all" else f"{len(targets)} 篇指定文献"

    async def runner(job: Job) -> dict[str, object]:
        def report(**kwargs: object) -> None:
            manager.update(job, **kwargs)  # type: ignore[arg-type]

        summary = await literature.analyze_batch(
            targets, force=force, client=openclaw_client, report=report
        )

        lines = [
            f"模式：{'批量（全部）' if payload.scope == 'all' else '批量（指定）'} · 共 {summary.total} 篇",
            f"目标：{'、'.join(targets[:8])}{'…' if len(targets) > 8 else ''}",
            f"结果：{summary.headline()}",
            f"AI 调用：{summary.calls} 次",
            f"耗时：{summary.elapsed_label}",
        ]
        if summary.modes:
            unique = sorted(set(summary.modes))
            lines.insert(3, f"分析方式：{'；'.join(unique[:3])}")
        if summary.missing:
            lines.append(f"缺失分块：{'；'.join(summary.missing[:5])}")
        if summary.failed:
            lines.append(
                "失败明细：" + "；".join(f"{item['file']} — {item['message']}" for item in summary.failed[:5])
            )
        await task_log.append_log("文献分析（批量）", lines)

        return summary.to_dict()

    job = await manager.submit("literature", scope_label, runner)
    return {"ok": True, "job_id": job.id, "targets": targets, "count": len(targets)}


@router.get("/content")
async def read_markdown(name: str = Query(..., min_length=1, max_length=300)) -> dict[str, object]:
    """读取一份已生成的分析结果。

    这是工作台里唯一"把文件内容送到浏览器"的接口，因此限制很严：
      - 只允许 .md（路径守卫的后缀白名单）
      - 只允许 literature 目录内
      - 有长度上限，超出截断并标注
    """
    path = guard.read_path(ROOT_LITERATURE, name)
    text, truncated = guard.read_text(path, max_chars=settings.markdown_view_max_chars)
    return {
        "name": name,
        "content": text,
        "chars": len(text),
        "truncated": truncated,
    }
