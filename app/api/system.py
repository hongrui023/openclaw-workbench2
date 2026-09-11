"""系统路由。

/api/health 无需认证，但**只返回一个布尔值**——不含版本、不含配置状态、
不含任何内部信息。它唯一的用途是给 Docker HEALTHCHECK 和穿透网关探活用。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
