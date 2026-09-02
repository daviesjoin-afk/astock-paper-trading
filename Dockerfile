FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
COPY --chown=app:app backend ./backend
RUN find /app/backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
COPY --chown=app:app frontend ./frontend
# Keep the legacy /assets/app.js URL byte-for-byte aligned with the canonical
# frontend entrypoint.  Older cached HTML referenced this path; allowing the
# checked-in mirror to drift made a normal refresh execute obsolete code.
RUN install -o app -g app -m 0644 /app/frontend/app.js /app/frontend/assets/app.js
# Keep the legacy CSS URL aligned with the canonical stylesheet as well;
# cached pages still request /assets/app.css.
RUN install -o app -g app -m 0644 /app/frontend/app.css /app/frontend/assets/app.css
# 注：公开镜像不含宿主部署/调度配置（cron、worker 编排等由部署侧提供）

USER app
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; [urllib.request.urlopen(url, timeout=2).read() for url in ('http://127.0.0.1:8600/api/health','http://127.0.0.1:8600/api/adaptive/ai/settings')]"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8600", "--workers", "1", "--limit-concurrency", "32", "--backlog", "128", "--timeout-keep-alive", "5", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
