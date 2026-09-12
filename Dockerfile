FROM python:3.11-slim-bookworm

ARG ASTOCK_GIT_COMMIT=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    ASTOCK_GIT_COMMIT=${ASTOCK_GIT_COMMIT}

WORKDIR /app

COPY requirements.txt requirements.lock ./
# 主仓库走腾讯云内网友好的镜像；security 单独走阿里云——腾讯云的
# debian-security 镜像曾出现 InRelease 过期（2026-09-07 起），会直接让
# apt-get update 失败并中断镜像构建。仍保留一次「忽略 Valid-Until」的兜底
# 重试，避免任何单一镜像源同步滞后再次阻断构建。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && { apt-get update \
         || apt-get -o Acquire::Check-Valid-Until=false update; } \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir -r requirements.lock

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
COPY --chown=app:app backend ./backend
RUN find /app/backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
COPY --chown=app:app frontend ./frontend
# frontend/dist (esbuild output) is committed alongside the sources and is the
# only artifact served at runtime (/app.js, /app.css).  The old checked-in
# assets/ mirrors and app.min.* duplicates are gone — no byte-alignment step
# needed here anymore.
COPY --chown=app:app deploy ./deploy

USER app
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; [urllib.request.urlopen(url, timeout=2).read() for url in ('http://127.0.0.1:8600/api/health','http://127.0.0.1:8600/api/adaptive/ai/settings')]"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8600", "--workers", "1", "--limit-concurrency", "32", "--backlog", "128", "--timeout-keep-alive", "5", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
