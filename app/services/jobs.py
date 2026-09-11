"""进程内异步任务队列（单 worker）。

为什么不用 Redis / Celery：一台 4GB 的 NAS，为了"看见一个进度条"引入消息队列和
第二个容器，是明显的过度设计。这里用一个 asyncio.Queue + 一个 worker 就够，
而且天然满足"串行执行、同一时刻只跑一个重任务"的资源约束。

为什么状态存内存而不是落盘：
  - 分析产物（.md 文件）本身就是"已完成"的事实记录，重启后靠"同名 .md 是否存在"
    就能判断，不需要额外的持久化层
  - 进程重启后未完成的任务标记为已中断，用户重新点一次即可（已生成的会被跳过）
  - 数据目录里永远只有用户的记录，没有工作台的内部状态文件

内存保护：只保留最近 N 条；超出部分从内存淘汰（不写盘、不删除任何文件）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger, now

log = get_logger()

MAX_HISTORY = 50
MAX_QUEUE = 4

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


@dataclass
class Job:
    id: str
    kind: str                  # literature | notes
    title: str
    status: str = STATUS_QUEUED
    stage: str = "排队中"
    done: int = 0
    total: int = 0
    failed: int = 0
    message: str = ""
    current: str = ""          # 批量处理时，当前正在处理的文件名
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    _runner: Callable[["Job"], Awaitable[dict[str, Any]]] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now().strftime("%Y-%m-%d %H:%M:%S")

    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        try:
            start = datetime.strptime(self.started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now().tzinfo)
        except ValueError:
            return 0.0
        end = now() if not self.finished_at else self._parse(self.finished_at)
        return max(0.0, (end - start).total_seconds())

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now().tzinfo)

    @property
    def active(self) -> bool:
        return self.status in (STATUS_QUEUED, STATUS_RUNNING)

    def to_poll(self) -> dict[str, Any]:
        """轮询响应。字段刻意保持精简——每 30 秒一次请求，能被 gzip 压到 150 字节级。"""
        payload = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "failed": self.failed,
            "current": self.current,
            "elapsed": int(self.elapsed_seconds()),
        }
        if self.message:
            payload["message"] = self.message
        if self.error:
            payload["error"] = self.error
        if self.status in (STATUS_SUCCEEDED, STATUS_FAILED):
            payload["finished_at"] = self.finished_at
        return payload

    def to_detail(self) -> dict[str, Any]:
        payload = self.to_poll()
        payload["title"] = self.title
        payload["created_at"] = self.created_at
        payload["started_at"] = self.started_at
        if self.result is not None:
            payload["result"] = self.result
        return payload


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=MAX_HISTORY)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping = False
            self._worker = asyncio.create_task(self._run_forever(), name="owb-job-worker")
            log.info("任务队列已启动（单 worker，串行执行）")

    async def stop(self) -> None:
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

    # ------------------------------------------------------------------
    # 提交与查询
    # ------------------------------------------------------------------
    def has_active(self, kind: str | None = None) -> bool:
        for job in self._jobs.values():
            if not job.active:
                continue
            if kind is None or job.kind == kind:
                return True
        return False

    async def submit(
        self,
        kind: str,
        title: str,
        runner: Callable[[Job], Awaitable[dict[str, Any]]],
    ) -> Job:
        if self._queue.full():
            raise WorkbenchError(ErrorCode.BUSY, "任务排队已满，请稍后再提交。")

        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title, _runner=runner)
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._evict()
        await self._queue.put(job.id)
        log.info("任务已入队：%s kind=%s title=%s", job.id, kind, title)
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise WorkbenchError(ErrorCode.JOB_NOT_FOUND)
        return job

    def recent(self, limit: int = 20) -> list[Job]:
        ids = list(self._order)[::-1][: max(1, min(limit, MAX_HISTORY))]
        return [self._jobs[i] for i in ids if i in self._jobs]

    def _evict(self) -> None:
        # deque 的 maxlen 会自动丢掉最旧的 id；这里同步清掉对应的 Job 对象
        live = set(self._order)
        for job_id in list(self._jobs.keys()):
            if job_id not in live:
                self._jobs.pop(job_id, None)

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    async def _run_forever(self) -> None:
        while not self._stopping:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue
            await self._execute(job)
            self._queue.task_done()

    async def _execute(self, job: Job) -> None:
        job.status = STATUS_RUNNING
        job.stage = "开始"
        job.started_at = now().strftime("%Y-%m-%d %H:%M:%S")
        started = asyncio.get_event_loop().time()
        runner = job._runner
        job._runner = None

        try:
            if runner is None:
                raise WorkbenchError(ErrorCode.INTERNAL, detail="runner_missing")
            result = await runner(job)
            job.result = result or {}
            job.status = STATUS_SUCCEEDED
            job.stage = "已完成"
        except WorkbenchError as exc:
            job.status = STATUS_FAILED
            job.stage = "已失败"
            job.error = {"code": exc.code.value, "message": exc.message}
            log.warning("任务失败：%s code=%s detail=%s", job.id, exc.code.value, exc.detail or "-")
        except asyncio.CancelledError:
            job.status = STATUS_FAILED
            job.stage = "已中断"
            job.error = {"code": "interrupted", "message": "任务被中断（服务可能正在重启），请重新提交。"}
            raise
        except Exception as exc:  # 兜底：任何未预料的异常都不能让 worker 死掉
            job.status = STATUS_FAILED
            job.stage = "已失败"
            # 用户看到的永远是通用文案；原始异常只进容器日志
            job.error = {"code": ErrorCode.INTERNAL.value, "message": "工作台内部错误，已在容器日志中记录。"}
            log.exception("任务异常：%s err=%s", job.id, type(exc).__name__)
        finally:
            job.finished_at = now().strftime("%Y-%m-%d %H:%M:%S")
            job.current = ""
            log.info(
                "任务结束：%s status=%s 耗时=%.1fs",
                job.id,
                job.status,
                asyncio.get_event_loop().time() - started,
            )

    # ------------------------------------------------------------------
    # 供业务层报告进度
    # ------------------------------------------------------------------
    @staticmethod
    def update(
        job: Job,
        *,
        stage: str | None = None,
        done: int | None = None,
        total: int | None = None,
        failed: int | None = None,
        message: str | None = None,
        current: str | None = None,
    ) -> None:
        if stage is not None:
            job.stage = stage
        if done is not None:
            job.done = done
        if total is not None:
            job.total = total
        if failed is not None:
            job.failed = failed
        if message is not None:
            job.message = message
        if current is not None:
            job.current = current


manager = JobManager()
