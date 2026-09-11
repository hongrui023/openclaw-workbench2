"""功能二：生活记录。

    life_notes/daily_notes.md   ← 待办 / 收支 / 随笔 统一追加到这里

分类策略是"两段式"（对应方案文档 7.1 节）：

  第一段：规则。金额 + 收支动词、待办关键词，用正则就能高置信判定。
          **命中就直接分类，不调用 AI**——零延迟、零 Token 成本。
          记账和待办是你最高频的两类操作，没必要每次都走一遍网络。
  第二段：规则不确定时才问模型，要求返回严格 JSON。
          模型也不可用时，**归为"随笔"并原样记录**——
          宁可归错类，也不能丢数据。

写入永远是**纯追加**：
  - 不重写文件、不重排已有条目
  - 只在文件尾部追加新内容（必要时补一个新的日期标题或小节标题）
  - 写完 fsync，避免 NAS 断电丢记录
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from app.errors import ErrorCode, WorkbenchError
from app.obs import clock_str, get_logger, now, today_str
from app.security.paths import ROOT_LIFE_NOTES, guard
from app.services import prompts
from app.services.openclaw import OpenClawClient, client as default_client

log = get_logger()

DAILY_FILENAME = "daily_notes.md"
_DAILY_HEADER = "# 生活记录\n\n> 由 openclaw-workbench 追加记录。\n"

TYPE_TODO = "todo"
TYPE_EXPENSE = "expense"
TYPE_NOTE = "note"

SECTION_OF = {TYPE_TODO: "待办", TYPE_EXPENSE: "收支", TYPE_NOTE: "随笔"}
LABEL_OF = {TYPE_TODO: "待办事项", TYPE_EXPENSE: "收支记账", TYPE_NOTE: "随笔"}

_amount_re = re.compile(r"(?P<num>\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|人民币|rmb|RMB|¥|￥)")

# 词表单独定义，同时用于「匹配」和「摘要清洗」两处，避免两套规则走偏
_OUT_WORDS = (
    "消费", "支出", "开销", "请客", "打车", "缴费", "交费", "汇款",
    "花", "买", "购", "付", "交", "充", "租",
)
_IN_WORDS = (
    "收入", "收到", "收款", "进账", "入账", "到账", "报销", "退款",
    "退回", "工资", "赚", "中奖", "红包", "补贴", "奖学金", "退税",
)
_TODO_WORDS = (
    "待办", "todo", "记得", "别忘了", "别忘记", "提醒我", "要记得",
    "明天", "后天", "大后天", "下周", "下个月", "下周一", "下月",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
    "要去", "需要", "打算", "计划", "准备去",
)
_TEMPORAL_WORDS = (
    "今天", "昨天", "前天", "上午", "中午", "下午", "晚上", "刚刚",
    "刚才", "这周", "本周", "上周", "上个月", "这个月", "今早", "今晚",
)

# re.IGNORECASE 让 todo / TODO / Todo 统一命中
_out_re = re.compile("|".join(_OUT_WORDS), re.IGNORECASE)
_in_re = re.compile("|".join(_IN_WORDS), re.IGNORECASE)
_todo_re = re.compile("|".join(_TODO_WORDS), re.IGNORECASE)
_filler_re = re.compile(r"[，,。.！!？?；;：:\s]+")

# 单进程单 worker，用进程内锁即可（不引入 fcntl —— Windows 上没有）
_write_lock = asyncio.Lock()


@dataclass
class NoteResult:
    type: str
    label: str
    summary: str
    amount: float | None = None
    direction: str | None = None
    method: str = "rule"      # rule | model | fallback
    entry: str = ""           # 实际追加的 Markdown 片段
    filename: str = DAILY_FILENAME

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type,
            "label": self.label,
            "summary": self.summary,
            "amount": self.amount,
            "direction": self.direction,
            "method": self.method,
            "path": self.filename,
            "entry": self.entry,
        }


# ----------------------------------------------------------------------
# 第一段：规则分类
# ----------------------------------------------------------------------
def _rule_classify(text: str) -> tuple[str, float | None, str | None] | None:
    """返回 (类型, 金额, 方向) 或 None（表示规则不确定，需要问模型）。"""
    amount_match = _amount_re.search(text)
    if amount_match:
        amount = float(amount_match.group("num"))
        if amount <= 0:
            return None
        # 有金额 + 有收支动词 → 高置信
        has_out = bool(_out_re.search(text))
        has_in = bool(_in_re.search(text))
        if has_out or has_in:
            if has_in and not has_out:
                direction = "in"
            elif has_out and not has_in:
                direction = "out"
            else:
                # 两者都有：以出现位置靠后者为准（"花了 300 元，后来退款 100 元"）
                direction = "out" if text.rfind(_out_re.search(text).group(0)) > text.rfind(
                    _in_re.search(text).group(0)
                ) else "in"
            return TYPE_EXPENSE, amount, direction
        # 有金额但没有任何收支动词：可能是"工资 8000"这类，判断不了方向 → 交给模型
        return None

    if _todo_re.search(text) and len(text) <= 200:
        return TYPE_TODO, None, None

    return None


# 只用于「摘要清洗」的词表。
#
# 关键区别（这里很容易写错）：
#   - 「花了 / 收到 / 支出」这类词描述的是"发生了一笔收付"这件事本身，
#     不属于"花在哪"的信息，所以从摘要里去掉；
#   - 「买 / 交 / 充 / 购」这类词描述的是**事项本身**，必须保留。
#     「今天买实验耗材花了 280 元」的摘要是「买实验耗材」，不是「实验耗材」。
_SUMMARY_STRIP_WORDS = (
    "花了", "花掉", "花费", "花销", "开销", "支出", "消费了",
    "支付了", "付了", "付款", "交了", "缴费", "交费",
    "收到", "收了", "收入", "进账", "入账", "到账", "报销了",
)
_TAIL_PARTICLES = "的了个着"


def _summarize_expense(text: str) -> str:
    """从原始句子里抽出"买了什么"，而不是把整句塞进表格。

    「今天买实验耗材花了 280 元。」→「买实验耗材」
    """
    stripped = _amount_re.sub(" ", text)
    for word in sorted(_SUMMARY_STRIP_WORDS, key=len, reverse=True):
        stripped = stripped.replace(word, " ")
    for word in _TEMPORAL_WORDS:
        stripped = stripped.replace(word, " ")
    stripped = _filler_re.sub(" ", stripped)

    # 逐词清掉残留的虚词（"50 元的移液枪头" 去掉金额后会剩下一个孤立的"的"）
    tokens = []
    for token in stripped.split(" "):
        token = token.strip(_TAIL_PARTICLES)
        if token:
            tokens.append(token)
    summary = " ".join(tokens).strip(_TAIL_PARTICLES + " ")
    return summary or _normalize_plain(text)


def _normalize_plain(text: str) -> str:
    return _filler_re.sub(" ", text).strip() or text.strip()


# ----------------------------------------------------------------------
# 第二段：模型兜底
# ----------------------------------------------------------------------
async def _model_classify(
    text: str, client: OpenClawClient
) -> tuple[str, float | None, str | None, str] | None:
    prompt = prompts.load("note_classify", content=text, current_time=now().strftime("%Y-%m-%d %H:%M"))
    try:
        data = await client.chat_json(prompts.guardrail(), prompt)
    except WorkbenchError as exc:
        log.info("生活记录分类调用失败，回退为随笔：code=%s", exc.code.value)
        return None

    raw_type = str(data.get("type", "")).strip().lower()
    if raw_type in ("expense", "记账", "收支"):
        note_type = TYPE_EXPENSE
    elif raw_type in ("todo", "待办", "任务"):
        note_type = TYPE_TODO
    elif raw_type in ("note", "随笔", "想法", "记录"):
        note_type = TYPE_NOTE
    else:
        return None

    amount: float | None = None
    raw_amount = data.get("amount")
    if isinstance(raw_amount, (int, float)) and float(raw_amount) > 0:
        amount = float(raw_amount)
    elif isinstance(raw_amount, str):
        found = _amount_re.search(raw_amount) or re.search(r"\d+(?:\.\d{1,2})?", raw_amount)
        if found:
            try:
                value = float(found.group(0).rstrip("元块"))
                amount = value if value > 0 else None
            except ValueError:
                amount = None

    direction = str(data.get("direction", "")).strip().lower()
    if direction not in ("in", "out"):
        direction = None

    summary = str(data.get("summary", "")).strip()

    # 校验模型的自相矛盾：说是记账却没有金额 → 降级为随笔，不硬凑
    if note_type == TYPE_EXPENSE and amount is None:
        log.info("模型判定为记账但未给出金额，降级为随笔：%s", _brief(text))
        return TYPE_NOTE, None, None, summary or _normalize_plain(text)

    if note_type == TYPE_EXPENSE and direction is None:
        direction = "out"

    return note_type, amount, direction, summary


def _brief(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# ----------------------------------------------------------------------
# Markdown 组装
# ----------------------------------------------------------------------
def _format_amount(amount: float) -> str:
    return f"¥{amount:,.2f}"


def _entry_line(note_type: str, summary: str, amount: float | None, direction: str | None) -> str:
    time_label = clock_str()
    if note_type == TYPE_EXPENSE:
        arrow = "收入" if direction == "in" else "支出"
        return f"| {time_label} | {arrow} | {_format_amount(amount or 0)} | {summary} |"
    if note_type == TYPE_TODO:
        return f"- [ ] {summary}  <!-- {time_label} -->"
    return f"- {time_label} {summary}"


_EXPENSE_TABLE_HEADER = "| 时间 | 方向 | 金额 | 说明 |\n|---|---|---|---|"


def _tail_state(tail: str) -> tuple[str | None, str | None]:
    """从文件尾部判断：当前日期标题是哪天、最后一个小节是什么。"""
    date_at = tail.rfind("\n## ")
    section_at = tail.rfind("\n### ")

    last_date: str | None = None
    if date_at != -1:
        line = tail[date_at + 1 :].split("\n", 1)[0]
        last_date = line[2:].strip()

    last_section: str | None = None
    if section_at != -1 and (date_at == -1 or section_at > date_at):
        line = tail[section_at + 1 :].split("\n", 1)[0]
        last_section = line[3:].strip()

    return last_date, last_section


def _build_block(note_type: str, entry: str, last_date: str | None, last_section: str | None) -> str:
    """决定这次追加需要带哪些标题——保持纯追加语义，同时让文件读起来有结构。"""
    today = today_str()
    section = SECTION_OF[note_type]

    need_date = last_date != today
    need_section = need_date or last_section != section

    parts: list[str] = []
    if need_date:
        parts.append(f"\n## {today}")
    if need_section:
        parts.append(f"\n### {section}")
        if note_type == TYPE_EXPENSE:
            parts.append(_EXPENSE_TABLE_HEADER)
        parts.append(entry)
        return "\n".join(parts) + "\n"

    # 同一天、同一小节：直接续上
    parts.append(entry)
    return "\n".join(parts) + "\n"


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
async def add_note(text: str, *, client: OpenClawClient | None = None) -> NoteResult:
    client = client or default_client
    text = (text or "").strip()
    if not text:
        raise WorkbenchError(ErrorCode.BAD_REQUEST, "记录内容不能为空。")

    # ---- 分类 ----
    method = "rule"
    rule = _rule_classify(text)
    summary = ""
    if rule is not None:
        note_type, amount, direction = rule
        if note_type == TYPE_EXPENSE:
            summary = _summarize_expense(text)
        else:
            summary = _normalize_plain(text)
    else:
        model = await _model_classify(text, client)
        if model is None:
            method = "fallback"
            note_type, amount, direction = TYPE_NOTE, None, None
            summary = _normalize_plain(text)
        else:
            method = "model"
            note_type, amount, direction, summary = model
            if note_type == TYPE_EXPENSE and not summary:
                summary = _summarize_expense(text)
            if note_type != TYPE_EXPENSE:
                summary = summary or _normalize_plain(text)

    entry = _entry_line(note_type, summary, amount, direction)

    # ---- 追加 ----
    async with _write_lock:
        path = guard.write_path(ROOT_LIFE_NOTES, DAILY_FILENAME)
        guard.create_text_if_absent(path, _DAILY_HEADER)
        tail = guard.read_tail(path, max_bytes=8192)
        last_date, last_section = _tail_state(tail)
        block = _build_block(note_type, entry, last_date, last_section)
        guard.append_text(path, block)

    log.info("生活记录已追加：type=%s method=%s amount=%s", note_type, method, amount)

    return NoteResult(
        type=note_type,
        label=LABEL_OF[note_type],
        summary=summary,
        amount=amount,
        direction=direction,
        method=method,
        entry=block.strip(),
    )


def read_recent(filename: str, max_chars: int) -> tuple[str, bool]:
    """读 daily_notes.md / workbench_log.md 的尾部，用于界面回显。

    只读不写；文件不存在时返回空串而不是报错——首次使用时就该是空的。
    """
    if filename not in (DAILY_FILENAME, "workbench_log.md"):
        raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
    try:
        path = guard.read_path(ROOT_LIFE_NOTES, filename)
    except WorkbenchError:
        return "", False
    text, truncated = guard.read_text(path, max_chars=max_chars)
    return text, truncated
