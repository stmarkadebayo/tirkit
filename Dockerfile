# Tirkit server image: API, OTLP ingest, web UI and worker.
FROM node:26-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install ".[server]"
COPY --from=web /web/dist /app/web
ENV TIRKIT_WEB_DIR=/app/web TIRKIT_MEDIA_DIR=/data/media
RUN useradd --system --uid 10001 tirkit && mkdir -p /data/media && chown -R tirkit /data
USER tirkit
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["tirkit", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
