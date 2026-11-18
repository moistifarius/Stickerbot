FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stickerbot ./stickerbot

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
RUN useradd -u 99 -g 100 -M -d /app -o stickerbot 2>/dev/null || true \
 && mkdir -p /data && chown 99:100 /data
USER 99:100
VOLUME /data

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD ["python", "-m", "stickerbot.healthcheck"]

CMD ["python", "-m", "stickerbot"]
