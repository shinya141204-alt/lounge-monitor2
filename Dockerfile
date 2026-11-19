FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Install Noto CJK Japanese fonts (required by Pillow for OGP & 5-slide IG carousels), ffmpeg for Reels MP4, libpq5 for PostgreSQL, brotli for static precompression
RUN apt-get update && apt-get install -y --no-install-recommends \
    brotli \
    ca-certificates \
    ffmpeg \
    fonts-noto-cjk \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appgroup . /app
RUN mkdir -p /app/data \
    && if [ -d /app/scripts ]; then find /app/scripts -maxdepth 1 \( -name '*.sh' -o -name '*.py' \) -exec chmod +x {} +; fi \
    && find /app/static -type f \( -name '*.css' -o -name '*.js' -o -name '*.svg' -o -name '*.json' \) -exec brotli -f -q 6 -k {} + \
    && python3 -c "import gzip, pathlib; [p.with_name(p.name + '.gz').write_bytes(gzip.compress(p.read_bytes(), compresslevel=9, mtime=0)) for p in pathlib.Path('/app/static').rglob('*') if p.is_file() and p.suffix.lower() in ('.css', '.js', '.svg', '.json')]" \
    && chown appuser:appgroup /app \
    && chown -R appuser:appgroup /app/data /app/static /home/appuser

USER appuser

EXPOSE 8080

# Default entrypoint runs HTTP Gunicorn workers; `fly.toml` overrides per process group (`app` vs `worker`)
CMD ["sh", "-c", "exec gunicorn --workers ${WEB_CONCURRENCY:-2} --threads 16 --timeout 60 --graceful-timeout 20 -b 0.0.0.0:${PORT:-8080} app:app"]
