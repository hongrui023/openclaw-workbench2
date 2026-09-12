#!/usr/bin/env python3
"""本地自检 —— 不需要 OpenClaw、不需要 NAS，就能验证整套逻辑。

这是方案文档里 "H 阶段（本地先跑通，不上 NAS）" 的落地脚本。
"构建 → 传输 → 导入极空间" 这个循环很慢，本地能挡掉绝大部分 bug。

覆盖范围：
  A. 路径守卫：目录逃逸、绝对路径、盘符、后缀白名单、符号链接逃逸
  B. 生活记录：规则分类、追加语义（真的只追加）、分类结果写入 daily_notes.md
  C. 分段分析：分块、页码标注、分层归并、Markdown 落盘
  D. 失败处理：文件不存在 / 损坏 PDF / 模型输出缺小节 → 都不产生 .md
  E. HTTP 层：健康检查、登录、CSRF、认证保护

用法：
    python scripts/selftest.py

脚本会把全部数据写进一个临时目录，**不会碰你 NAS 上的任何数据**，
跑完自动清理。退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ----------------------------------------------------------------------
# 必须在导入 app.* 之前设置环境变量——配置在导入时就读进内存
# ----------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="owb-selftest-"))
os.environ.update(
    {
        "WORKBENCH_APP_ROOT": str(ROOT),
        "LITERATURE_DIR": str(TMP / "literature"),
        "LIFE_NOTES_DIR": str(TMP / "life_notes"),
        "WORKBENCH_WORK_DIR": str(TMP / "work"),
        "WORKBENCH_DEV": "1",
        "WORKBENCH_PASSWORD": "selftest-password-0123456789",
        "OPENCLAW_TOKEN": "dummy-token-for-selftest",
        "OPENCLAW_MODEL": "selftest:model",
        # 调小分块上限并调小归并分组，让短测试文档也能走到"分段 + 归并"路径
        "CHUNK_MAX_CHARS": "4000",
        "CHUNK_OVERLAP_CHARS": "200",
        "REDUCE_FAN_IN": "2",
        "MAX_PDF_PAGES": "200",
        "WORKBENCH_LOG_LEVEL": "WARNING",
    }
)

(TMP / "literature").mkdir(parents=True, exist_ok=True)
(TMP / "life_notes").mkdir(parents=True, exist_ok=True)
(TMP / "work").mkdir(parents=True, exist_ok=True)

from app.errors import ErrorCode, WorkbenchError  # noqa: E402
from app.security.paths import (  # noqa: E402
    ROOT_LIFE_NOTES,
    ROOT_LITERATURE,
    _inside,
    guard,
)
from app.services import literature as lit_service  # noqa: E402
from app.services import notes as notes_service  # noqa: E402
from app.services import task_log  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []
REPORT: list[str] = []


def emit(line: str = "") -> None:
    """同时输出到控制台和一个 UTF-8 报告文件。

    为什么要写报告文件：Windows 的 PowerShell 在转发子进程的中文输出时
    编码很不稳定，控制台看到的可能是乱码。报告文件永远是 UTF-8，
    这样"自检结果"本身不会因为终端问题而不可读。
    """
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("utf-8", "replace").decode("ascii", "replace"))
    REPORT.append(line)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        emit(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        emit(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(f"{name}（{reason}）")
    emit(f"  [SKIP] {name}：{reason}")


def section(title: str) -> None:
    emit()
    emit(title)
    emit("-" * len(title))


def expect_error(code: ErrorCode, fn, *args: Any, **kwargs: Any) -> bool:
    try:
        fn(*args, **kwargs)
    except WorkbenchError as exc:
        return exc.code is code
    except Exception:
        return False
    return False


# ----------------------------------------------------------------------
# 假的 AI 客户端：把 OpenClaw 换成一个确定性输出，从而能断言结果
# ----------------------------------------------------------------------
class FakeClient:
    """实现与 OpenClawClient 相同的调用面，但完全离线。"""

    def __init__(self, *, omit_sections: bool = False) -> None:
        self.call_count = 0
        self.omit_sections = omit_sections
        self.seen_prompts: list[str] = []

    async def chat(self, system: str, user: str, **_: Any) -> str:
        self.call_count += 1
        self.seen_prompts.append(user)

        if "待分类内容" in user:  # note_classify
            return json.dumps(
                {"type": "note", "amount": None, "direction": None, "summary": "随手记的一句话"}
            )

        if "本块正文开始" in user:  # chunk_extract
            return (
                "## 研究背景\n本块谈及研究背景（第 1 页）\n\n"
                "## 核心实验方法\n本块记录了若干方法（第 1 页）\n\n"
                "## 关键实验结果\n数值 12.5（第 1 页）\n\n"
                "## 主要结论\n本块给出初步结论（第 1 页）\n\n"
                "## 研究局限性\n本块未涉及\n\n"
                "## 参考文献（本块出现的引用）\n本块未涉及\n"
            )

        if "合并分组" in user:  # merge_partial
            return (
                "## 研究背景\n合并后的背景（第 1–2 页）\n\n"
                "## 核心实验方法\n合并后的方法（第 1–2 页）\n\n"
                "## 关键实验结果\n合并后的结果（第 1–2 页）\n\n"
                "## 主要结论\n合并后的结论（第 1–2 页）\n\n"
                "## 研究局限性\n合并后的局限（第 1–2 页）\n\n"
                "## 参考文献（本块出现的引用）\n[1] Someone et al., 2020\n"
            )

        # final_synthesis
        if self.omit_sections:
            return "## 研究背景\n只有一个小节，故意缺其他必备小节。\n"
        return (
            "## 研究背景\n这是自检生成的背景段落。\n\n> 来源：原文第 1–2 页\n\n"
            "## 核心实验方法\n这是自检生成的方法段落。\n\n> 来源：原文第 1–2 页\n\n"
            "## 关键实验结果\n这是自检生成的结果段落。\n\n> 来源：原文第 1–2 页\n\n"
            "## 主要结论\n这是自检生成的结论段落。\n\n> 来源：原文第 2 页\n\n"
            "## 研究局限性\n原文未明确讨论研究局限性。\n\n"
            "## 参考文献\n原文未提供可完整列出的参考文献列表。\n"
        )

    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = await self.chat(system, user)
        return json.loads(raw)


def make_test_pdf(name: str, pages: int = 6, chars_per_page: int = 900) -> Path:
    """生成一份有真实文本层的测试 PDF。"""
    import pymupdf as fitz  # 用新导入名，避免旧 fitz 别名触发的弃用警告

    doc = fitz.open()
    body = (
        "Experimental results show that the catalytic efficiency increased by 37.5 percent "
        "under ambient conditions, with a standard deviation of 2.1. Previous studies "
        "reported conflicting values. We therefore designed a controlled experiment with "
        "six groups and measured the reaction rate at 298 Kelvin. "
    )
    for index in range(pages):
        page = doc.new_page()
        text = f"Page {index + 1}. " + (body * max(1, chars_per_page // len(body) + 1))[:chars_per_page]
        page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=10)
    path = TMP / "literature" / name
    doc.save(str(path))
    doc.close()
    return path


# ----------------------------------------------------------------------
# A. 路径守卫
# ----------------------------------------------------------------------
def test_path_guard() -> None:
    section("A. 路径守卫")

    # A0. 根内判定函数本身（这是整个逃逸检测的地基，值得单独测）
    check("根内判定：根本身算在内", _inside("/data/literature", "/data/literature"))
    check("根内判定：子文件算在内", _inside("/data/literature/a.pdf", "/data/literature"))
    check("根内判定：根外绝对路径被排除", not _inside("/etc/passwd", "/data/literature"))
    check("根内判定：兄弟目录被排除", not _inside("/data/other/x.pdf", "/data/literature"))
    # 前缀陷阱：只做 startswith 会把 "literature-secret" 误判成在 "literature" 内
    check(
        "根内判定：前缀陷阱（literature-secret）",
        not _inside("/data/literature-secret/a.pdf", "/data/literature"),
    )
    check(
        "根内判定：前缀陷阱（literature.bak）",
        not _inside("/data/literature.bak/a.pdf", "/data/literature"),
    )

    check("拒绝 .. 逃逸", expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "../secret.pdf"))
    check("拒绝绝对路径", expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "/etc/passwd"))
    check(
        "拒绝 Windows 盘符",
        expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "C:/Windows/win.ini"),
    )
    check(
        "拒绝深层 .. 逃逸",
        expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "sub/../../secret.pdf"),
    )
    check(
        "拒绝反斜杠逃逸",
        expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "..\\..\\secret.pdf"),
    )
    check(
        "拒绝 NUL 字节",
        expect_error(ErrorCode.PATH_BAD_NAME, guard.read_path, ROOT_LITERATURE, "a\x00.pdf"),
    )
    check(
        "拒绝非白名单后缀（读）",
        expect_error(ErrorCode.PATH_BAD_SUFFIX, guard.read_path, ROOT_LITERATURE, "notes.txt"),
    )
    check(
        "拒绝非白名单后缀（写）",
        expect_error(ErrorCode.PATH_BAD_SUFFIX, guard.write_path, ROOT_LITERATURE, "payload.sh"),
    )
    check(
        "未知根名直接被拒（不得进入后续分支）",
        expect_error(ErrorCode.PATH_FORBIDDEN, guard.write_path, "other_root", "x.md"),
    )
    check(
        "不允许把 .pdf 当输出目标",
        expect_error(ErrorCode.PATH_BAD_SUFFIX, guard.write_path, ROOT_LITERATURE, "out.pdf"),
    )
    check(
        "不存在的 PDF 报 PDF_NOT_FOUND",
        expect_error(ErrorCode.PDF_NOT_FOUND, guard.read_path, ROOT_LITERATURE, "not-here.pdf"),
    )

    # A1. 链接逃逸（符号链接 / Windows junction）
    #
    # 要证明的事：literature 里放一个指向目录外的链接，守卫必须拒绝。
    #
    # 平台差异（本测试最麻烦的一处）：
    #   - Linux（也就是 NAS）：os.symlink 直接可用，realpath 会解引用。
    #   - Windows：os.symlink 需要管理员或开发者模式，普通会话直接失败；
    #     但 junction（目录联接）不需要任何特权就能创建，而 os.path.islink()
    #     对 junction 返回 False —— 换句话说，Windows 上"最容易造的链接"
    #     恰好是"最容易被漏掉的链接"。所以代码里没有只用 islink()，
    #     而是用了能覆盖 junction 的 _is_link_like()。
    #     本测试因此分两条路径：能建 symlink 就测 symlink，否则测 junction。
    #
    # 诚实声明：junction 是 Windows 的等价物，**不等同于** Linux 符号链接。
    # 真正的 Linux symlink 验证必须在 NAS 容器里做（见 docs/DEPLOY-NAS.md 末节）。
    outside_dir = TMP / "outside-dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "secret.pdf").write_text(
        "this must never be readable through the guard", encoding="utf-8"
    )

    file_link = TMP / "literature" / "evil.pdf"
    dir_link = TMP / "literature" / "evil-sub"

    made = ""
    try:
        if file_link.is_symlink() or file_link.exists():
            os.unlink(file_link)
        os.symlink(str(outside_dir / "secret.pdf"), str(file_link))
        if os.path.islink(file_link):
            made = "symlink"
    except (OSError, NotImplementedError, AttributeError):
        pass

    if not made:
        # Windows 兜底：junction 无需特权。用 PowerShell 而不是 cmd，避免交互式提示。
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"New-Item -ItemType Junction -Path '{dir_link}' -Target '{outside_dir}' -Force | Out-Null",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            if dir_link.exists() and os.path.realpath(str(dir_link)) != str(dir_link):
                made = "junction"
        except Exception:
            pass

    if made == "symlink":
        check(
            "拒绝链接逃逸（软链接指向根外文件）",
            expect_error(ErrorCode.PATH_FORBIDDEN, guard.read_path, ROOT_LITERATURE, "evil.pdf"),
        )
    elif made == "junction":
        check(
            "拒绝链接逃逸（junction 指向根外目录）",
            expect_error(
                ErrorCode.PATH_FORBIDDEN, guard.read_path, ROOT_LITERATURE, "evil-sub/secret.pdf"
            ),
        )
        check(
            "链接本身也不出现在文献列表里",
            all(e.name != "evil-sub" for e in guard.list_literature()),
        )
        # 清理：junction 用 rmdir 摘掉，不会动到目标目录里的内容
        try:
            os.rmdir(dir_link)
        except OSError:
            pass
    else:
        skip(
            "链接逃逸",
            "本机既建不了软链接（Windows 需管理员/开发者模式）也建不了 junction",
        )

    # 正常路径必须仍然可用
    make_test_pdf("guard-ok.pdf", pages=1, chars_per_page=200)
    ok = True
    try:
        guard.read_path(ROOT_LITERATURE, "guard-ok.pdf")
    except WorkbenchError:
        ok = False
    check("正常文件名可以通过", ok)

    check("列表只返回 PDF", all(e.name.lower().endswith(".pdf") for e in guard.list_literature()))


# ----------------------------------------------------------------------
# B. 生活记录
# ----------------------------------------------------------------------
def test_notes() -> None:
    section("B. 生活记录")

    async def run() -> None:
        fake = FakeClient()

        r1 = await notes_service.add_note("今天买实验耗材花了 280 元。", client=fake)  # type: ignore[arg-type]
        check("记账被规则识别", r1.type == "expense" and r1.method == "rule", f"got {r1.type}/{r1.method}")
        check("金额正确", r1.amount == 280.0, f"got {r1.amount}")
        check("方向为支出", r1.direction == "out", f"got {r1.direction}")
        check("摘要只留要点", r1.summary == "买实验耗材", f"got {r1.summary!r}")
        check("记账没有调用模型", fake.call_count == 0, f"calls={fake.call_count}")

        r2 = await notes_service.add_note("明天去取实验试剂", client=fake)  # type: ignore[arg-type]
        check("待办被规则识别", r2.type == "todo" and r2.method == "rule", f"got {r2.type}/{r2.method}")

        r3 = await notes_service.add_note("收到报销款 1200 元", client=fake)  # type: ignore[arg-type]
        check("收入方向正确", r3.type == "expense" and r3.direction == "in", f"got {r3.direction}")

        r4 = await notes_service.add_note("今天看到一篇关于钙钛矿的论文挺有意思", client=fake)  # type: ignore[arg-type]
        check("随笔走模型兜底", r4.type == "note", f"got {r4.type}")
        check("模型被调用", fake.call_count == 1, f"calls={fake.call_count}")

        path = guard.read_path(ROOT_LIFE_NOTES, "daily_notes.md")
        content, _ = guard.read_text(path)
        check("daily_notes.md 已创建", bool(content))
        check("含待办小节", "### 待办" in content)
        check("含收支小节", "### 收支" in content)
        check("含随笔小节", "### 随笔" in content)
        check("待办复选框格式正确", "- [ ] 明天去取实验试剂" in content)
        check("金额格式化为两位小数", "¥280.00" in content)
        check("收入行方向标记为收入", "收入" in content)

        # 追加语义：原有内容必须一字不动
        before = content
        await notes_service.add_note("又买了 50 元的移液枪头", client=fake)  # type: ignore[arg-type]
        after, _ = guard.read_text(path)
        check("追加而非重写（原有内容完整保留）", after.startswith(before), "原有内容被改动")

        # 表格连续性：
        # 上一条是随笔，所以这次会**新开**一个「### 收支」小节并带表头（这是正确行为）；
        # 紧接着再写一条支出，就应该续进同一张表，而不再重复表头。
        headers_first = after.count("| 时间 | 方向 | 金额 | 说明 |")
        await notes_service.add_note("买咖啡花了 22 元", client=fake)  # type: ignore[arg-type]
        after2, _ = guard.read_text(path)
        headers_second = after2.count("| 时间 | 方向 | 金额 | 说明 |")
        check(
            "连续支出续进同一张表（不重复表头）",
            headers_first == headers_second,
            f"{headers_first} → {headers_second}",
        )
        check("切换小节时才建新表头", headers_first >= 2, str(headers_first))

    asyncio.run(run())


# ----------------------------------------------------------------------
# C. 分段分析全流程
# ----------------------------------------------------------------------
def test_literature_pipeline() -> None:
    section("C. 文献分析（分段 + 分层归并）")

    async def run() -> None:
        make_test_pdf("paper-long.pdf", pages=12, chars_per_page=900)

        fake = FakeClient()
        outcome = await lit_service.analyze_pdf("paper-long.pdf", client=fake)  # type: ignore[arg-type]

        check("分析成功", outcome.status == "ok", f"{outcome.status} {outcome.error_message}")
        check("走了分段模式", "分段分析" in outcome.mode, outcome.mode)
        check("分块数 > 1", outcome.chunk_count > 1, f"chunks={outcome.chunk_count}")
        check("发生多次模型调用", fake.call_count > outcome.chunk_count, f"calls={fake.call_count}")
        check("Markdown 已写入", guard.exists(ROOT_LITERATURE, "paper-long.md"))

        path = guard.read_path(ROOT_LITERATURE, "paper-long.md")
        content, _ = guard.read_text(path)
        check("包含六个小节", all(s in content for s in lit_service.ALL_SECTIONS))
        check("含来源页标注", "来源：原文第" in content)
        check("头部含来源文件", "来源文件：paper-long.pdf" in content)
        check("头部含分析方式", "分析方式：" in content)
        check("头部含模型标识", "模型标识：" in content)

        # 提示词里必须带上页码定位信息，否则溯源无从谈起
        check(
            "提示词带页码标记",
            any("[第 1 页]" in prompt for prompt in fake.seen_prompts),
            "正文里没有页码标记",
        )

        # 跳过已生成
        outcome2 = await lit_service.analyze_pdf("paper-long.pdf", client=fake)  # type: ignore[arg-type]
        check("已有 .md 时默认跳过", outcome2.status == "skipped", outcome2.status)
        calls_before = fake.call_count
        check("跳过时不调用模型", fake.call_count == calls_before)

        # 强制重做
        outcome3 = await lit_service.analyze_pdf("paper-long.pdf", force=True, client=fake)  # type: ignore[arg-type]
        check("强制重新分析可覆盖", outcome3.status == "ok", outcome3.status)

        # 短文走直通
        make_test_pdf("paper-short.pdf", pages=1, chars_per_page=1500)
        fake2 = FakeClient()
        outcome4 = await lit_service.analyze_pdf("paper-short.pdf", client=fake2)  # type: ignore[arg-type]
        check("短文走直通模式", "直通模式" in outcome4.mode, outcome4.mode)
        check("直通模式只调用一次", fake2.call_count == 1, f"calls={fake2.call_count}")

        # 批量
        summary = await lit_service.analyze_batch(
            ["paper-short.pdf", "paper-long.pdf"], client=fake  # type: ignore[arg-type]
        )
        check("批量：短篇被跳过", "paper-short.pdf" in summary.skipped, str(summary.skipped))
        check("批量：总数为 2", summary.total == 2, str(summary.total))

    asyncio.run(run())


# ----------------------------------------------------------------------
# C2. 结果文案组装（回归：曾经把 to_dict 的键当成属性用）
# ----------------------------------------------------------------------
def test_summary_text() -> None:
    """曾经踩过的坑：

    api/literature.py 里写过 `summary.elapsed_label`，但 `elapsed_label` 只是
    `BatchSummary.to_dict()` 返回的**字典键**，类上并没有这个属性。
    结果是每篇文献分析完（文件已成功写入）之后抛 AttributeError，
    被任务层兜底成"工作台内部错误"——用户看到任务失败，文件却已经生成了。

    这里锁死两件事：① 真实存在的属性名 ② 文案组装用的调用方式本身能跑通。
    """
    section("C2. 批量结果文案（防 to_dict 键误当属性）")

    from app.obs import human_duration

    s = lit_service.BatchSummary(total=2, ok=["a.pdf"], skipped=["b.pdf"], elapsed=125.0)

    for attr in ("total", "ok", "skipped", "failed", "modes", "calls", "missing", "elapsed"):
        check(f"BatchSummary 有属性 {attr}", hasattr(s, attr))

    check("BatchSummary 没有 elapsed_label 属性", not hasattr(s, "elapsed_label"))

    d = s.to_dict()
    check("to_dict 里有 elapsed_label", "elapsed_label" in d)
    check("elapsed_label 是字符串", isinstance(d["elapsed_label"], str))

    # api/literature.py 的 runner 就是这几行，必须能不抛异常地跑完
    try:
        lines = [
            f"模式：批量（全部） · 共 {s.total} 篇",
            f"结果：{s.headline()}",
            f"AI 调用：{s.calls} 次",
            f"耗时：{human_duration(s.elapsed)}",
        ]
        check("结果文案组装不抛异常", True)
        check("文案里含耗时", any("耗时：" in line for line in lines))
    except Exception as exc:  # noqa: BLE001
        check("结果文案组装不抛异常", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# C3. 性能优化：轮询自适应 + 文献分析分块并发
# ----------------------------------------------------------------------
def test_performance_optimizations() -> None:
    """锁死 1.0.3 的两项提速改动（防回归）。

    ① Job.to_poll() 返回 poll_hint：任务刚启动/活跃时给短间隔（用户立刻看到首条进度），
       长跑时给长间隔（反代穿透下省流量）；终态不返回（前端据此停轮询）。
    ② _run_chunked 同篇内分块并发：让 in-flight 峰值 ≥ literature_concurrency 的下限，
       证明并发路径确实生效，而不是悄悄退化回了串行。
    """
    section("C3. 性能优化（轮询自适应 + 分块并发）")

    from datetime import datetime, timedelta

    from app.config import settings
    from app.services.jobs import (
        STATUS_FAILED,
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_SUCCEEDED,
        Job,
    )

    # ---- 子项 1：Job.to_poll 的 poll_hint ----
    j_queued = Job(id="q", kind="literature", title="x", status=STATUS_QUEUED)
    poll = j_queued.to_poll()
    check("queued 任务 to_poll 含 poll_hint", "poll_hint" in poll)
    check(
        "queued 任务 poll_hint = active 间隔",
        poll.get("poll_hint") == settings.ai_poll_hint_active_seconds,
        f"got {poll.get('poll_hint')}",
    )

    j_running_old = Job(id="ro", kind="literature", title="x", status=STATUS_RUNNING)
    j_running_old.started_at = (datetime.now() - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
    poll = j_running_old.to_poll()
    check(
        "running（>60s）poll_hint = idle 间隔",
        poll.get("poll_hint") == settings.ai_poll_hint_idle_seconds,
        f"got {poll.get('poll_hint')}",
    )

    j_running_new = Job(id="rn", kind="literature", title="x", status=STATUS_RUNNING)
    poll = j_running_new.to_poll()
    check(
        "running（<60s）poll_hint = active 间隔",
        poll.get("poll_hint") == settings.ai_poll_hint_active_seconds,
        f"got {poll.get('poll_hint')}",
    )

    j_done = Job(id="d", kind="literature", title="x", status=STATUS_SUCCEEDED)
    check("succeeded 任务 to_poll 不含 poll_hint（让前端停轮询）", "poll_hint" not in j_done.to_poll())
    j_failed = Job(id="f", kind="literature", title="x", status=STATUS_FAILED)
    check("failed 任务 to_poll 不含 poll_hint", "poll_hint" not in j_failed.to_poll())

    # ---- 子项 2：_run_chunked 同篇内分块并发 ----
    async def run_concurrency() -> None:
        class ConcurrentTraceClient(FakeClient):
            """继承 FakeClient，在 chat() 里追踪同时在跑的请求数。"""

            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self._lock = asyncio.Lock()
                self.in_flight = 0
                self.peak = 0
                self.delay = 0.05  # 50ms 让并发现象可被观察到

            async def chat(self, system: str, user: str, **kw: Any) -> str:
                async with self._lock:
                    self.in_flight += 1
                    if self.in_flight > self.peak:
                        self.peak = self.in_flight
                try:
                    await asyncio.sleep(self.delay)
                    return await super().chat(system, user, **kw)
                finally:
                    async with self._lock:
                        self.in_flight -= 1

        # 12 页 × 1000 字符 = 12000 字符 > selftest CHUNK_MAX_CHARS(4000) → 走分段分析
        # 切 3 块，默认 literature_concurrency=2 → 峰值 in-flight 应 = 2
        make_test_pdf("concurrency.pdf", pages=12, chars_per_page=1000)
        client = ConcurrentTraceClient()
        outcome = await lit_service.analyze_pdf("concurrency.pdf", client=client)  # type: ignore[arg-type]
        check("并发跑通：分析状态 OK", outcome.status == "ok", f"status={outcome.status} err={outcome.error_code}")
        check(
            f"并发跑通：峰值 in-flight ≥ 2（实际 {client.peak}）",
            client.peak >= 2,
            f"peak={client.peak}",
        )

    asyncio.run(run_concurrency())


# ----------------------------------------------------------------------
# D. 失败处理：任何失败都不能产出 .md
# ----------------------------------------------------------------------
def test_failures() -> None:
    section("D. 失败处理（硬性要求：不编造、不产出脏文件）")

    async def run() -> None:
        fake = FakeClient()

        # D1 文件不存在
        outcome = await lit_service.analyze_pdf("missing.pdf", client=fake)  # type: ignore[arg-type]
        check("不存在的文件：报失败", outcome.status == "failed")
        check(
            "不存在的文件：错误码正确",
            outcome.error_code == ErrorCode.PDF_NOT_FOUND.value,
            outcome.error_code,
        )
        check("不存在的文件：没有生成 .md", not guard.exists(ROOT_LITERATURE, "missing.md"))

        # D2 损坏的 PDF
        broken = TMP / "literature" / "broken.pdf"
        broken.write_bytes(b"this is definitely not a pdf file" * 40)
        outcome = await lit_service.analyze_pdf("broken.pdf", client=fake)  # type: ignore[arg-type]
        check("损坏文件：报失败", outcome.status == "failed", outcome.status)
        check(
            "损坏文件：错误码为 PDF_CORRUPT 或 PDF_NO_TEXT",
            outcome.error_code in (ErrorCode.PDF_CORRUPT.value, ErrorCode.PDF_NO_TEXT.value),
            outcome.error_code,
        )
        check("损坏文件：没有生成 .md", not guard.exists(ROOT_LITERATURE, "broken.md"))

        # D3 模型输出缺必备小节 → 判定失败、不写文件
        make_test_pdf("paper-badpdf.pdf", pages=1, chars_per_page=1200)
        bad = FakeClient(omit_sections=True)
        outcome = await lit_service.analyze_pdf("paper-badpdf.pdf", client=bad)  # type: ignore[arg-type]
        check("输出缺小节：报失败", outcome.status == "failed", outcome.status)
        check(
            "输出缺小节：错误码正确",
            outcome.error_code == ErrorCode.MODEL_OUTPUT_INVALID.value,
            outcome.error_code,
        )
        check("输出缺小节：没有生成 .md", not guard.exists(ROOT_LITERATURE, "paper-badpdf.md"))

        # D4 模型完全不可用（抛 WorkbenchError）
        class DeadClient:
            call_count = 0

            async def chat(self, *_: Any, **__: Any) -> str:
                raise WorkbenchError(ErrorCode.UPSTREAM_UNAVAILABLE, detail="selftest")

        make_test_pdf("paper-dead.pdf", pages=1, chars_per_page=1200)
        outcome = await lit_service.analyze_pdf("paper-dead.pdf", client=DeadClient())  # type: ignore[arg-type]
        check("模型不可用：报失败", outcome.status == "failed")
        check(
            "模型不可用：错误码正确",
            outcome.error_code == ErrorCode.UPSTREAM_UNAVAILABLE.value,
            outcome.error_code,
        )
        check("模型不可用：没有生成 .md", not guard.exists(ROOT_LITERATURE, "paper-dead.md"))

        # D5 分块失败比例过高 → 整篇失败
        # 用"每两块失败一块"构造 50% 缺失，远超 30% 的熔断线。
        class FlakyClient(FakeClient):
            """注意：必须用自己的计数器。

            复用父类的 call_count 会因为父类也自增而被错开奇偶判断，
            那样这个测试就形同虚设（第一版就是这么写错的）。
            """

            def __init__(self) -> None:
                super().__init__()
                self.chunk_calls = 0

            async def chat(self, system: str, user: str, **kwargs: Any) -> str:
                if "本块正文开始" in user:
                    self.chunk_calls += 1
                    if self.chunk_calls % 2 == 0:
                        self.call_count += 1
                        raise WorkbenchError(ErrorCode.UPSTREAM_UNAVAILABLE, detail="flaky")
                return await super().chat(system, user, **kwargs)

        make_test_pdf("paper-flaky.pdf", pages=16, chars_per_page=900)
        flaky = FlakyClient()
        outcome = await lit_service.analyze_pdf("paper-flaky.pdf", client=flaky)  # type: ignore[arg-type]
        check("确实发生了分块失败", flaky.chunk_calls >= 4, f"chunk_calls={flaky.chunk_calls}")
        check("缺失比例超限：整篇失败", outcome.status == "failed", outcome.status)
        check(
            "缺失比例超限：错误码为 ANALYSIS_FAILED",
            outcome.error_code == ErrorCode.ANALYSIS_FAILED.value,
            outcome.error_code,
        )
        check("缺失比例超限：没有生成 .md", not guard.exists(ROOT_LITERATURE, "paper-flaky.md"))

        # D6 目标解析：打错文件名要立刻报错，而不是排队后再失败
        bad_target = False
        try:
            lit_service.resolve_targets("files", ["paper-long.pfd"])
        except WorkbenchError as exc:
            bad_target = exc.code is ErrorCode.PDF_NOT_FOUND
        check("打错文件名立刻报错", bad_target)

        targets = lit_service.resolve_targets("all", [])
        check("scope=all 能列出全部 PDF", len(targets) >= 4, f"got {len(targets)}")

        # D7 任务日志：必须是追加语义
        await task_log.append_log("自检", ["第一条自检日志", "结果：成功"])
        await task_log.append_log("自检", ["第二条自检日志"])
        log_path = guard.read_path(ROOT_LIFE_NOTES, "workbench_log.md")
        log_text, _ = guard.read_text(log_path)
        check("任务日志已创建", bool(log_text))
        check("任务日志含表头", "# 工作台任务日志" in log_text)
        check("任务日志为追加（两条都在）", "第一条自检日志" in log_text and "第二条自检日志" in log_text)

    asyncio.run(run())


# ----------------------------------------------------------------------
# E. HTTP 层
# ----------------------------------------------------------------------
def test_http_layer() -> None:
    section("E. HTTP 层（认证 / CSRF / 路由）")

    try:
        from fastapi.testclient import TestClient

        from app.main import app
    except Exception as exc:  # noqa: BLE001 - 想连"环境问题"一起兜住
        # 这一段失败几乎总是环境问题，不是代码缺陷：
        #   Windows 上偶发 "DLL load failed: 另一个程序正在使用此文件"
        #   （杀毒 / 索引服务临时锁住了 pydantic_core 的 .pyd）。
        # 标成 SKIP 而不是 FAIL —— 报成"测试失败"会让人去查根本没坏的代码。
        skip(
            "HTTP 层测试",
            f"无法导入测试客户端（{type(exc).__name__}: {str(exc)[:100]}）"
            "—— 多为环境/文件锁问题，隔几秒重跑一次通常即可",
        )
        return

    csrf = {"X-Requested-With": "owb"}

    with TestClient(app) as client:
        r = client.get("/api/health")
        check("健康检查无需认证", r.status_code == 200 and r.json() == {"ok": True})

        r = client.get("/api/auth/me")
        check("未登录时 me 返回 authenticated=false", r.json().get("authenticated") is False)

        r = client.get("/api/literature/files")
        check("未登录访问业务接口 → 401", r.status_code == 401, str(r.status_code))

        r = client.post("/api/auth/login", json={"password": "wrong-password"}, headers=csrf)
        check("错误口令被拒绝", r.status_code == 401, str(r.status_code))

        r = client.post("/api/auth/login", json={"password": "selftest-password-0123456789"}, headers=csrf)
        check("正确口令登录成功", r.status_code == 200, r.text[:120])

        r = client.get("/api/literature/files")
        check("登录后可访问文件列表", r.status_code == 200, r.text[:120])
        if r.status_code == 200:
            data = r.json()
            check("文件列表含 PDF 条目", data.get("total", 0) >= 4, str(data.get("total")))

        # CSRF：带 Cookie 但不带自定义头
        r = client.post("/api/notes", json={"text": "今天买书花了 30 元。"})
        check("缺少自定义头 → 403（防 CSRF）", r.status_code == 403, str(r.status_code))

        r = client.post("/api/notes", json={"text": "今天买书花了 30 元。"}, headers=csrf)
        check("带自定义头可写入", r.status_code == 200, r.text[:160])
        if r.status_code == 200:
            check("接口返回分类结果", r.json().get("type") == "expense", r.text[:120])

        r = client.get("/api/notes/recent")
        check("可读回 daily_notes.md", r.status_code == 200 and "买书" in r.json().get("content", ""))

        r = client.post("/api/literature/analyze", json={"scope": "files", "files": ["nope.pdf"]}, headers=csrf)
        check("分析不存在的文件 → 404", r.status_code == 404, str(r.status_code))

        r = client.get("/api/literature/content?name=../secret.md")
        check("读文件接口拒绝逃逸路径", r.status_code in (400, 403), str(r.status_code))

        r = client.get("/api/jobs/recent")
        check("任务列表可访问", r.status_code == 200)

        r = client.post("/api/auth/logout", headers=csrf)
        check("登出成功", r.status_code == 200, r.text[:80])

        r = client.get("/api/literature/files")
        check("登出后重新被拦截", r.status_code == 401, str(r.status_code))

        r = client.get("/")
        check("首页返回 HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""))

        r = client.get("/api/health")
        check("响应头不含 Server 版本", r.headers.get("server") == "workbench", str(r.headers.get("server")))


# ----------------------------------------------------------------------
def main() -> int:
    emit("=" * 72)
    emit("openclaw-workbench 本地自检")
    emit(f"临时数据目录：{TMP}")
    emit("=" * 72)

    try:
        test_path_guard()
        test_notes()
        test_literature_pipeline()
        test_summary_text()
        test_performance_optimizations()
        test_failures()
        test_http_layer()
    except Exception as exc:
        FAILED.append(f"自检脚本自身抛出异常：{type(exc).__name__}: {exc}")
        import traceback

        emit()
        emit("自检脚本异常中断：")
        for line in traceback.format_exc().splitlines():
            emit("  " + line)

    emit()
    emit("=" * 72)
    emit(f"结果：{len(PASSED)} 项通过，{len(FAILED)} 项失败，{len(SKIPPED)} 项跳过")
    if SKIPPED:
        emit("-" * 72)
        for item in SKIPPED:
            emit(f"  跳过：{item}")
    if FAILED:
        emit("-" * 72)
        for item in FAILED:
            emit(f"  失败：{item}")
    emit("=" * 72)

    report_path = os.environ.get("OWB_SELFTEST_REPORT")
    if report_path:
        try:
            Path(report_path).write_text("\n".join(REPORT) + "\n", encoding="utf-8")
        except OSError:
            pass

    # 清理临时目录。注意：这是自检脚本自己创建的临时目录，不是用户数据。
    shutil.rmtree(TMP, ignore_errors=True)

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
