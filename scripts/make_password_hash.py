#!/usr/bin/env python3
"""生成 WORKBENCH_PASSWORD_HASH（scrypt）。

用法：

    # 交互式（推荐：不回显、不留在 shell 历史里）
    python scripts/make_password_hash.py

    # 非交互（会留在命令历史里，谨慎使用）
    python scripts/make_password_hash.py --password '你的口令'

然后在极空间容器的「环境」页签里把结果填给 WORKBENCH_PASSWORD_HASH，
或写进 .env 的同一项。

口径建议：**至少 24 位随机字符串**。
不要用你平时在别的网站用的那个口令——外网入口目前是 HTTP（详见 docs/SECURITY.md）。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.auth import make_password_hash  # noqa: E402

MIN_LENGTH = 12
RECOMMENDED_LENGTH = 24


def _strength(password: str) -> str:
    kinds = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if len(password) >= RECOMMENDED_LENGTH and kinds >= 3:
        return "好"
    if len(password) >= 16 and kinds >= 2:
        return "尚可"
    return "偏弱"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 openclaw-workbench 的口令哈希")
    parser.add_argument("--password", help="直接指定口令（不推荐：会留在命令历史里）")
    args = parser.parse_args()

    if args.password:
        password = args.password
    else:
        password = getpass.getpass("请输入访问口令：")
        confirm = getpass.getpass("再输入一次确认：")
        if password != confirm:
            print("\n两次输入不一致，已取消。", file=sys.stderr)
            return 1

    if len(password) < MIN_LENGTH:
        print(f"\n口令太短（至少 {MIN_LENGTH} 位）。已取消。", file=sys.stderr)
        return 1

    strength = _strength(password)
    digest = make_password_hash(password)

    print()
    print("=" * 68)
    print("把它填到 WORKBENCH_PASSWORD_HASH：")
    print("=" * 68)
    print(digest)
    print("=" * 68)
    print(f"口令长度：{len(password)} 位    强度评估：{strength}")
    if strength != "好":
        print(
            f"建议改用 {RECOMMENDED_LENGTH} 位以上、混合大小写/数字/符号的随机串。"
            "生成一个可以用：python -c \"import secrets;print(secrets.token_urlsafe(24))\""
        )
    print()
    print("注意：这段哈希可以安全地放进 .env，但 .env 绝不能提交到 GitHub。")
    print("     明文口令只应存在于你的密码管理器里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
