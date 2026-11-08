FROM python:3.11-slim-bookworm

# Base environment and Chromium installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MUSE2API_HOME=/app \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=18610 \
    MUSE2API_CHROMIUM=/usr/bin/chromium \
    MUSE2API_CDP_PORT=19210

# The API process runs as this uid/gid. Defaults match the first regular user on most
# Linux hosts, so the ./data bind mount stays writable. Override for a different host
# uid: docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
ARG UID=1000
ARG GID=1000

WORKDIR /app

# Install Chromium, CJK fonts, and core dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-wqy-zenhei \
    fonts-freefont-ttf \
    procps \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code and required directories
COPY admin.html app.py cdp.py config.py engine.py store.py proxy.py version.json ./
COPY deploy/ ./deploy/
COPY extension/ ./extension/
COPY tools/ ./tools/

# Unprivileged runtime user. Chromium still needs --no-sandbox in containers (user
# namespaces are usually disabled), but the API process itself no longer runs as root,
# so a bug in the service cannot write outside /app or touch the host filesystem.
RUN groupadd -g ${GID} muse2api \
    && useradd -u ${UID} -g muse2api -d /app -s /usr/sbin/nologin muse2api \
    && mkdir -p /app/data/media /app/data/downloads /app/muse2api-profiles \
    && chown -R ${UID}:${GID} /app

VOLUME ["/app/data"]

USER ${UID}:${GID}

EXPOSE 18610

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18610/healthz || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18610"]
