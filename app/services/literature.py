"""功能一：科研文献分析（编排层）。

完整流水线（对应方案文档 6.2–6.5 节）：

    PDF → 抽文本 → 判断长度
                     ├─ ≤ 一块容量 → 【直通模式】1 次调用出结果
                     └─ > 一块容量 → 【分段模式】
                                       Level 1  N 块 → N 份结构化要点（逐块，串行）
                                       Level 2  份数 > fan_in → 分组归并（可多轮）
                                       Level 3  汇总成最终六段式 Markdown
                     → 校验必备小节 → 写入 literature/<同名>.md

不可动摇的三条底线：
  1. **不编造**：解析失败绝不调用模型去猜；模型缺小节则判定失败、不写文件。
  2. **不无限重试**：只有网络类错误重试 1 次；配置类错误立刻终止。
  3. **不产出脏文件**：任何一步失败都不写 .md。宁可没有文件，也不要半成品。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import settings
from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger, human_duration, now
from app.security.paths import ROOT_LITERATURE, guard
from app.services import prompts
from app.services.chunker import Chunk, build_chunks
from app.services.openclaw import OpenClawClient, client as default_client
from app.services.pdf_reader import PdfDocument, extract_pdf

log = get_logger()

REQUIRED_SECTIONS = ("研究背景", "核心实验方法", "关键实验结果", "主要结论")
ALL_SECTIONS = REQUIRED_SECTIONS + ("研究局限性", "参考文献")

# 缺失分块比例超过这个值 → 整篇判失败，不写文件
MISSING_RATIO_LIMIT = 0.30

# 组装提示词时单个小结的字符上限，防止归并轮次里输入无限膨胀
SUMMARY_MAX_CHARS = 6000
MERGED_MAX_CHARS = 12000

# 模型输出的长度上限（防御性，避免异常输出把内存和磁盘打满）
OUTPUT_MAX_CHARS = 200_000

_STATUS_OK = "ok"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"

ProgressFn = Callable[..., None]


def _noop(**_kwargs: Any) -> None:  # pragma: no cover - 占位
    return None


@dataclass
class AnalyzeOutcome:
    filename: str
    status: str
    mode: str = ""
    markdown_name: str = ""
    chars_written: int = 0
    calls: int = 0
    chunk_count: int = 0
    missing_labels: list[str] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    elapsed: float = 0.0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == _STATUS_OK


# ----------------------------------------------------------------------
# 输出清洗与校验
# ----------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def _normalize_markdown(text: str) -> str:
    """把模型输出整理成"可以直接落盘"的正文。"""
    out = (text or "").strip()
    if not out:
        return ""

    fenced = _FENCE.match(out)
    if fenced:
        out = fenced.group(1).strip()

    # 如果模型自己写了 H1 标题，去掉——标题由工作台统一生成，保证可追溯
    lines = out.split("\n")
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if first.startswith("# ") and not first.startswith("## "):
            lines.pop(0)
            continue
        break
    out = "\n".join(lines).strip()

    if len(out) > OUTPUT_MAX_CHARS:
        out = out[:OUTPUT_MAX_CHARS] + "\n\n（输出过长，已截断）"
    return out


def _section_present(text: str, name: str) -> bool:
    return re.search(rf"^#{{1,4}}\s*{re.escape(name)}", text, re.MULTILINE) is not None


def validate_output(text: str) -> tuple[bool, list[str]]:
    """校验必备小节。返回 (是否通过, 缺失列表)。

    只强制四个核心小节；「研究局限性」和「参考文献」允许写成
    "原文未提供"——有些论文确实没有独立的局限性讨论段落，
    强行要求模型编一段出来，正好违背"不编造"。
    """
    missing = [name for name in REQUIRED_SECTIONS if not _section_present(text, name)]
    return (not missing), missing


def _assemble_document(
    *,
    doc: PdfDocument,
    mode_label: str,
    body: str,
    missing_labels: list[str],
    missing_pages: list[str],
) -> str:
    header = [
        f"# {doc.title}",
        "",
        f"> 来源文件：{doc.filename}",
        f"> 分析时间：{now().strftime('%Y-%m-%d %H:%M')}",
        f"> 分析方式：{mode_label}",
        f"> 原文规模：{doc.page_count} 页 / 约 {doc.total_chars:,} 字符",
        f"> 模型标识：{settings.openclaw_model}",
        "> 说明：本文件由 openclaw-workbench 自动生成，是对原文的提炼，可能存在偏差。",
        "> 请以原文为准，尤其是数据与结论部分。",
    ]

    if missing_labels:
        warning = [
            "",
            f"> ⚠️ 本次分析有 {len(missing_labels)} 个分块未能完成：{'、'.join(missing_labels)}"
            + (f"（{'、'.join(missing_pages)}）" if missing_pages else ""),
            "> 以下内容不包含这些部分，请勿视为完整分析。",
        ]
        header += warning

    return "\n".join(header) + "\n\n" + body.strip() + "\n"


def _missing_note(labels: list[str], pages: list[str]) -> str:
    if not labels:
        return "无。全部内容均成功分析。"
    detail = "、".join(labels)
    if pages:
        detail += f"（对应 {'、'.join(pages)}）"
    return (
        f"缺失 {len(labels)} 个分块：{detail}。\n"
        "请【不要】为这些缺失部分编造任何内容；如果某一个小节完全依赖于缺失部分，"
        "请在该小节下明确写「（本部分内容位于未成功分析的原文区间，无法确认）」。"
    )


def _numbered_notes(items: list[tuple[str, str]]) -> str:
    blocks = []
    for label, text in items:
        body = text.strip()
        if len(body) > SUMMARY_MAX_CHARS:
            body = body[:SUMMARY_MAX_CHARS] + "…（本块小结过长，已截断）"
        blocks.append(f"【{label}】\n{body}")
    return "\n\n---\n\n".join(blocks)


# ----------------------------------------------------------------------
# 单篇分析
# ----------------------------------------------------------------------
async def analyze_pdf(
    filename: str,
    *,
    force: bool = False,
    client: OpenClawClient | None = None,
    report: ProgressFn = _noop,
) -> AnalyzeOutcome:
    client = client or default_client
    started = time.monotonic()
    outcome = AnalyzeOutcome(filename=filename, status=_STATUS_FAILED)

    markdown_name = f"{os.path.splitext(filename)[0]}.md"

    try:
        # ---- 1. 路径守卫 + 存在性 ----
        pdf_path = guard.read_path(ROOT_LITERATURE, filename)

        # ---- 2. 跳过已生成 ----
        if not force and guard.exists(ROOT_LITERATURE, markdown_name):
            outcome.status = _STATUS_SKIPPED
            outcome.markdown_name = markdown_name
            outcome.note = "已存在同名 Markdown，本次跳过（如需重做请勾选「强制重新分析」）"
            return outcome

        # ---- 3. 抽文本（失败即终止，绝不交给模型去猜）----
        report(stage="解析 PDF", message=f"正在抽取《{filename}》的文本…", current=filename)
        doc = extract_pdf(pdf_path, filename)

        if doc.blank_pages:
            log.info("%s 有 %d 页无可读文本（图片页或无内容页）", filename, len(doc.blank_pages))

        # ---- 4. 直通 or 分段 ----
        if doc.total_chars <= settings.direct_mode_max_chars:
            body, mode_label, chunk_count, missing = await _run_direct(doc, client, report)
        else:
            body, mode_label, chunk_count, missing = await _run_chunked(doc, client, report)

        # ---- 5. 校验必备小节 ----
        ok, missing_sections = validate_output(body)
        if not ok:
            log.warning("%s 输出缺少必备小节：%s", filename, missing_sections)
            raise WorkbenchError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                f"「{filename}」的分析结果缺少必备小节（{'、'.join(missing_sections)}），"
                "已放弃写入，避免生成错误文件。可稍后重试。",
                detail=f"missing_sections={missing_sections}",
            )

        # ---- 6. 组装并写入 ----
        report(stage="写入文件", message=f"正在写入 {markdown_name}…")
        content = _assemble_document(
            doc=doc,
            mode_label=mode_label,
            body=body,
            missing_labels=[label for label, _ in missing],
            missing_pages=[pages for _, pages in missing],
        )
        target = guard.write_path(ROOT_LITERATURE, markdown_name)
        written = guard.write_text_replace(target, content)

        outcome.status = _STATUS_OK
        outcome.mode = mode_label
        outcome.markdown_name = markdown_name
        outcome.chars_written = written
        outcome.chunk_count = chunk_count
        outcome.missing_labels = [label for label, _ in missing]
        return outcome

    except WorkbenchError as exc:
        outcome.error_code = exc.code.value
        outcome.error_message = exc.message
        log.info("分析未完成：%s code=%s detail=%s", filename, exc.code.value, exc.detail or "-")
        return outcome
    except Exception as exc:
        outcome.error_code = ErrorCode.INTERNAL.value
        outcome.error_message = ErrorCode.INTERNAL.value
        log.exception("分析异常：%s err=%s", filename, type(exc).__name__)
        return outcome
    finally:
        outcome.elapsed = time.monotonic() - started


# ----------------------------------------------------------------------
# 直通模式
# ----------------------------------------------------------------------
async def _run_direct(
    doc: PdfDocument, client: OpenClawClient, report: ProgressFn
) -> tuple[str, str, int, list[tuple[str, str]]]:
    report(
        stage="直通分析",
        done=0,
        total=1,
        message=f"文献较短（约 {doc.total_chars:,} 字符），一次性分析中…",
    )
    prompt = prompts.load(
        "final_synthesis",
        document_title=doc.title,
        source_note=(
            f"以下是《{doc.filename}》的完整正文（约 {doc.total_chars:,} 字符，"
            f"共 {doc.page_count} 页）。正文中的 [第 N 页] 标记表示该段内容在原文中的页码。"
        ),
        summaries=_full_text_with_pages(doc),
        missing_note="无。全文已完整提供。",
        mode_note="本文献采用直通模式：一次性阅读全文后直接输出六段式分析。",
    )
    body = await client.chat(prompts.guardrail(), prompt)
    report(done=1, total=1, message="分析完成，正在校验输出…")
    return _normalize_markdown(body), f"直通模式（{doc.total_chars:,} 字符，1 次调用）", 1, []


def _full_text_with_pages(doc: PdfDocument) -> str:
    blocks = []
    for page in doc.pages:
        if not page.text:
            continue
        blocks.append(f"[第 {page.number} 页]\n{page.text}")
    return "\n\n".join(blocks)


# ----------------------------------------------------------------------
# 分段模式
# ----------------------------------------------------------------------
async def _run_chunked(
    doc: PdfDocument, client: OpenClawClient, report: ProgressFn
) -> tuple[str, str, int, list[tuple[str, str]]]:
    chunks = build_chunks(doc)
    total_chunks = len(chunks)

    # 预估总调用次数，让进度条有意义
    merge_calls = _estimate_merge_calls(total_chunks)
    total_steps = total_chunks + merge_calls + 1

    report(
        stage="分段分析",
        done=0,
        total=total_steps,
        message=f"文献较长，按页边界切为 {total_chunks} 块，逐块分析中…",
    )

    # ---- Level 1：逐块抽取要点（同篇内并发，默认 2，环境变量 LITERATURE_CONCURRENCY 可调）----
    notes: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []  # (块标签, 页码范围)
    done = 0

    sem = asyncio.Semaphore(settings.literature_concurrency)

    async def _extract_one(chunk: Chunk) -> tuple[str, str, str | None, tuple[WorkbenchError, str] | None]:
        label = chunk.label(total_chunks)
        async with sem:
            try:
                text = await _extract_chunk(client, doc, chunk, total_chunks)
                return (label, "ok", text, None)
            except WorkbenchError as exc:
                return (label, "err", None, (exc, chunk.page_range))

    # 一次发出所有块，但受 semaphore 限流。gather 保持顺序 → 进度 done 按顺序计。
    results = await asyncio.gather(*[_extract_one(chunk) for chunk in chunks])
    for label, status, text, err_info in results:
        if status == "ok":
            notes.append((label, text))
        else:
            exc, pages = err_info  # type: ignore[misc]
            log.warning("分块失败：%s %s code=%s", doc.filename, label, exc.code.value)
            missing.append((label, pages))
            report(failed=len(missing))
        done += 1
        report(done=done, message=f"已完成 {label}…")

    if not notes:
        raise WorkbenchError(
            ErrorCode.ANALYSIS_FAILED,
            f"「{doc.filename}」的所有分块都未能完成分析，未生成 Markdown。"
            "请检查 AI 服务是否可用（可用 scripts/check_openclaw.py 自检）。",
            detail=f"all_chunks_failed total={total_chunks}",
        )

    # ---- 熔断：缺失比例过高就整篇失败，不产出"看起来完整其实缺一半"的文件 ----
    missing_ratio = len(missing) / total_chunks
    if missing_ratio > MISSING_RATIO_LIMIT:
        raise WorkbenchError(
            ErrorCode.ANALYSIS_FAILED,
            f"「{doc.filename}」有 {len(missing)}/{total_chunks} 个分块分析失败"
            f"（超过 {int(MISSING_RATIO_LIMIT * 100)}% 上限），已放弃写入，避免生成不完整的文件。",
            detail=f"missing={len(missing)}/{total_chunks}",
        )

    # ---- Level 2：分层归并（每轮内并发，归并失败透传原始小结）----
    level = 1
    while len(notes) > settings.reduce_fan_in:
        groups = _group(notes, settings.reduce_fan_in)
        group_labels = [
            f"归并{level}-{i+1}（合并：{'、'.join(lbl for lbl, _ in g)}）"
            for i, g in enumerate(groups, start=1)
        ]
        report(
            stage="归并中间结果",
            message=f"第 {level} 轮归并：{len(notes)} 份小结 → {len(groups)} 组…",
        )

        async def _merge_one(group_label: str, group: list[tuple[str, str]]) -> tuple[str, str | None]:
            async with sem:
                try:
                    text = await _merge_group(client, doc, group_label, group)
                    return ("merged", text)
                except WorkbenchError as exc:
                    log.warning("归并失败，改为透传原始小结：%s code=%s", group_label, exc.code.value)
                    return ("passthrough", None)

        merge_results = await asyncio.gather(
            *[_merge_one(gl, g) for gl, g in zip(group_labels, groups)]
        )
        merged: list[tuple[str, str]] = []
        for index, ((status, text), group) in enumerate(zip(merge_results, groups), start=1):
            if status == "merged":
                merged.append((f"第 {level} 轮归并结果 {index}/{len(groups)}", text or ""))
            else:
                merged.extend(group)
        notes = merged
        level += 1
        done += len(groups)
        report(done=done)

    # ---- Level 3：最终汇总 ----
    report(
        stage="生成最终 Markdown",
        done=total_steps - 1,
        total=total_steps,
        message="正在汇总为六段式分析…",
    )
    prompt = prompts.load(
        "final_synthesis",
        document_title=doc.title,
        source_note=(
            f"以下是《{doc.filename}》分块分析后得到的 {len(notes)} 份结构化小结"
            f"（原文共 {doc.page_count} 页，切成 {total_chunks} 块）。"
            "每份小结前的【】里标注了它对应的原文页码范围，请在最终输出的每个小节末尾"
            "以「> 来源：原文第 X–Y 页」的形式标出来源。"
        ),
        summaries=_numbered_notes(notes),
        missing_note=_missing_note([lbl for lbl, _ in missing], [pg for _, pg in missing]),
        mode_note=(
            f"本文献采用分段分析：原文切为 {total_chunks} 块，逐块抽取要点，"
            f"再经 {level - 1} 轮分层归并后汇总为最终结果。"
        ),
    )
    body = await client.chat(prompts.guardrail(), prompt)

    mode_label = (
        f"分段分析（原文 {doc.page_count} 页 → {total_chunks} 块 → "
        f"{level - 1} 轮归并 → 汇总）"
    )
    report(done=total_steps, total=total_steps, message="分析完成，正在校验输出…")
    return _normalize_markdown(body), mode_label, total_chunks, missing


def _estimate_merge_calls(count: int) -> int:
    calls = 0
    current = count
    fan_in = max(2, settings.reduce_fan_in)
    while current > fan_in:
        current = (current + fan_in - 1) // fan_in
        calls += current
    return calls


def _group(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _extract_chunk(
    client: OpenClawClient, doc: PdfDocument, chunk: Chunk, total_chunks: int
) -> str:
    prompt = prompts.load(
        "chunk_extract",
        document_title=doc.title,
        chunk_label=chunk.label(total_chunks),
        content=chunk.text,
    )
    text = await client.chat(prompts.guardrail(), prompt)
    if not text.strip():
        raise WorkbenchError(ErrorCode.MODEL_OUTPUT_INVALID, detail="empty_chunk_summary")
    return text.strip()


async def _merge_group(
    client: OpenClawClient,
    doc: PdfDocument,
    group_label: str,
    group: list[tuple[str, str]],
) -> str:
    body = _numbered_notes(group)
    if len(body) > MERGED_MAX_CHARS:
        body = body[:MERGED_MAX_CHARS] + "…（合并输入过长，已截断）"
    prompt = prompts.load(
        "merge_partial",
        document_title=doc.title,
        group_label=group_label,
        summaries=body,
    )
    text = await client.chat(prompts.guardrail(), prompt)
    if not text.strip():
        raise WorkbenchError(ErrorCode.MODEL_OUTPUT_INVALID, detail="empty_merge")
    return text.strip()


# ----------------------------------------------------------------------
# 目标解析与批量执行
# ----------------------------------------------------------------------
def resolve_targets(scope: str, files: list[str]) -> list[str]:
    """把用户请求变成确定的文件清单。所有名字都来自 literature 的实际列表。"""
    available = {entry.name: entry for entry in guard.list_literature()}

    if scope == "all":
        if not available:
            raise WorkbenchError(
                ErrorCode.PDF_NOT_FOUND,
                "literature 目录中没有找到任何 PDF 文件。",
            )
        return [entry.name for entry in sorted(available.values(), key=lambda e: e.name.lower())]

    if not files:
        raise WorkbenchError(ErrorCode.BAD_REQUEST, "请至少指定一个 PDF 文件名。")

    resolved: list[str] = []
    unknown: list[str] = []
    for name in files:
        if name in available:
            resolved.append(name)
            continue
        # 容忍不带 .pdf 后缀的写法
        if not name.lower().endswith(".pdf") and f"{name}.pdf" in available:
            resolved.append(f"{name}.pdf")
            continue
        unknown.append(name)

    if unknown:
        suggestion = _suggest(unknown[0], list(available.keys()))
        hint = f"你是不是想找：{suggestion}？" if suggestion else "请在「literature 文件列表」中确认准确名称。"
        raise WorkbenchError(
            ErrorCode.PDF_NOT_FOUND,
            f"未在 literature/ 中找到：{'、'.join(unknown)}。{hint}",
        )
    return resolved


def _suggest(name: str, candidates: list[str]) -> str:
    stem = os.path.splitext(name)[0].lower().replace(" ", "")
    best = ""
    best_score = 0
    for candidate in candidates:
        cand_stem = os.path.splitext(candidate)[0].lower().replace(" ", "")
        if not cand_stem:
            continue
        if stem and (stem in cand_stem or cand_stem in stem):
            score = min(len(stem), len(cand_stem))
        else:
            score = len(set(stem) & set(cand_stem))
        if score > best_score:
            best_score = score
            best = candidate
    return best if best_score >= max(3, len(stem) // 3) else ""


@dataclass
class BatchSummary:
    total: int = 0
    ok: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    calls: int = 0
    missing: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "skipped": self.skipped,
            "failed": self.failed,
            "calls": self.calls,
            "missing": self.missing,
            "elapsed": int(self.elapsed),
            "elapsed_label": human_duration(self.elapsed),
        }

    def headline(self) -> str:
        parts = [f"成功 {len(self.ok)}"]
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)}")
        if self.failed:
            parts.append(f"失败 {len(self.failed)}")
        return " / ".join(parts)


async def analyze_batch(
    filenames: list[str],
    *,
    force: bool = False,
    client: OpenClawClient | None = None,
    report: ProgressFn = _noop,
) -> BatchSummary:
    """批量分析。串行执行——4GB 内存 + OpenClaw 也在抢资源，并行只会互相拖慢。"""
    client = client or default_client
    summary = BatchSummary(total=len(filenames))
    started = time.monotonic()

    for index, filename in enumerate(filenames, start=1):
        report(
            stage="批量分析",
            total=len(filenames),
            done=index - 1,
            message=f"[{index}/{len(filenames)}] 正在处理《{filename}》…",
            current=filename,
        )
        calls_before = client.call_count
        outcome = await analyze_pdf(filename, force=force, client=client, report=report)
        summary.calls += client.call_count - calls_before

        if outcome.status == _STATUS_OK:
            summary.ok.append(filename)
            if outcome.mode:
                summary.modes.append(outcome.mode)
            if outcome.missing_labels:
                summary.missing.extend(f"{filename}：{'、'.join(outcome.missing_labels)}")
        elif outcome.status == _STATUS_SKIPPED:
            summary.skipped.append(filename)
        else:
            summary.failed.append(
                {
                    "file": filename,
                    "code": outcome.error_code,
                    "message": outcome.error_message,
                }
            )
        report(done=index, failed=len(summary.failed))

    summary.elapsed = time.monotonic() - started
    return summary
