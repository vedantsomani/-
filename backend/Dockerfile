# SPHEREx Time Machine API.
#
# BUILD CONTEXT IS THE REPO ROOT (fly.toml lives there), so paths below start with backend/ and scripts/:
#   docker build -f backend/Dockerfile -t spherex-api .
#
# One worker only: download/processing jobs live in process memory (FieldManager), and the
# on-disk cache is shared through the volume mounted at /data.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPHEREX_CACHE_DIR=/data/cache \
    PORT=8080 \
    HOME=/home/app

# Unprivileged runtime user. /data is the Fly volume mount point; volumes arrive root-owned, so
# the entrypoint fixes ownership and then drops root before starting uvicorn.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/cache \
    && chown -R app:app /data

WORKDIR /app

# Dependencies first so code-only changes reuse the cached layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY backend/docker-entrypoint.sh ./docker-entrypoint.sh
# Demo prewarm helpers (run inside the machine with: sh /app/prewarm-demo.sh)
COPY scripts/demo-targets.txt ./demo-targets.txt
COPY scripts/prewarm-demo.sh ./prewarm-demo.sh

EXPOSE 8080
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
