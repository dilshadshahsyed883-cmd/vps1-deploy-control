FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QT_QPA_PLATFORM=offscreen
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 libegl1 libxkbcommon0 libdbus-1-3 \
    fonts-dejavu-core fonts-noto-core fonts-noto-extra curl rclone \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/scene-builder
COPY vps_engine/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY app/smart_scroll/ ./app/smart_scroll/
COPY app/assets/ ./app/assets/
COPY app/vps_scripts/ ./app/vps_scripts/
COPY vps_engine/ ./vps_engine/
ENV PYTHONPATH=/srv/scene-builder/app:/srv/scene-builder
RUN mkdir -p /data/jobs /data/permanent/music /data/permanent/sfx /root/.config/rclone \
    && chmod +x /srv/scene-builder/app/vps_scripts/move_output_to_drive.sh \
    && ln -s /srv/scene-builder/app /app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn","vps_engine.api_server:app","--host","0.0.0.0","--port","8000","--workers","1"]
