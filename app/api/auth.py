"""认证路由。

登录接口本身也强制要求自定义请求头（防登录 CSRF）。
副作用是 curl 调试时必须带上 `-H 'X-Requested-With: owb'`——这是有意的。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.models import LoginRequest
from app.obs import get_logger
from app.security.auth import COOKIE_NAME, CSRF_HEADER, CSRF_VALUE, auth, require_session

log = get_logger()
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise WorkbenchError(ErrorCode.FORBIDDEN)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    _require_csrf_header(request)

    problem = auth.config_problem()
    if problem and not settings.dev_mode:
        log.error("登录被拒绝：认证未配置（%s）", problem)
        raise WorkbenchError(
            ErrorCode.NOT_CONFIGURED,
            "工作台尚未配置登录口令，无法登录。请设置 WORKBENCH_PASSWORD_HASH 后重启容器。",
        )

    locked = auth.lock_remaining()
    if locked > 0:
        minutes = max(1, locked // 60)
        raise WorkbenchError(
            ErrorCode.LOGIN_LOCKED,
            f"登录失败次数过多，已锁定，请在约 {minutes} 分钟后再试。",
        )

    if not auth.verify_password(payload.password):
        await auth.note_login_failure()
        log.warning("登录失败（口令不正确）")
        raise WorkbenchError(ErrorCode.LOGIN_FAILED, status=401)

    await auth.note_login_success()
    token, max_age = await auth.create_session()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,      # JS 拿不到，XSS 也偷不走
        samesite="strict",  # 跨站请求不带 Cookie，配合自定义头双重防 CSRF
        path="/",
        # 注意：外网入口目前是 HTTP，因此不能设 secure=True，否则浏览器不会回传 Cookie。
        # 这一点在 docs/SECURITY.md 5.7 节有说明和缓解措施。
        secure=False,
    )
    log.info("登录成功，已签发会话")
    return {"ok": True, "session_days": settings.session_days}


@router.post("/logout")
async def logout(request: Request, response: Response, token: str = Depends(require_session)) -> dict[str, object]:
    await auth.revoke(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    log.info("已注销会话")
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict[str, object]:
    """供登录页判断要不要显示登录框。未登录时只返回最小信息。"""
    token = request.cookies.get(COOKIE_NAME)
    authenticated = auth.validate(token)
    payload: dict[str, object] = {"authenticated": authenticated}
    if authenticated:
        payload["ai_configured"] = bool(settings.openclaw_token or settings.openclaw_agent_id)
        payload["model"] = settings.openclaw_model
        payload["poll_seconds"] = settings.ai_poll_hint_seconds
    else:
        payload["auth_configured"] = bool(auth.config_problem() == "")
    return payload
