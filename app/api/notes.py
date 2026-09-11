"""生活记录路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models import NoteRequest
from app.obs import get_logger
from app.security.auth import require_session
from app.services import notes, task_log
from app.services.openclaw import client as openclaw_client

log = get_logger()
router = APIRouter(prefix="/api/notes", tags=["notes"], dependencies=[Depends(require_session)])


@router.post("")
async def create(payload: NoteRequest) -> dict[str, object]:
    result = await notes.add_note(payload.text, client=openclaw_client)

    await task_log.append_log(
        "生活记录",
        [
            f"类型：{result.label}",
            f"判定方式：{'规则' if result.method == 'rule' else ('模型' if result.method == 'model' else '兜底归为随笔')}",
            f"内容摘要：{result.summary[:80]}",
            f"写入：life_notes/{result.filename}",
        ],
    )
    return {"ok": True, **result.to_dict()}


@router.get("/recent")
async def recent(limit_chars: int = 6000) -> dict[str, object]:
    text, truncated = notes.read_recent("daily_notes.md", max(500, min(limit_chars, 20000)))
    return {"content": text, "truncated": truncated}


@router.get("/log")
async def task_log_recent(limit_chars: int = 6000) -> dict[str, object]:
    text, truncated = notes.read_recent("workbench_log.md", max(500, min(limit_chars, 20000)))
    return {"content": text, "truncated": truncated}
