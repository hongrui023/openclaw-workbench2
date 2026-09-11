"""API 请求/响应模型。

全部用 Pydantic 做校验，好处是"畸形输入"在进入业务逻辑之前就被挡住，
路径守卫只需要面对类型正确的字符串。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_NOTE_CHARS = 2000
MAX_FILES_PER_BATCH = 100


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class AnalyzeRequest(BaseModel):
    """文献分析提交。

    scope="files" 时必须给 files；scope="all" 时忽略 files。
    """

    scope: Literal["files", "all"] = "files"
    files: list[str] = Field(default_factory=list, max_length=MAX_FILES_PER_BATCH)
    force: bool = False

    @field_validator("files")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = (item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        return cleaned


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("内容不能为空")
        return text
