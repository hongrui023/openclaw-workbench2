"""★ OpenClaw 调用适配层 —— 全项目唯一与 AI 服务通信的模块。

为什么要做成独立适配层（而不是直接在业务代码里发 HTTP）：

1. **唯一出口**：想审计"工作台到底给 AI 发了什么"，只看这一个文件。
2. **唯一持有 Token 的地方**。Token 只出现在这里的请求头里，永远不进入
   任何 HTTP 响应、任何日志、任何前端代码。
3. **只收纯文本、只发纯文本**。这个适配层的接口签名就限制死了这一点：
   入参是 str，返回值是 str。它拿不到文件路径，也无从读写文件。
   所以即使 PDF 里藏着提示词注入，注入的收益上限只是"分析结果不准"，
   而不可能是"读到 literature 之外的文件"。
4. **可替换**：日后 OpenClaw 换成任何 OpenAI 兼容服务（vLLM、Ollama、
   LiteLLM…），只改这一个文件。

接口约定：POST {OPENCLAW_BASE_URL}/chat/completions
请求体遵循 OpenAI Chat Completions 规范。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger
from app.security.sanitize import scrub

log = get_logger()

# 网络类错误才重试。4xx/5xx 一律不重试——
# 配置错、Token 错、端点没开，重试一万次也是同样的结果，
# 只会白等时间、白烧配额。
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)

_CODE_FENCE = re.compile(r"^\s*```(?:json|markdown|md)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


@dataclass
class ProbeResult:
    """连通性自检结果。只给本地诊断脚本用，绝不进任何 API 响应。"""

    ok: bool
    stage: str
    detail: str
    hint: str = ""

    def render(self) -> str:
        lines = [f"[{'OK' if self.ok else '失败'}] {self.stage}: {self.detail}"]
        if self.hint:
            lines.append(f"       → {self.hint}")
        return "\n".join(lines)


class OpenClawClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self.call_count = 0  # 供任务日志统计"本次分析调用了几次模型"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            settings.openclaw_timeout_seconds,
                            connect=10.0,
                        ),
                        # 不跟随重定向：跟随会让请求跑到意料之外的主机上
                        follow_redirects=False,
                        headers={"User-Agent": "openclaw-workbench/1.0"},
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def reset_counter(self) -> None:
        self.call_count = 0

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------
    def _build_payload(self, messages: list[dict[str, str]], *, temperature: float | None, max_tokens: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": settings.openclaw_model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.openclaw_token:
            headers["Authorization"] = f"Bearer {settings.openclaw_token}"
        if settings.openclaw_agent_id:
            headers["x-openclaw-agent-id"] = settings.openclaw_agent_id
        return headers

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """一次纯文本对话。返回模型输出的纯文本。

        异常统一转成 WorkbenchError，用户可见文案全部来自预定义表，
        绝不透传 httpx 的异常字符串（那里面会带内网地址）。
        """
        if not settings.openclaw_token and not settings.openclaw_agent_id:
            # 没配 Token 时不去发请求——否则会得到一堆 401，噪音大于信息量
            log.error("OPENCLAW_TOKEN 未配置，拒绝发起调用")
            raise WorkbenchError(ErrorCode.NOT_CONFIGURED, "AI 服务未配置（缺少访问凭据）。")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload = self._build_payload(messages, temperature=temperature, max_tokens=max_tokens)

        last_error: Exception | None = None
        attempts = settings.openclaw_max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                text = await self._post_once(payload)
            except _RETRYABLE as exc:
                last_error = exc
                log.warning(
                    "OpenClaw 调用网络异常（第 %d/%d 次）：%s",
                    attempt,
                    attempts,
                    scrub(f"{type(exc).__name__} {exc}"),
                )
                if attempt < attempts:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                raise WorkbenchError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    detail=f"network:{type(exc).__name__}",
                ) from exc
            else:
                self.call_count += 1
                return text

        raise WorkbenchError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            detail=f"exhausted:{type(last_error).__name__ if last_error else 'unknown'}",
        )

    async def _post_once(self, payload: dict[str, Any]) -> str:
        # 每块输入可能很大（48000 字符 + 提示词），超时按配置给足
        timeout = httpx.Timeout(settings.openclaw_timeout_seconds, connect=10.0)
        try:
            client = await self._get_client()
            response = await client.post(
                settings.openclaw_chat_url,
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise WorkbenchError(ErrorCode.UPSTREAM_TIMEOUT, detail=f"timeout:{type(exc).__name__}") from exc

        if response.status_code >= 400:
            self._log_http_failure(response.status_code, response.text)
            if response.status_code in (401, 403):
                raise WorkbenchError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    detail=f"auth:{response.status_code}",
                )
            if response.status_code == 404:
                raise WorkbenchError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    detail="endpoint_not_found",
                )
            if response.status_code == 429:
                raise WorkbenchError(ErrorCode.UPSTREAM_UNAVAILABLE, detail="rate_limited")
            raise WorkbenchError(
                ErrorCode.UPSTREAM_UNAVAILABLE, detail=f"http:{response.status_code}"
            )

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            log.error(
                "OpenClaw 返回的不是 JSON，顶层内容片段：%s",
                scrub(response.text[:300]),
            )
            raise WorkbenchError(ErrorCode.MODEL_OUTPUT_INVALID, detail="non_json") from exc

        content = self._extract_content(body)
        if not content:
            log.error("OpenClaw 返回内容为空，响应顶层键：%s", sorted(body.keys()) if isinstance(body, dict) else type(body).__name__)
            raise WorkbenchError(ErrorCode.MODEL_OUTPUT_INVALID, detail="empty_content")
        return content

    @staticmethod
    def _extract_content(body: Any) -> str:
        """兼容 OpenAI 规范的几种形态，避免因为响应结构差异整个流程挂掉。"""
        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"].strip()
                if isinstance(first.get("text"), str):
                    return first["text"].strip()
        # 兼容少数实现直接给 output_text / content
        for key in ("output_text", "content", "text", "response"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _log_http_failure(self, status: int, text: str) -> None:
        """把失败原因写进容器日志，并附上可执行的排查提示。

        用户看到的仍然是 errors.py 里的通用文案——这里的信息只进日志。
        """
        snippet = scrub(text[:400])
        hints = {
            401: "Token 不正确或已失效。检查 OPENCLAW_TOKEN。",
            403: "Token 权限不足，或被 OpenClaw 的访问策略拒绝。",
            404: "路径不对，或 OpenClaw 未启用 chatCompletions 端点。见 docs/OPENCLAW-API.md。",
            405: "方法不被允许，说明该路径存在但不是 chat/completions。检查 base_url 是否多了/少了 /v1。",
            400: "请求被拒绝，通常是 model 字段不被 OpenClaw 接受。检查 OPENCLAW_MODEL。",
            429: "被限流。稍后重试，或降低并发（V1 本身是串行调用）。",
            500: "OpenClaw 内部错误，检查其自身日志与其后端模型（DeepSeek）的连通性。",
            502: "OpenClaw 无法连到其后端模型服务的上游。",
            503: "OpenClaw 或其上游模型服务不可用。",
        }
        log.error("OpenClaw HTTP %d：%s", status, snippet)
        hint = hints.get(status)
        if hint:
            log.error("排查提示：%s", hint)

    # ------------------------------------------------------------------
    # JSON 模式（生活记录分类用）
    # ------------------------------------------------------------------
    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        text = await self.chat(system, user, temperature=0.0, max_tokens=500)
        data = extract_json_object(text)
        if data is None:
            log.warning("模型 JSON 解析失败，原文片段：%s", scrub(text[:200]))
            raise WorkbenchError(ErrorCode.MODEL_OUTPUT_INVALID, detail="json_parse")
        return data

    # ------------------------------------------------------------------
    # 连通性自检（供 scripts/check_openclaw.py 使用）
    # ------------------------------------------------------------------
    async def probe(self) -> ProbeResult:
        if not settings.openclaw_token and not settings.openclaw_agent_id:
            return ProbeResult(
                ok=False,
                stage="配置检查",
                detail="OPENCLAW_TOKEN 为空",
                hint="填入 OpenClaw Gateway 的访问令牌后重试。",
            )

        payload = self._build_payload(
            [
                {"role": "system", "content": "你是一个连通性测试端点，只回答指定字符。"},
                {"role": "user", "content": "请只回复两个字：正常"},
            ],
            temperature=0.0,
            max_tokens=16,
        )
        try:
            content = await self._post_once(payload)
        except WorkbenchError as exc:
            return ProbeResult(
                ok=False,
                stage="调用 /chat/completions",
                detail=f"{exc.code.value}（{exc.detail or '无细节'}）",
                hint=_probe_hint(exc),
            )
        except httpx.HTTPError as exc:
            return ProbeResult(
                ok=False,
                stage="网络连接",
                detail=scrub(f"{type(exc).__name__}"),
                hint=(
                    "网络不通。注意两点：① 容器里的 127.0.0.1 指的是容器自己，不是 NAS；"
                    "② 端口要填宿主机映射出来的那个，不是容器内部端口。"
                    "OPENCLAW_BASE_URL 形如 http://<NAS局域网IP>:<宿主机映射端口>/v1"
                ),
            )
        return ProbeResult(ok=True, stage="调用 /chat/completions", detail=f"模型已返回：{scrub(content)}")


def _probe_hint(exc: WorkbenchError) -> str:
    detail = exc.detail or ""
    if detail.startswith("auth"):
        return "Token 不正确。回到极空间 → 容器 appstore_openclaw → 环境，核对 Gateway token。"
    if detail == "endpoint_not_found":
        return (
            "路径 404：OpenClaw 很可能没有启用 chatCompletions 端点。"
            "需要在 OpenClaw 配置里开启 gateway.http.endpoints.chatCompletions.enabled（见 docs/OPENCLAW-API.md）。"
        )
    if "timeout" in detail:
        return "调用超时。若后端模型较慢，调大 OPENCLAW_TIMEOUT_SECONDS。"
    if detail.startswith("http:400"):
        return "请求被拒。多半是 OPENCLAW_MODEL 不被接受，改为 OpenClaw 中实际存在的 agent 标识。"
    if detail.startswith("http:405"):
        return "HTTP 405：路径存在但方法不对。检查 OPENCLAW_BASE_URL 结尾的 /v1。"
    if detail.startswith("http:5"):
        return "OpenClaw 或其后端模型（DeepSeek）侧的问题。检查 OpenClaw 容器日志。"
    return "检查 OPENCLAW_BASE_URL / OPENCLAW_TOKEN / OPENCLAW_MODEL 三项配置。"


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里稳妥地取出一个 JSON 对象。

    模型经常给 ```json ... ``` 包裹，或者前后带一句解释。
    策略：先剥代码块，再尝试直接解析，最后退化为"取第一个大括号配对区间"。
    """
    if not text:
        return None
    candidate = text.strip()

    fence = _CODE_FENCE.match(candidate)
    if fence:
        candidate = fence.group(1).strip()

    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    start = candidate.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(candidate[start : index + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
                    return data if isinstance(data, dict) else None
        start = candidate.find("{", start + 1)
    return None


# 全局单例。业务层通过它调用 AI。
client = OpenClawClient()
