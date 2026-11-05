FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRICEWATCH_HOST=0.0.0.0 \
    PRICEWATCH_PORT=8000 \
    PRICEWATCH_DB=/data/pricewatch.db

WORKDIR /app

# Dependencies first so this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pricewatch ./pricewatch

# Run as an unprivileged user; /data holds the SQLite file.
RUN useradd --create-home --uid 10001 app \
    && mkdir /data \
    && chown app:app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ['PRICEWATCH_PORT'], timeout=2)"

# The feed, price store and event bus live in memory: run exactly one container.
CMD ["python", "-m", "pricewatch"]
