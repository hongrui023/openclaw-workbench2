"""配置：全部来自环境变量。

刻意不使用 config.yaml：
  1. 少一个必须挂载的文件，部署只需 2 个挂载点（literature / life_notes）
  2. 配置与机密（Token）放在同一处，避免"模板进了 Git、真实值忘了排除"
  3. 极空间 Docker 界面本来就有"环境"页签，填环境变量最自然
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUE = {"1", "true", "yes", "on", "y"}


def _str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _int(name: str, default: int, *, low: int = 0, high: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw.strip()))
    except (TypeError, ValueError):
        return default
    if value < low:
        return low
    if high is not None and value > high:
        return high
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _resolve(path_str: str, fallback: Path) -> Path:
    """空值回退到默认；相对路径按当前工作目录解析，便于本地开发。"""
    if not path_str:
        return fallback
    return Path(path_str).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    # ---- 数据根目录（容器内固定，真实路径由挂载决定）----
    data_root: Path
    literature_dir: Path
    life_notes_dir: Path

    # ---- 应用目录 ----
    app_root: Path
    prompts_dir: Path
    work_dir: Path          # 中间产物。刻意不挂载，容器重建即清空
    static_dir: Path

    # ---- 认证 ----
    password_hash: str
    dev_mode: bool
    dev_password: str
    session_days: int
    lockout_minutes: int

    # ---- OpenClaw 适配层 ----
    openclaw_base_url: str
    openclaw_token: str
    openclaw_model: str
    openclaw_agent_id: str
    openclaw_timeout_seconds: int
    openclaw_max_retries: int

    # ---- 长文献分段 ----
    chunk_max_chars: int
    chunk_overlap_chars: int
    max_chunks: int
    reduce_fan_in: int

    # ---- 资源上限 ----
    max_pdf_mb: int
    max_pdf_pages: int
    min_chars_per_page: int
    markdown_view_max_chars: int

    # ---- 其他 ----
    timezone: str
    log_level: str
    port: int
    ai_poll_hint_seconds: int

    @property
    def direct_mode_max_chars(self) -> int:
        """≤ 一块的容量就直通，避免给短文增加无谓的多轮调用。"""
        return self.chunk_max_chars

    @property
    def openclaw_chat_url(self) -> str:
        base = self.openclaw_base_url.rstrip("/")
        # 容忍用户填 http://ip:18789（漏了 /v1）
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    @classmethod
    def from_env(cls) -> "Settings":
        app_root = _resolve(_str("WORKBENCH_APP_ROOT"), Path("/app"))
        if not app_root.exists():
            # 本地直接 python -m uvicorn 时，/app 不存在，回退到仓库根目录
            app_root = Path(__file__).resolve().parent.parent

        data_root = _resolve(_str("WORKBENCH_DATA_ROOT"), Path("/data"))
        if not data_root.exists() and not _str("WORKBENCH_DATA_ROOT"):
            data_root = app_root / "var" / "data"

        return cls(
            data_root=data_root,
            literature_dir=_resolve(_str("LITERATURE_DIR"), data_root / "literature"),
            life_notes_dir=_resolve(_str("LIFE_NOTES_DIR"), data_root / "life_notes"),
            app_root=app_root,
            prompts_dir=_resolve(_str("PROMPTS_DIR"), app_root / "prompts"),
            work_dir=_resolve(_str("WORKBENCH_WORK_DIR"), app_root / "var" / "jobs"),
            static_dir=app_root / "app" / "static",
            password_hash=_str("WORKBENCH_PASSWORD_HASH"),
            dev_mode=_bool("WORKBENCH_DEV", False),
            dev_password=_str("WORKBENCH_PASSWORD"),
            session_days=_int("SESSION_DAYS", 30, low=1, high=365),
            lockout_minutes=_int("LOGIN_LOCKOUT_MINUTES", 30, low=1, high=1440),
            openclaw_base_url=_str("OPENCLAW_BASE_URL", "http://127.0.0.1:18789/v1"),
            openclaw_token=_str("OPENCLAW_TOKEN"),
            openclaw_model=_str("OPENCLAW_MODEL", "deepseek/deepseek-v4-flash"),
            openclaw_agent_id=_str("OPENCLAW_AGENT_ID"),
            openclaw_timeout_seconds=_int("OPENCLAW_TIMEOUT_SECONDS", 300, low=10, high=3600),
            openclaw_max_retries=_int("OPENCLAW_MAX_RETRIES", 1, low=0, high=3),
            chunk_max_chars=_int("CHUNK_MAX_CHARS", 48000, low=4000, high=400000),
            chunk_overlap_chars=_int("CHUNK_OVERLAP_CHARS", 800, low=0, high=8000),
            max_chunks=_int("MAX_CHUNKS", 60, low=1, high=500),
            reduce_fan_in=_int("REDUCE_FAN_IN", 6, low=2, high=20),
            max_pdf_mb=_int("MAX_PDF_MB", 100, low=1, high=2048),
            max_pdf_pages=_int("MAX_PDF_PAGES", 500, low=1, high=10000),
            min_chars_per_page=_int("MIN_CHARS_PER_PAGE", 30, low=1, high=1000),
            markdown_view_max_chars=_int("MARKDOWN_VIEW_MAX_CHARS", 40000, low=2000, high=1000000),
            timezone=_str("TZ", "Asia/Shanghai"),
            log_level=_str("WORKBENCH_LOG_LEVEL", "INFO").upper(),
            port=_int("WORKBENCH_PORT", 8080, low=1, high=65535),
            ai_poll_hint_seconds=_int("POLL_HINT_SECONDS", 30, low=5, high=600),
        )

    def auth_ready(self) -> tuple[bool, str]:
        """返回 (是否可认证, 原因)。启动时校验，避免部署完才发现没配口令。"""
        if self.dev_mode and self.dev_password:
            return True, "dev"
        if not self.password_hash:
            return False, "未设置 WORKBENCH_PASSWORD_HASH"
        if self.password_hash.startswith("scrypt$") and "REPLACE_ME" not in self.password_hash:
            return True, "scrypt"
        return False, "WORKBENCH_PASSWORD_HASH 仍是占位符或格式不正确"


settings = Settings.from_env()
