"""应用入口：中间件装配、路由挂载、异常处理。

    uvicorn app.main:app --host 0.0.0.0 --port 8080

单 worker 是硬性要求（会话表 / 任务队列 / 登录限速都在进程内存里）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app import __version__
from app.api import auth as auth_api
from app.api import jobs as jobs_api
from app.api import literature as literature_api
from app.api import notes as notes_api
from app.api import system as system_api
from app.config import settings
from app.errors import ErrorCode, WorkbenchError, message_for
from app.obs import get_logger, setup_logging
from app.security.auth import auth
from app.security.paths import ROOT_LIFE_NOTES, ROOT_LITERATURE, guard
from app.security.sanitize import scrub
from app.services.jobs import manager
from app.services.openclaw import client as openclaw_client

log = setup_logging()


def _describe_secret(secret: str) -> str:
    """只报告"配了没有、多长"，绝不打印任何字符。

    为什么不打印掩码（哪怕只露首尾几位）：
      "Token 绝不写入日志"是硬要求，不是"尽量少写"。首尾几位加上长度，
      合起来仍然是关于密钥本身的信息——容器日志一旦被带走（截图贴进聊天、
      提 issue、导出给别人排查），那就是泄露。
      而"配了没有 / 长度是多少"已经足够判断 99% 的配置错误：
      忘配、复制时被截断、尾巴上多带了引号或空格。
    """
    if not secret:
        return "未配置"
    return f"已配置（长度 {len(secret)}）"


def _log_startup() -> None:
    log.info("openclaw-workbench v%s 启动中…", __version__)

    # 这条日志刻意打印目标地址：极空间没有 SSH，容器日志是唯一能确认
    # "我填的地址对不对"的地方。但 Token 一个字符都不打印。
    log.info("AI 服务地址：%s", settings.openclaw_chat_url)
    log.info("AI 服务令牌：%s", _describe_secret(settings.openclaw_token))
    log.info("模型标识：%s", settings.openclaw_model)
    log.info("文献目录：%s", guard.root(ROOT_LITERATURE))
    log.info("记录目录：%s", guard.root(ROOT_LIFE_NOTES))
    log.info(
        "分块参数：单块上限 %d 字符 / 重叠 %d / 归并分组 %d / 块数上限 %d",
        settings.chunk_max_chars,
        settings.chunk_overlap_chars,
        settings.reduce_fan_in,
        settings.max_chunks,
    )
    if settings.dev_mode:
        log.warning("开发模式已启用（WORKBENCH_DEV=1）——不要在 NAS 上使用此模式")

    problem = auth.config_problem()
    if problem:
        log.error("★ 登录口令未配置（%s）：所有登录都会被拒绝。", problem)
        log.error("  解决：运行 python scripts/make_password_hash.py 生成哈希，填入 WORKBENCH_PASSWORD_HASH 后重启容器。")
    if not settings.openclaw_token and not settings.openclaw_agent_id:
        log.warning("★ OPENCLAW_TOKEN 未设置：文献分析功能会直接报「AI 服务未配置」。")
        log.warning("  见 docs/OPENCLAW-API.md 了解需要在 OpenClaw 侧开启什么。")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _log_startup()

    # 只确保两个根目录存在。绝不在其中创建、扫描或改动任何文件。
    guard.ensure_root(ROOT_LITERATURE)
    guard.ensure_root(ROOT_LIFE_NOTES)
    try:
        os.makedirs(settings.work_dir, exist_ok=True)
    except OSError as exc:
        log.warning("中间产物目录不可用：%s", type(exc).__name__)

    await manager.start()
    log.info("启动完成，等待请求。")
    try:
        yield
    finally:
        await manager.stop()
        await openclaw_client.aclose()
        log.info("已停止。")


app = FastAPI(
    title="openclaw-workbench",
    version=__version__,
    lifespan=lifespan,
    # 公网部署时关闭接口文档页——它是纯粹的额外信息暴露面。
    # 本地开发（WORKBENCH_DEV=1）时自动打开，方便用 /docs 调试。
    docs_url="/docs" if settings.dev_mode else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.dev_mode else None,
)

# 穿透通道按流量计费，gzip 是"减少流量"里性价比最高的一项
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # 不泄露服务端实现
    response.headers["Server"] = "workbench"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # 前端零外链：CSP 直接把"不许连外部资源"变成浏览器强制执行的事
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    # 静态资源带 ?v= 版本号，可以放心长缓存：二次访问直接命中本地缓存，
    # 一次请求都不发——穿透通道按流量计费，这是最省的一种省法。
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# ----------------------------------------------------------------------
# 异常处理：用户永远只看到预定义文案
# ----------------------------------------------------------------------
@app.exception_handler(WorkbenchError)
async def handle_workbench_error(request: Request, exc: WorkbenchError) -> JSONResponse:
    if exc.status >= 500 or exc.code in (
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.INTERNAL,
    ):
        log.warning("请求失败：%s %s → %s", request.method, request.url.path, exc.code.value)
    payload: dict[str, object] = {"ok": False, "code": exc.code.value, "message": exc.message}
    return JSONResponse(status_code=exc.status, content=payload)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 不回显 pydantic 的原始报错（它会带上字段路径与输入值）
    log.info("请求参数校验失败：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=400,
        content={"ok": False, "code": ErrorCode.BAD_REQUEST.value, "message": message_for(ErrorCode.BAD_REQUEST)},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # 原始信息只进容器日志，且经过脱敏——防止把内网地址带进响应
    log.error(
        "未处理异常：%s %s → %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        scrub(exc),
    )
    return JSONResponse(
        status_code=500,
        content={"ok": False, "code": ErrorCode.INTERNAL.value, "message": message_for(ErrorCode.INTERNAL)},
    )


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
app.include_router(system_api.router)
app.include_router(auth_api.router)
app.include_router(jobs_api.router)
app.include_router(literature_api.router)
app.include_router(notes_api.router)


# ----------------------------------------------------------------------
# 静态资源（零外链：无 CDN、无外部字体、无图标库）
# ----------------------------------------------------------------------
_INDEX = settings.static_dir / "index.html"

if settings.static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        _INDEX,
        media_type="text/html; charset=utf-8",
        # 页面本体走协商缓存（304 极省流量）；
        # CSS/JS 通过 ?v= 版本号配合长缓存，见 /static 的响应头。
        headers={"Cache-Control": "no-cache"},
    )
