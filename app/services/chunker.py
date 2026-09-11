"""按页边界分块。

为什么不用"固定字符数硬切"：
  1. 硬切会把一句话、一个公式、一张表格从中间切断，每一块都变得难读，
     模型对每一块的理解质量都会下降。
  2. 硬切之后无法回答"这个结论来自原文哪一页"——页码溯源是科研场景的刚需。
所以：**累积连续页，逼近上限再切**；只有在"单页本身就超上限"时才退化为
按段落/句子切分那一页（此时页码仍然准确）。

每块文本内部保留 `[第 N 页]` 标记，它同时承担两个作用：
  - 给模型定位（提示词里也明确告诉它这个标记的含义）
  - 汇总时让模型能写出「来源：原文第 15–24 页」
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger
from app.services.pdf_reader import PdfDocument

log = get_logger()

# 句子结束符（中英文），用于在超长段落里找一个体面的切点
_SENTENCE_END = "。！？；.!?;\n"


@dataclass
class Chunk:
    index: int          # 1-based
    start_page: int
    end_page: int
    text: str
    part: int = 1       # 同一页被拆成多段时，段序号
    parts_total: int = 1

    @property
    def page_range(self) -> str:
        if self.start_page == self.end_page:
            return f"第 {self.start_page} 页"
        return f"第 {self.start_page}–{self.end_page} 页"

    def label(self, total: int) -> str:
        base = f"第 {self.index}/{total} 块 · 原文{self.page_range}"
        if self.parts_total > 1:
            base += f"（该页第 {self.part}/{self.parts_total} 段）"
        return base


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """把一段超长文本切成多份。优先在段落边界切，其次句子边界，最后硬切。"""
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = -1
        # 优先级 1：段落边界
        for marker in ("\n\n", "\n"):
            pos = window.rfind(marker)
            if pos > max_chars * 0.5:
                cut = pos + len(marker)
                break
        # 优先级 2：句子边界
        if cut <= 0:
            for char in _SENTENCE_END:
                pos = window.rfind(char)
                if pos > max_chars * 0.5:
                    cut = max(cut, pos + 1)
        # 优先级 3：硬切（实在没有边界时）
        if cut <= 0:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:]
    if remaining.strip():
        parts.append(remaining.strip())
    return [p for p in parts if p]


def _overlap_tail(text: str, overlap_chars: int) -> str:
    """取上一块的尾部作为重叠区，对齐到行首，避免从半个词开始。"""
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:]
    newline = tail.find("\n")
    if newline != -1 and newline < len(tail) - 1:
        tail = tail[newline + 1 :]
    return tail.strip()


def build_chunks(doc: PdfDocument) -> list[Chunk]:
    """把 PDF 按页边界切成块。超过块数上限直接判定为异常文档并报错。"""
    max_chars = settings.chunk_max_chars
    overlap_chars = settings.chunk_overlap_chars

    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_len = 0
    start_page: int | None = None
    end_page: int | None = None
    carry = ""

    def flush() -> None:
        nonlocal buffer, buffer_len, start_page, end_page, carry
        if not buffer:
            return
        body = "\n\n".join(buffer)
        text = f"[接上文]\n{carry}\n\n{body}" if carry else body
        chunks.append(
            Chunk(
                index=len(chunks) + 1,
                start_page=start_page or 1,
                end_page=end_page or (start_page or 1),
                text=text,
            )
        )
        carry = _overlap_tail(body, overlap_chars)
        buffer = []
        buffer_len = 0
        start_page = None
        end_page = None

    for page in doc.pages:
        if not page.text:
            # 空白页（含图片页）不塞进块里，避免制造无意义的"请分析这张图"
            continue

        marker = f"[第 {page.number} 页]\n{page.text}"

        if len(marker) > max_chars:
            # 单页超上限：先把已累积的内容落成一块，再单独切这一页
            flush()
            parts = _split_oversized(marker, max_chars)
            for order, part in enumerate(parts, start=1):
                chunks.append(
                    Chunk(
                        index=len(chunks) + 1,
                        start_page=page.number,
                        end_page=page.number,
                        text=(f"[接上文]\n{carry}\n\n{part}" if carry else part),
                        part=order,
                        parts_total=len(parts),
                    )
                )
                carry = _overlap_tail(part, overlap_chars)
            continue

        if buffer and buffer_len + len(marker) > max_chars:
            flush()

        if not buffer:
            start_page = page.number
        end_page = page.number
        buffer.append(marker)
        buffer_len += len(marker) + 2

    flush()

    if not chunks:
        raise WorkbenchError(ErrorCode.PDF_NO_TEXT, detail="no_chunks")

    if len(chunks) > settings.max_chunks:
        log.info(
            "文档分块数 %d 超过上限 %d：%s", len(chunks), settings.max_chunks, doc.filename
        )
        raise WorkbenchError(
            ErrorCode.PDF_TOO_FRAGMENTED,
            f"「{doc.filename}」预计需要分为 {len(chunks)} 块，超过上限 "
            f"{settings.max_chunks} 块，已跳过。该文件可能是整本书或合订本，"
            "V1 建议先按章节拆分。",
            detail=f"chunks={len(chunks)}",
        )

    total = len(chunks)
    log.info("分块完成：%s → %d 块", doc.filename, total)
    return chunks
