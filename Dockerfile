FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data \
    PUID=1000 PGID=1000
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY arrbot ./arrbot
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME /data

# Starts as root only to fix ownership of /data, then runs the bot as PUID:PGID.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "arrbot"]
