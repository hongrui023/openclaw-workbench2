"""任务状态路由。

轮询响应刻意保持极小（约 150 字节），因为外网穿透是按流量计费的，
而轮询是这个应用里唯一的高频请求。字段只含计数字段与一句短文案。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.obs import get_logger
from app.security.auth import require_session
from app.services.jobs import manager

log = get_logger()
router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_session)])


@router.get("/recent")
async def recent(limit: int = Query(10, ge=1, le=50)) -> dict[str, object]:
    return {"jobs": [job.to_poll() for job in manager.recent(limit)]}


@router.get("/{job_id}")
async def detail(job_id: str, full: int = Query(0, ge=0, le=1)) -> dict[str, object]:
    job = manager.get(job_id)
    # full=1 时才带上结果明细（批量清单可能稍大），轮询默认不带
    return job.to_detail() if full else job.to_poll()
