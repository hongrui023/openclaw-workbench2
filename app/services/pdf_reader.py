"""PDF → 按页文本。

职责边界很窄：只负责"把 PDF 变成带页码的文本"，不负责分块、不负责调用模型。

失败必须分类上报（对应方案文档 6.5 节）。这里的原则是：
**任何解析失败都不允许去问模型"这篇论文讲了什么"**——
不知道就是不知道，猜出来的内容比失败更糟。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger

log = get_logger()

# PyMuPDF 新旧两种导入名都兼容（1.24.x 用 fitz，1.25+ 推荐 pymupdf）
try:  # pragma: no cover - 取决于安装版本
    import pymupdf as _fitz  # type: ignore
    _MUPDF_API = "pymupdf"
except ImportError:  # pragma: no cover
    try:
        import fitz as _fitz  # type: ignore
        _MUPDF_API = "fitz"
    except ImportError:
        _fitz = None  # type: ignore
        _MUPDF_API = "missing"

_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
# PDF 里常见的软连字符（行尾断词），拼回去读起来才正常
_SOFT_HYPHEN = re.compile(r"(\w)-\n(\w)")


@dataclass
class PdfPage:
    number: int  # 1-based，与阅读器里看到的页码一致
    text: str


@dataclass
class PdfDocument:
    filename: str
    title: str
    pages: list[PdfPage] = field(default_factory=list)
    total_chars: int = 0
    page_count: int = 0
    blank_pages: list[int] = field(default_factory=list)

    @property
    def chars_per_page(self) -> float:
        return self.total_chars / self.page_count if self.page_count else 0.0


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SOFT_HYPHEN.sub(r"\1\2", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def _normalize_title(raw: str | None, filename: str) -> str:
    """PDF 元数据里的标题经常是空的、或写着 Word 的默认名，一律兜底到文件名。"""
    stem = os.path.splitext(filename)[0]
    if not raw:
        return stem
    title = " ".join(str(raw).split()).strip()
    if not title:
        return stem
    low = title.lower()
    if low in {"untitled", "microsoft word", "document1", "pdf", "无标题"} or low.startswith("microsoft word -"):
        return stem
    if len(title) > 200:
        title = title[:200] + "…"
    return title


def extract_pdf(path: Path, filename: str) -> PdfDocument:
    """抽取 PDF 全文。所有失败都转成带明确文案的 WorkbenchError。"""
    if _fitz is None:
        raise WorkbenchError(
            ErrorCode.INTERNAL,
            "PDF 解析组件未安装，无法处理文献。",
            detail="pymupdf import failed",
        )

    # ---- 体积上限（在打开之前判断，避免坏文件把内存吃掉）----
    try:
        size_bytes = os.path.getsize(path)
    except OSError as exc:
        raise WorkbenchError(ErrorCode.PDF_NOT_FOUND, detail=f"stat:{type(exc).__name__}") from exc

    limit_bytes = settings.max_pdf_mb * 1024 * 1024
    if size_bytes > limit_bytes:
        raise WorkbenchError(
            ErrorCode.PDF_TOO_LARGE,
            f"「{filename}」体积 {size_bytes / 1048576:.1f} MB，超出上限 "
            f"{settings.max_pdf_mb} MB，已跳过。如需处理请在 NAS 上先压缩或拆分。",
            detail=f"size={size_bytes}",
        )

    # ---- 打开 ----
    try:
        doc = _fitz.open(path)
    except Exception as exc:  # PyMuPDF 的异常类型随版本变化，统一兜住
        name = type(exc).__name__
        if "FileData" in name or "EmptyFile" in name or "format" in str(exc).lower():
            raise WorkbenchError(ErrorCode.PDF_CORRUPT, detail=f"open:{name}") from exc
        log.error("打开 PDF 失败：%s %s", name, str(exc)[:200])
        raise WorkbenchError(ErrorCode.PDF_CORRUPT, detail=f"open:{name}") from exc

    try:
        # ---- 加密 ----
        if doc.needs_pass or doc.is_encrypted:
            raise WorkbenchError(ErrorCode.PDF_ENCRYPTED, detail="encrypted")

        page_count = doc.page_count
        if page_count <= 0:
            raise WorkbenchError(ErrorCode.PDF_CORRUPT, detail="zero_pages")
        if page_count > settings.max_pdf_pages:
            raise WorkbenchError(
                ErrorCode.PDF_TOO_MANY_PAGES,
                f"「{filename}」共 {page_count} 页，超出上限 {settings.max_pdf_pages} 页，已跳过。",
                detail=f"pages={page_count}",
            )

        # ---- 逐页抽文本 ----
        # 内存策略：按页抽取、就地清洗，不保留 PDF 原始对象的多份副本。
        pages: list[PdfPage] = []
        blank_pages: list[int] = []
        total_chars = 0
        for index in range(page_count):
            try:
                page = doc.load_page(index)
                raw = page.get_text("text") or ""
            except Exception as exc:
                # 单页损坏不应毁掉整篇；记下来，按空页处理
                log.warning("第 %d 页抽取失败：%s", index + 1, type(exc).__name__)
                raw = ""
            text = _clean(raw)
            if not text:
                blank_pages.append(index + 1)
            else:
                total_chars += len(text)
            pages.append(PdfPage(number=index + 1, text=text))

        title = _normalize_title(doc.metadata.get("title") if doc.metadata else None, filename)
    finally:
        # 及时释放 PyMuPDF 的底层句柄（4GB 内存的机器上这个很重要）
        try:
            doc.close()
        except Exception:
            pass

    # ---- 无文本层判定 ----
    if total_chars < 20 or (total_chars / page_count) < settings.min_chars_per_page:
        log.info(
            "判定为无可提取文本层：%s 页数=%d 总字符=%d",
            filename,
            page_count,
            total_chars,
        )
        raise WorkbenchError(
            ErrorCode.PDF_NO_TEXT,
            f"「{filename}」没有可提取的文本层（{page_count} 页仅 {total_chars} 个字符），"
            "疑似纯扫描件。V1 不支持 OCR，已跳过。",
            detail=f"chars={total_chars} pages={page_count}",
        )

    return PdfDocument(
        filename=filename,
        title=title,
        pages=pages,
        total_chars=total_chars,
        page_count=page_count,
        blank_pages=blank_pages,
    )
