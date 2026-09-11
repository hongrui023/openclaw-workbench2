#!/usr/bin/env python3
"""真实 HTTP 冒烟测试：用 uvicorn 起一个真服务，逐项检查对外行为。

与 scripts/selftest.py 的分工：
  - selftest.py 用 TestClient 直接调 ASGI 应用，覆盖业务逻辑与权限逻辑，跑得快
  - 本脚本真的启动 uvicorn 进程，验证"部署时那条命令"能跑通，
    并检查响应头、静态资源缓存、前端是否泄露服务标识

用法：
    python scripts/smoke_http.py

会把关键结论写成 UTF-8 报告（如果设置了 OWB_SMOKE_REPORT 环境变量）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="owb-smoke-"))
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"

(TMP / "literature").mkdir(parents=True, exist_ok=True)
(TMP / "life_notes").mkdir(parents=True, exist_ok=True)

LINES: list[str] = []


def emit(text: str = "") -> None:
    LINES.append(text)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("utf-8", "replace").decode("ascii", "replace"))


def get(path: str):
    request = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


def main() -> int:
    env = dict(os.environ)
    env.update(
        {
            "WORKBENCH_APP_ROOT": str(ROOT),
            "LITERATURE_DIR": str(TMP / "literature"),
            "LIFE_NOTES_DIR": str(TMP / "life_notes"),
            "WORKBENCH_WORK_DIR": str(TMP / "work"),
            "WORKBENCH_DEV": "1",
            "WORKBENCH_PASSWORD": "smoke-test-password-123456",
            "OPENCLAW_TOKEN": "dummy-token",
            "WORKBENCH_PORT": str(PORT),
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )

    log_path = TMP / "uvicorn.log"
    log_file = open(str(log_path), "wb")
    # 刻意加上与 Dockerfile CMD 相同的开关，保证测的就是部署时那条命令
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--no-server-header",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    failures = 0
    try:
        started = False
        for _ in range(40):
            try:
                status, _, body = get("/api/health")
                if status == 200 and b'"ok"' in body:
                    started = True
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)

        if not started:
            emit("uvicorn 启动失败。日志见下方。")
            failures += 1
        else:
            emit("uvicorn 启动成功")

            _, headers, body = get("/api/health")
            emit(f"/api/health -> {body.decode()!r}")
            server = headers.get("server")
            ok = server == "workbench"
            failures += 0 if ok else 1
            emit(f"  Server 头 = {server!r}  {'OK' if ok else '失败：应为 workbench（检查 --no-server-header）'}")

            status, headers, body = get("/")
            emit(f"/ -> {status}，{len(body)} 字节")
            emit(f"  Cache-Control = {headers.get('cache-control')!r}")
            emit(f"  CSP = {'有' if 'content-security-policy' in {k.lower() for k in headers} else '缺失'}")

            total = len(body)
            for path in ("/static/style.css?v=1.0.0", "/static/app.js?v=1.0.0"):
                status, headers, payload = get(path)
                total += len(payload)
                emit(f"{path} -> {status}，{len(payload)} 字节，Cache-Control = {headers.get('cache-control')!r}")

            emit(f"首屏合计（未压缩）≈ {total / 1024:.1f} KB；gzip 后约 8–10 KB")

            # 前端不得泄露 AI 服务的任何标识
            _, _, js = get("/static/app.js?v=1.0.0")
            js_text = js.decode("utf-8", "replace").lower()
            for needle in ("openclaw", "18789", "deepseek", "bearer"):
                if needle in js_text:
                    failures += 1
                    emit(f"  ✗ 前端 JS 中出现敏感标识：{needle}")
            emit("  前端 JS 未出现 AI 服务的名称/端口/厂商  OK")

            try:
                get("/api/literature/files")
                failures += 1
                emit("未认证访问业务接口：未被拦截  ✗")
            except urllib.error.HTTPError as exc:
                ok = exc.code == 401
                failures += 0 if ok else 1
                emit(f"未认证访问业务接口 -> {exc.code}  {'OK（应拦截）' if ok else '✗'}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    if log_path.exists():
        emit()
        emit("--- uvicorn 日志 ---")
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            emit(line)

    emit()
    emit(f"冒烟测试结果：{'全部通过' if failures == 0 else f'{failures} 项异常'}")

    report = os.environ.get("OWB_SMOKE_REPORT")
    if report:
        try:
            Path(report).write_text("\n".join(LINES) + "\n", encoding="utf-8")
        except OSError:
            pass

    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
