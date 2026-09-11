#!/usr/bin/env python3
"""OpenClaw 连通性自检 —— 部署时第一个该跑的脚本。

它会依次检查：
  1. 配置是否齐全（地址 / 令牌 / 模型标识）
  2. 网络是否可达
  3. /chat/completions 端点是否存在且启用
  4. 请求是否被接受（模型标识是否正确）
  5. 模型确实返回了内容

用法：

    # 在 NAS 上（容器内执行，用的是容器里那份配置）
    docker exec -it workbench python /app/scripts/check_openclaw.py

    # 在开发机上（.env 或环境变量已设置）
    python scripts/check_openclaw.py

这个脚本【只读】：它不修改 OpenClaw 的任何配置，也不写入任何文件。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.openclaw import client  # noqa: E402


def _describe_secret(secret: str) -> str:
    """只报告"配了没有、多长"。

    刻意不打印掩码：本脚本的输出经常被贴到聊天里求助，
    露出首尾几位 + 长度同样是泄露。见 app/main.py 里同名函数的说明。
    """
    if not secret:
        return "(未配置)"
    return f"已配置（长度 {len(secret)}）"


async def main() -> int:
    print("=" * 72)
    print("OpenClaw 连通性自检")
    print("=" * 72)
    print(f"目标地址   : {settings.openclaw_chat_url}")
    print(f"访问令牌   : {_describe_secret(settings.openclaw_token)}")
    print(f"模型标识   : {settings.openclaw_model}")
    print(f"指定 agent : {settings.openclaw_agent_id or '(未指定)'}")
    print(f"单次超时   : {settings.openclaw_timeout_seconds} 秒")
    print("-" * 72)

    result = await client.probe()
    print(result.render())
    print("-" * 72)

    if result.ok:
        print("结论：OpenClaw 侧已就绪，工作台可以调用。")
        print()
        print("建议的下一步：")
        print("  1) 在界面上提交一篇短文献，确认整条链路（PDF → 模型 → Markdown）通畅")
        print("  2) 部署完成后再考虑给工作台建一个专用受限 agent（见 docs/OPENCLAW-API.md）")
        await client.aclose()
        return 0

    print("结论：尚不可用。请按上面的提示排查。")
    print()
    print("最常见的三种原因：")
    print("  ① OPENCLAW_BASE_URL 填了 127.0.0.1")
    print("     → 容器里的 127.0.0.1 是容器自己，不是 NAS。必须填 NAS 的局域网 IP。")
    print("  ② OpenClaw 未启用 chatCompletions 端点（返回 404 / 405）")
    print("     → 需要在 OpenClaw 配置里开启 gateway.http.endpoints.chatCompletions.enabled")
    print("       这是修改 OpenClaw 配置，工作台不会替你改。详见 docs/OPENCLAW-API.md。")
    print("  ③ 令牌不对（返回 401 / 403）或模型标识不被接受（返回 400）")
    print("     → 核对 OPENCLAW_TOKEN 与 OPENCLAW_MODEL。")
    await client.aclose()
    return 1


if __name__ == "__main__":
    # 输出既打到终端，也可选写一份 UTF-8 报告文件。
    # 为什么要文件：Windows 终端转发中文时的编码不稳定，而这份报告恰恰是
    # "出问题时最该保存下来"的东西，不能是乱码。
    #   NAS 上： docker exec -it workbench python /app/scripts/check_openclaw.py
    #   写文件：OWB_REPORT=./check.txt python scripts/check_openclaw.py
    import contextlib
    import io
    import os

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = asyncio.run(main())

    text = buffer.getvalue()
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("utf-8", "replace").decode("ascii", "replace"))

    report = os.environ.get("OWB_REPORT")
    if report:
        Path(report).write_text(text, encoding="utf-8")

    raise SystemExit(code)
