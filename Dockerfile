# SQLite 运行时库重建（A04）。
#
# Debian bookworm 提供 3.40.1-2+deb12u2；该修订回补了 CVE-2025-6965，但**没有**
# 回补 WAL-reset 缺陷——官方说明旧版本在特定并发写入与归并条件下可能损坏数据，
# 修复进入 3.51.3 及后续版本。账本是本项目的唯一事实来源，因此运行时库改为按
# 上游 amalgamation 重建，版本与 SHA3-256 双双钉死；构建阶段被 BuildKit 缓存，
# 只有改了这两个参数才会重新下载和编译。
FROM python:3.14-slim-bookworm AS sqlite-build

ARG SQLITE_VERSION=3540100
ARG SQLITE_SHA3=92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30

WORKDIR /build
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && { apt-get update \
         || apt-get -o Acquire::Check-Valid-Until=false update; } \
    && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*
# sqlite.org 从部署机下载很慢，允许长时间重试；SHA3 不匹配直接让构建失败。
RUN curl -fsSL --retry 6 --retry-delay 5 --retry-all-errors --connect-timeout 30 \
        -o sqlite.zip "https://www.sqlite.org/2026/sqlite-amalgamation-${SQLITE_VERSION}.zip" \
    && python -c "import hashlib, sys; data = open('sqlite.zip','rb').read(); \
digest = hashlib.sha3_256(data).hexdigest(); \
print('amalgamation sha3:', digest); \
sys.exit(0 if digest == '${SQLITE_SHA3}' else 'sqlite amalgamation checksum mismatch')" \
    && python -c "import zipfile; zipfile.ZipFile('sqlite.zip').extractall('.')"
RUN gcc -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 \
        -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_RTREE \
        -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_THREADSAFE=1 \
        -o libsqlite3.so.0.8.6 "sqlite-amalgamation-${SQLITE_VERSION}/sqlite3.c" \
        -lpthread -ldl -lm

FROM python:3.14-slim-bookworm

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
# /usr/local/lib 在默认 ld.so 搜索顺序中先于 /usr/lib/x86_64-linux-gnu，
# 因此 Python 的 _sqlite3 会加载这里的新库；下面这条 RUN 断言"实际加载的库
# 已是修复版本"，让版本回退在构建期就失败，而不是等运行期才发现。
COPY --from=sqlite-build /build/libsqlite3.so.0.8.6 /usr/local/lib/libsqlite3.so.0.8.6
RUN ln -sf libsqlite3.so.0.8.6 /usr/local/lib/libsqlite3.so.0 \
    && ldconfig \
    && python -c "import sqlite3, sys; version = sqlite3.sqlite_version; \
print('runtime sqlite:', version); \
sys.exit(0 if tuple(int(p) for p in version.split('.')[:2]) >= (3, 51) else 'runtime sqlite is older than 3.51')"
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
