# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

# OS deps for headless Chromium (Playwright) + build tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gzip \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libx11-6 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 \
        libgbm1 libdrm2 libpango-1.0-0 libcairo2 libasound2 libwayland-server0 libxi6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY carscout /app/carscout
COPY tests /app/tests
RUN pip install --upgrade pip && pip install ".[test]"

# Chromium for Playwright (platforms B & D need a real browser context).
# --with-deps pulls the OS libraries; harmless duplicates with the apt block above.
RUN playwright install --with-deps chromium \
    && rm -rf /ms-playwright/.links /var/lib/apt/lists/*

RUN useradd -m -u 10001 carscout && mkdir -p /data && chown -R carscout:carscout /data /app
USER carscout
ENV HOME=/home/carscout

EXPOSE 8000 8088
CMD ["carscout", "serve"]
