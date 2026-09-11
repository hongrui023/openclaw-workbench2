"""认证：口令校验、会话签发、登录限速。

对应方案文档第 5.3 节（第 2 层防线）。

三个刻意的选择：

1. **用标准库 scrypt，不引入 passlib/bcrypt**。零新依赖，ARM64 上无编译风险。
2. **会话存进程内存**，不落盘。容器重启后需要重新登录——这是可接受的代价，
   换来的是"会话不写在 NAS 上"（数据目录里永远只有你自己的记录）。
3. **限速不按 IP**。经节点小宝穿透后，所有请求的来源 IP 很可能被改写成同一个，
   按 IP 限速要么失效要么误伤自己。改为全局失败计数 + 指数退避。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger

log = get_logger()

COOKIE_NAME = "owb_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "owb"

# scrypt 参数。n=2^14 在 4 核 ARM 上单次约 40–80ms，
# 对正常登录无感，对暴力破解是有效的成本墙。
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# 限速阈值
_FAIL_DELAY_AFTER = 3      # 连续失败 3 次后开始强制延迟
_FAIL_DELAY_SECONDS = 2.0
_FAIL_LOCK_AFTER = 10      # 连续失败 10 次后锁定


def make_password_hash(password: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM
    )
    return "$".join(
        [
            "scrypt",
            str(n),
            str(r),
            str(p),
            base64.b64encode(salt).decode(),
            base64.b64encode(dk).decode(),
        ]
    )


def verify_password_hash(stored: str, password: str) -> bool:
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$", 5)
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass
class _Session:
    token: str
    created_at: float
    expires_at: float


@dataclass
class _ThrottleState:
    consecutive_failures: int = 0
    locked_until: float = 0.0
    last_attempt: float = field(default=0.0)


class AuthManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        self._throttle = _ThrottleState()
        self.password_hash = settings.password_hash
        if settings.dev_mode and settings.dev_password:
            # 本地开发便利：每次启动重新生成哈希，口令仍是环境变量里的那个
            self.password_hash = make_password_hash(settings.dev_password)

    # ---- 启动自检 ----
    def config_problem(self) -> str:
        ok, reason = settings.auth_ready()
        if ok:
            return ""
        return reason

    # ---- 口令 ----
    def verify_password(self, password: str) -> bool:
        if not password or not self.password_hash:
            return False
        return verify_password_hash(self.password_hash, password)

    # ---- 登录限速（全局，不按 IP）----
    def lock_remaining(self) -> int:
        remaining = self._throttle.locked_until - time.time()
        return int(remaining) if remaining > 0 else 0

    async def note_login_failure(self) -> None:
        async with self._lock:
            state = self._throttle
            state.consecutive_failures += 1
            state.last_attempt = time.time()
            if state.consecutive_failures >= _FAIL_LOCK_AFTER:
                state.locked_until = time.time() + settings.lockout_minutes * 60
                log.warning(
                    "连续登录失败 %d 次，已锁定 %d 分钟",
                    state.consecutive_failures,
                    settings.lockout_minutes,
                )
            elif state.consecutive_failures >= _FAIL_DELAY_AFTER:
                # 强制延迟：直接 await，让绕口令的尝试成本线性上升
                await asyncio.sleep(_FAIL_DELAY_SECONDS)

    async def note_login_success(self) -> None:
        async with self._lock:
            self._throttle = _ThrottleState()

    # ---- 会话 ----
    async def create_session(self) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)  # 256 位
        now = time.time()
        expires = now + settings.session_days * 86400
        async with self._lock:
            self._sessions[token] = _Session(token=token, created_at=now, expires_at=expires)
            self._gc_locked(now)
        return token, settings.session_days * 86400

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        session = self._sessions.get(token)
        if session is None:
            return False
        now = time.time()
        if session.expires_at <= now:
            self._sessions.pop(token, None)
            return False
        return True

    async def revoke(self, token: str | None) -> None:
        if not token:
            return
        async with self._lock:
            self._sessions.pop(token, None)

    def _gc_locked(self, now: float) -> None:
        expired = [t for t, s in self._sessions.items() if s.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
        # 防御性上限：单用户场景不该有几百个会话，有则说明异常
        if len(self._sessions) > 50:
            ordered = sorted(self._sessions.values(), key=lambda s: s.created_at)
            for session in ordered[: len(self._sessions) - 50]:
                self._sessions.pop(session.token, None)


auth = AuthManager()


async def require_session(request: Request) -> str:
    """所有业务路由的统一依赖。

    刻意做成依赖而不是中间件：FastAPI 的依赖注入会出现在路由签名里，
    新建路由时"忘了保护"的可能性更低（漏了就没有 request 参数，一眼可见）。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not auth.validate(token):
        raise WorkbenchError(ErrorCode.UNAUTHORIZED)

    # CSRF：写操作必须带自定义头。
    # 跨站表单提交无法设置自定义头，因此这一条能挡掉 CSRF。
    if request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
            raise WorkbenchError(ErrorCode.FORBIDDEN)

    return token  # type: ignore[return-value]
