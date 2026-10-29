FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Install Noto CJK Japanese fonts (required by Pillow for OGP & 5-slide IG carousels), ffmpeg for Reels MP4, + libpq5 for PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    fonts-noto-cjk \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app
RUN mkdir -p /app/data && chmod +x /app/scripts/*.sh /app/scripts/*.py || true

EXPOSE 8080

# Default entrypoint runs HTTP Gunicorn workers; `fly.toml` overrides per process group (`app` vs `worker`)
CMD ["sh", "-c", "exec gunicorn --workers ${WEB_CONCURRENCY:-2} --threads 16 --timeout 60 --graceful-timeout 20 -b 0.0.0.0:${PORT:-8080} app:app"]
