# ============================================================
# openclaw-workbench · Dockerfile
#
# 目标平台：linux/arm64/v8（极空间 Z2 Pro，RK3568，4GB RAM）
# 基础镜像刻意选择 python:3.12-slim（Debian）而不是 alpine：
#   PyMuPDF 在 musl 上容易出现运行时问题，Debian 更省心。
#
# 所有依赖都有 aarch64 预编译轮子，因此镜像内不需要 gcc / make。
#
# 交叉构建（在你的 Windows 电脑上执行）：
#   docker buildx build --platform linux/arm64/v8 -t openclaw-workbench:1.0.0 . --load
#
# 如果挂载目录写入报 Permission denied（极空间的目录属主与容器 UID 不一致），
# 有两种解法，任选一种：
#   a) 让目录可被任意用户写：在极空间把两个数据目录权限放开
#   b) 用宿主相同的 UID 构建（临时办法，安全性下降）：
#      docker buildx build --build-arg OWB_UID=0 --build-arg OWB_GID=0 ...
# ============================================================

FROM python:3.12-slim

# 容器内运行身份，可在构建时覆盖。默认 10001（非 root）。
ARG OWB_UID=10001
ARG OWB_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=Asia/Shanghai \
    WORKBENCH_PORT=8080 \
    WORKBENCH_LOG_LEVEL=INFO \
    PYTHONPATH=/app

WORKDIR /app

# ---- 依赖层（单独一层，改代码时不会触发重装）----
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---- 代码 ----
COPY app /app/app
COPY prompts /app/prompts
COPY scripts /app/scripts

# ---- 运行期目录与非 root 用户 ----
# /app/var/jobs 是分段分析的中间产物目录，【刻意不挂载】：
#   留在容器可写层，容器重建时由 Docker 自动清空。
#   这样工作台代码里永远不需要出现"删除"操作。
RUN groupadd --gid "${OWB_GID}" owb 2>/dev/null || true \
 && useradd --uid "${OWB_UID}" --gid "${OWB_GID}" --no-create-home --shell /usr/sbin/nologin owb 2>/dev/null || true \
 && mkdir -p /app/var/jobs /data/literature /data/life_notes \
 && chown -R "${OWB_UID}:${OWB_GID}" /app/var

# 数据目录的属主由挂载覆盖，这里只保证"目录存在"这一默认形态可用。
# 真实的 literature / life_notes 由 docker-compose 或极空间挂载进来。

USER ${OWB_UID}:${OWB_GID}

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WORKBENCH_PORT','8080')+'/api/health',timeout=3)"

# 单进程单 worker 是【硬性要求】：
#   会话表、任务队列、登录限速状态都在进程内存里，多 worker 会导致状态不一致。
#
# --no-server-header：uvicorn 默认会自己加上 "Server: uvicorn" 并覆盖应用设的值，
#   等于把服务端实现告诉公网。加上这个开关，响应头里就只剩应用自己写的 "Server: workbench"。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
