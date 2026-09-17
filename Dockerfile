FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WEB_STATE_FILE=logs/web_monitor_state.json \
    WEB_STATE_LOCK_FILE=logs/web_monitor_state.lock \
    BUY_DISABLED_FILE=buy_disabled.json \
    WEB_LOG_LEVEL=INFO \
    WEB_ALLOW_LOCAL_NOAUTH=false \
    TRADING_ENABLED=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-web.txt

COPY . .

RUN mkdir -p logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

CMD ["python", "scripts/cloudflare_start.py"]