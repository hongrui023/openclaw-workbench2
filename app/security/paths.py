"""★ 路径守卫：工作台【唯一】允许触碰文件系统的模块。

设计契约（改这个文件前请先读完）：

1. 除本模块外，项目其他任何位置都不得出现 `open(` / `Path.write_text` /
   `shutil` / `os.remove` 等文件操作。审计方法：
       grep -rn "open(" app/ | grep -v "security/paths.py"
   应当没有结果。

2. 本模块【不实现也不会实现】任何删除、移动、重命名能力。
   代码里不存在 unlink / rmtree / remove / rename 的调用。
   这不是"忘了加"，这是有意的设计——没有这个能力，就不可能在程序里误删。

3. 白名单只有两个根：literature（只读 PDF / 写 MD）与 life_notes（读写 MD）。

4. 判断"是否在根内"必须在 os.path.realpath() 之后做。
   只做字符串前缀检查会被符号链接绕过：
       literature/evil.pdf -> /etc/passwd
   这类软链接的前缀是合法的，但解引用后跑到了根外。这是最容易漏的漏洞。

5. 所有函数只接受【文件名或相对路径】，绝不接受绝对路径。
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from app.errors import ErrorCode, WorkbenchError
from app.obs import get_logger

log = get_logger()

ROOT_LITERATURE = "literature"
ROOT_LIFE_NOTES = "life_notes"

# 后缀白名单：能读什么、能写什么，是分开的
READABLE_SUFFIXES: dict[str, set[str]] = {
    ROOT_LITERATURE: {".pdf", ".md"},
    ROOT_LIFE_NOTES: {".md"},
}
WRITABLE_SUFFIXES: dict[str, set[str]] = {
    ROOT_LITERATURE: {".md"},
    ROOT_LIFE_NOTES: {".md"},
}

# 单段文件名（不是整个路径）中不允许出现的字符。
# 说明：允许中文、空格、括号、连字符等常见字符——NAS 上的论文文件名经常有这些。
# 拒绝的是有真实风险的：路径分隔符（已单独处理）、Windows 数据流冒号、
# 通配符与引号、NUL 与控制字符。
_FORBIDDEN_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

_MAX_SEGMENT_BYTES = 200  # ext4 上限 255 字节，留出余量
_MAX_PATH_SEGMENTS = 4


@dataclass(frozen=True)
class FileEntry:
    """literature 目录下的一份文献。绝对路径不出现在 API 响应中。"""

    name: str
    size_bytes: int
    mtime: float
    has_markdown: bool


def _inside(path: str, root_real: str) -> bool:
    """真实的"在根内"判断，输入应当是已 realpath 的绝对路径。

    为什么要 normpath + normcase，而不是直接拼 os.sep：
      - normpath 让分隔符风格一致（Windows 上传入 "/data/x" 也能正确比较），
        在 Linux 上它是**恒等变换**，不会破坏含反斜杠的合法文件名；
      - normcase 在 Windows 上统一大小写（NTFS 不区分大小写），
        在 Linux 上同样是恒等变换。
      不能改用"把 \\ 全替换成 /"的做法——Linux 上反斜杠是合法文件名字符，
      那样替换会制造出误判为根内的漏洞。

    末尾必须用 root + os.sep 比较，否则 "/data/literature-secret"
    会被 startswith("/data/literature") 误判为在根内（前缀陷阱）。
    """
    candidate = os.path.normcase(os.path.normpath(path))
    root = os.path.normcase(os.path.normpath(root_real))
    if candidate == root:
        return True
    return candidate.startswith(root + os.sep)


# Windows 的重解析点类型（非 Windows 上这两个常量不存在，用字面值兜底）
_REPARSE_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
_REPARSE_SYMLINK = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)


def _is_link_like(path: str) -> bool:
    """路径本身是否是"重定向"——符号链接、Windows junction、挂载点。

    为什么不能只用 os.path.islink()：
      Windows 上 junction（目录联接）的 reparse tag 是 MOUNT_POINT，不是
      SYMLINK，因此 os.path.islink() 对它返回 **False**。而 junction 恰恰是
      Windows 上最容易、最不需要权限就能造出来的"指向别处"的东西。
      只判断 islink 会在 Windows 上留下一个真实的绕过口子。

      st_reparse_tag 只在 Windows 的 os.stat_result 上存在，
      在 Linux 上 getattr 兜底为 0，不影响 Linux 行为。
    """
    try:
        st = os.lstat(path)
    except OSError:
        # 路径不存在：交给后面的存在性检查处理
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in (_REPARSE_MOUNT_POINT, _REPARSE_SYMLINK)


class PathGuard:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots: dict[str, Path] = {}
        for name, root in roots.items():
            resolved = Path(os.path.realpath(str(root)))
            self._roots[name] = resolved

    # ------------------------------------------------------------------
    # 内部：名称校验
    # ------------------------------------------------------------------
    def root(self, root_name: str) -> Path:
        if root_name not in self._roots:
            raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)
        return self._roots[root_name]

    def root_real(self, root_name: str) -> str:
        return os.path.realpath(str(self.root(root_name)))

    def _clean_relative(self, relative: str) -> tuple[str, list[str]]:
        """把用户输入变成安全的相对路径。任何可疑成分直接拒绝，不做"修复式"猜测。"""
        if not isinstance(relative, str) or not relative.strip():
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)

        raw = relative.strip()

        # NUL 截断攻击
        if "\x00" in raw:
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)

        # 统一分隔符：Windows 风格的反斜杠按分隔符处理，而不是当成普通字符
        raw = raw.replace("\\", "/")

        # 绝对路径
        if raw.startswith("/"):
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
        # 盘符（C:/...）与 UNC（//server/share，已由上面的 / 判断拦下）
        if _WINDOWS_DRIVE.match(raw):
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
        # 家目录展开
        if raw.startswith("~"):
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)

        segments = [seg for seg in raw.split("/") if seg not in ("",)]
        if not segments or len(segments) > _MAX_PATH_SEGMENTS:
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)

        for seg in segments:
            if seg in (".", ".."):
                raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
            if _FORBIDDEN_CHARS.search(seg):
                raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
            # Windows 会静默去掉结尾的点和空格，导致"写入的文件名与预期不同"
            if seg != seg.rstrip(" ."):
                raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
            if len(seg.encode("utf-8", "surrogatepass")) > _MAX_SEGMENT_BYTES:
                raise WorkbenchError(ErrorCode.PATH_BAD_NAME)

        return "/".join(segments), segments

    @staticmethod
    def _check_suffix(root_name: str, filename: str, allowed: dict[str, set[str]]) -> str:
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in allowed.get(root_name, set()):
            raise WorkbenchError(ErrorCode.PATH_BAD_SUFFIX)
        return suffix

    def _assert_no_symlink(self, root_real: str, segments: list[str], root_name: str) -> None:
        """数据目录内不允许出现任何"重定向"——与平台无关的硬规则。

        覆盖对象：Linux 符号链接、Windows junction、其他重解析点（见 _is_link_like）。

        为什么不能只靠 realpath：
          realpath 校验在 Linux（也就是 NAS 上跑的那个内核）确实能挡住软链接逃逸，
          但"依赖某个平台的行为"这件事本身不该赌：Windows 上 junction 对
          os.path.islink() 返回 False，只判断 islink 会留下一个真实的绕过口子。
          而 literature / life_notes 里本来就没有任何理由放链接，
          所以直接一刀切禁止：既简单，又不用管平台差异。

        这一道与后面那道 realpath 校验是【互相独立】的：
        即便某一平台上第一道失效，第二道仍能兜住（反之亦然）。
        """
        current = root_real
        for segment in segments:
            current = os.path.join(current, segment)
            if _is_link_like(current):
                log.warning("拒绝链接：root=%s 输入段=%s", root_name, segment)
                raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)

    def _resolve(self, root_name: str, relative: str, *, allowed: dict[str, set[str]]) -> Path:
        # 先校验根名：未知根直接拒绝，不进入后续任何分支
        if root_name not in self._roots:
            raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)

        clean, segments = self._clean_relative(relative)
        suffix = self._check_suffix(root_name, clean, allowed)

        root_real = self.root_real(root_name)
        candidate = os.path.join(root_real, *segments)

        # 第一道：拒绝路径链上的任何符号链接（与平台无关）
        self._assert_no_symlink(root_real, segments, root_name)

        # 第二道：realpath 之后再判断。
        #   这同时覆盖了两种情况：
        #     a) candidate 本身是指向根外的软链接
        #     b) candidate 的上级目录是软链接（例如 literature/sub -> /etc）
        real = os.path.realpath(candidate)
        if not _inside(real, root_real):
            # 记日志但不回显具体路径——避免把 NAS 目录结构带出去
            log.warning(
                "路径逃逸被拒绝：root=%s 输入=%s suffix=%s", root_name, relative, suffix
            )
            raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)

        return Path(real)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def read_path(self, root_name: str, relative: str) -> Path:
        """解析一个【已存在】的可读文件。不存在则报错。"""
        path = self._resolve(root_name, relative, allowed=READABLE_SUFFIXES)
        if not path.is_file():
            code = ErrorCode.PDF_NOT_FOUND if root_name == ROOT_LITERATURE else ErrorCode.NOT_FOUND
            raise WorkbenchError(code)
        return path

    def write_path(self, root_name: str, relative: str) -> Path:
        """解析一个【待写入】的目标文件。目标可以不存在，但父目录必须在根内且存在。"""
        path = self._resolve(root_name, relative, allowed=WRITABLE_SUFFIXES)
        if path.is_dir():
            raise WorkbenchError(ErrorCode.PATH_BAD_NAME)
        parent = path.parent
        parent_real = os.path.realpath(str(parent))
        if not _inside(parent_real, self.root_real(root_name)):
            raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)
        if not parent.is_dir():
            # 不自动创建子目录——挂载点本身就应该是存在的那个目录
            raise WorkbenchError(ErrorCode.PATH_FORBIDDEN)
        return path

    def exists(self, root_name: str, relative: str) -> bool:
        try:
            path = self.read_path(root_name, relative)
        except WorkbenchError:
            return False
        return path.is_file()

    def ensure_root(self, root_name: str) -> None:
        """确保根目录存在。只创建根目录本身，绝不在其中创建或改动任何文件。"""
        root = self.root(root_name)
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            log.error("数据目录不可用：root=%s err=%s", root_name, type(exc).__name__)

    # ---- 目录列举（仅在用户显式请求时调用，没有任何后台扫描）----
    def list_literature(self) -> list[FileEntry]:
        root = self.root(ROOT_LITERATURE)
        root_real = self.root_real(ROOT_LITERATURE)
        entries: list[FileEntry] = []
        try:
            with os.scandir(root) as it:
                for item in it:
                    if not item.name.lower().endswith(".pdf"):
                        continue
                    try:
                        if item.is_symlink() or _is_link_like(item.path):
                            # 链接一律不进列表：避免"看起来在库里其实在库外"
                            continue
                        if not item.is_file(follow_symlinks=False):
                            continue
                        stat = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    stem = item.name[:-4]
                    md_name = f"{stem}.md"
                    md_path = os.path.join(root_real, md_name)
                    entries.append(
                        FileEntry(
                            name=item.name,
                            size_bytes=stat.st_size,
                            mtime=stat.st_mtime,
                            has_markdown=os.path.isfile(md_path),
                        )
                    )
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.error("列举 literature 失败：err=%s", type(exc).__name__)
            return []
        entries.sort(key=lambda e: e.name.lower())
        return entries

    # ---- 文本读写（唯一的落盘通道）----
    def read_text(self, path: Path, *, max_chars: int | None = None) -> tuple[str, bool]:
        """读文本。返回 (内容, 是否被截断)。写入侧的安全性由调用方已解析的 path 保证。"""
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.error("读文件失败：%s", type(exc).__name__)
            raise WorkbenchError(ErrorCode.NOT_FOUND) from exc
        text = data.decode("utf-8", errors="replace")
        truncated = False
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return text, truncated

    def read_tail(self, path: Path, *, max_bytes: int = 8192) -> str:
        """只读文件尾部。用于判断 daily_notes.md 的当前日期/小节状态，避免整文件读入。"""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - max_bytes)
                fh.seek(start)
                data = fh.read()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.error("读文件尾部失败：%s", type(exc).__name__)
            return ""
        if start > 0:
            # 从中间截断可能切掉半个多字节字符，丢掉第一行即可
            _, _, data = data.partition(b"\n")
        return data.decode("utf-8", errors="replace")

    def append_text(self, path: Path, text: str) -> int:
        """★ 追加写入。

        全项目唯一的写盘入口，且【只做追加】：
          - 不用 'w' 模式（会截断已有内容）
          - 不先读后写（会把整个文件重写一遍）
          - 写完 fsync，避免 NAS 断电/容器被杀时丢记录
        返回实际写入的字节数。
        """
        payload = text.encode("utf-8")
        try:
            with open(path, "ab") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.error("追加写入失败：%s", type(exc).__name__)
            raise WorkbenchError(
                ErrorCode.INTERNAL,
                "写入数据目录失败。请检查 NAS 上该目录的写入权限。",
                detail=f"{type(exc).__name__}",
            ) from exc
        return len(payload)

    def write_text_replace(self, path: Path, text: str) -> int:
        """★ 整体写入（会覆盖同名文件）。

        这是全项目【唯一】能覆盖已有文件内容的函数。使用约束必须严格遵守：

          - 只用于写入【工作台自己生成的】分析产物（literature/*.md）
          - 只有在用户显式勾选"强制重新分析"时才会走到这里
          - 内容先在内存里完整拼好，一次写入——避免中途失败留下半份文件
          - 绝不用于 daily_notes.md / workbench_log.md（那两者永远只有 append）

        为什么允许覆盖而不是"先删后写"：项目里不存在任何删除能力，
        覆盖是"重新分析"唯一可行的实现方式。副作用是：如果你手工编辑过
        某个 ABC.md，重新分析会覆盖掉你的编辑——这是刻意的取舍，
        因为"分析结果"的价值就在于它忠实对应当前那次分析。
        """
        payload = text.encode("utf-8")
        try:
            with open(path, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.error("写入文件失败：%s", type(exc).__name__)
            raise WorkbenchError(
                ErrorCode.INTERNAL,
                "写入文献目录失败。请检查 NAS 上 literature 目录的写入权限。",
                detail=f"{type(exc).__name__}",
            ) from exc
        return len(payload)

    def create_text_if_absent(self, path: Path, text: str) -> bool:
        """仅在文件不存在时创建（用于首次初始化 daily_notes.md 的标题行）。

        使用 'x' 模式：已存在则抛 FileExistsError，由调用方忽略。
        绝不覆盖任何已有内容。
        """
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            log.error("创建文件失败：%s", type(exc).__name__)
            raise WorkbenchError(
                ErrorCode.INTERNAL,
                "写入数据目录失败。请检查 NAS 上该目录的写入权限。",
                detail=f"{type(exc).__name__}",
            ) from exc

    # ---- 中间产物目录（容器可写层，容器重建即清空，永不需删除）----
    def ensure_work_dir(self, task_id: str) -> Path:
        """为一次任务准备中间产物目录。

        task_id 由工作台自己生成（uuid4().hex，仅十六进制字符），但仍走一次校验——
        "自己生成的就一定安全"是危险的假设。
        """
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(task_id))[:64]
        if not safe:
            raise WorkbenchError(ErrorCode.INTERNAL, detail="非法 task_id")
        base = Path(os.path.realpath(str(settings_work_dir())))
        target = base / safe
        os.makedirs(target, exist_ok=True)
        return target


def settings_work_dir() -> str:
    from app.config import settings as _settings

    return str(_settings.work_dir)


def build_default_guard() -> PathGuard:
    from app.config import settings as _settings

    return PathGuard(
        {
            ROOT_LITERATURE: _settings.literature_dir,
            ROOT_LIFE_NOTES: _settings.life_notes_dir,
        }
    )


# 全局单例。所有模块通过它访问文件。
guard = build_default_guard()
