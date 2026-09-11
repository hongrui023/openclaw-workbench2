#!/usr/bin/env python3
"""容器健康检查（供 Docker HEALTHCHECK 或手动使用）。"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("WORKBENCH_PORT", "8080")


def main() -> int:
    url = f"http://127.0.0.1:{PORT}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"unhealthy: {type(exc).__name__}", file=sys.stderr)
        return 1
    # /api/health 只返回 {"ok":true}，这里不做任何额外信息输出
    if '"ok"' in body and "true" in body:
        print("healthy")
        return 0
    print("unhealthy: unexpected body", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
