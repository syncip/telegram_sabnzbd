FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data \
    PUID=1000 PGID=1000
WORKDIR /app

# ffmpeg: 🎧 listening in; calibre (ebook-convert): 📚 books in the format the user wants
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg calibre && rm -rf /var/lib/apt/lists/*
ENV QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox

COPY requirements.txt .
# Patient with flaky networks (Unraid): longer timeout, more retries.
RUN pip install --no-cache-dir --timeout 60 --retries 10 -r requirements.txt

COPY arrbot ./arrbot
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME /data

# Starts as root only to fix ownership of /data, then runs the bot as PUID:PGID.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "arrbot"]
