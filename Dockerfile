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

CMD ["python", "scripts/cloudflare_start.py"]