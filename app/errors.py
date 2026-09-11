"""错误模型。

设计原则（严格贯彻"不编造、不泄露"）：

1. 每个错误码对应一句【预定义的用户可读文案】——用户看到的文案永远不来自异常
   的字符串转义，因此不可能带出内网地址、端口、Token 片段或堆栈。
2. 原始异常只写容器日志（且经过脱敏），不进入 HTTP 响应。
3. 每个错误码带 http_status，路由层无需各自判断。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # 通用
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INTERNAL = "internal"
    NOT_CONFIGURED = "not_configured"

    # 路径守卫
    PATH_FORBIDDEN = "path_forbidden"
    PATH_BAD_NAME = "path_bad_name"
    PATH_BAD_SUFFIX = "path_bad_suffix"

    # PDF 解析
    PDF_NOT_FOUND = "pdf_not_found"
    PDF_ENCRYPTED = "pdf_encrypted"
    PDF_NO_TEXT = "pdf_no_text"
    PDF_CORRUPT = "pdf_corrupt"
    PDF_TOO_LARGE = "pdf_too_large"
    PDF_TOO_MANY_PAGES = "pdf_too_many_pages"
    PDF_TOO_FRAGMENTED = "pdf_too_fragmented"

    # AI 服务
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    ANALYSIS_FAILED = "analysis_failed"

    # 任务
    BUSY = "busy"
    JOB_NOT_FOUND = "job_not_found"

    # 认证
    LOGIN_LOCKED = "login_locked"
    LOGIN_FAILED = "login_failed"


# 用户可见文案。任何一条都必须满足：
#   - 说清发生了什么
#   - 说清用户能做什么
#   - 不含内网地址、端口、Token、堆栈
_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.BAD_REQUEST: "请求内容不正确，请检查后重试。",
    ErrorCode.UNAUTHORIZED: "登录已过期，请重新登录。",
    ErrorCode.FORBIDDEN: "该请求被拒绝。",
    ErrorCode.NOT_FOUND: "未找到请求的资源。",
    ErrorCode.INTERNAL: "工作台内部错误，已记录日志。请稍后重试。",
    ErrorCode.NOT_CONFIGURED: "工作台尚未完成配置，无法执行该操作。",

    ErrorCode.PATH_FORBIDDEN: "只允许访问 literature 和 life_notes 目录，该路径已被拒绝。",
    ErrorCode.PATH_BAD_NAME: "文件名不合法，请只使用普通文件名（不要带路径、不要带特殊字符）。",
    ErrorCode.PATH_BAD_SUFFIX: "文件类型不被支持。",

    ErrorCode.PDF_NOT_FOUND: "未在 literature/ 中找到该 PDF，请确认文件名（区分大小写）。",
    ErrorCode.PDF_ENCRYPTED: "该 PDF 受密码保护，无法解析。请先去除密码后重试。",
    ErrorCode.PDF_NO_TEXT: "该 PDF 没有可提取的文本层，疑似纯扫描件。V1 不支持 OCR，已跳过。",
    ErrorCode.PDF_CORRUPT: "该 PDF 文件结构损坏，无法打开。",
    ErrorCode.PDF_TOO_LARGE: "该 PDF 超出处理上限，已跳过。",
    ErrorCode.PDF_TOO_MANY_PAGES: "该 PDF 页数超出处理上限，已跳过。",
    ErrorCode.PDF_TOO_FRAGMENTED: "该文档体积异常（预计分块数超过上限），V1 不处理此类文档。",

    ErrorCode.UPSTREAM_UNAVAILABLE: "AI 服务暂时不可用，未生成 Markdown。请稍后重试。",
    ErrorCode.UPSTREAM_TIMEOUT: "AI 服务响应超时，未生成 Markdown。请稍后重试。",
    ErrorCode.MODEL_OUTPUT_INVALID: "模型返回内容不完整，已放弃写入，避免生成错误文件。",
    ErrorCode.ANALYSIS_FAILED: "本次分析未能完成，未生成 Markdown。",

    ErrorCode.BUSY: "已有分析任务正在进行，请等它结束后再提交。",
    ErrorCode.JOB_NOT_FOUND: "任务不存在或已被清理。",

    ErrorCode.LOGIN_LOCKED: "登录失败次数过多，已暂时锁定，请稍后再试。",
    ErrorCode.LOGIN_FAILED: "口令不正确。",
}


class WorkbenchError(Exception):
    """业务错误。message 若留空则使用预定义文案。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        status: int | None = None,
        *,
        detail: str = "",
        **extra: object,
    ) -> None:
        self.code = code
        self.message = message or _MESSAGES.get(code, "操作失败。")
        self.status = status or _default_status(code)
        self.detail = detail          # 仅写日志，绝不进响应
        self.extra = extra            # 附加的结构化信息（如失败清单），由路由决定是否返回
        super().__init__(f"{code.value}: {self.detail or self.message}")


def _default_status(code: ErrorCode) -> int:
    if code is ErrorCode.UNAUTHORIZED:
        return 401
    if code is ErrorCode.FORBIDDEN:
        return 403
    if code is ErrorCode.NOT_FOUND or code in (ErrorCode.JOB_NOT_FOUND, ErrorCode.PDF_NOT_FOUND):
        return 404
    if code in (ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.UPSTREAM_TIMEOUT):
        return 503
    if code in (ErrorCode.BUSY, ErrorCode.LOGIN_LOCKED):
        return 429
    if code is ErrorCode.INTERNAL:
        return 500
    return 400


def message_for(code: ErrorCode) -> str:
    return _MESSAGES.get(code, "操作失败。")
