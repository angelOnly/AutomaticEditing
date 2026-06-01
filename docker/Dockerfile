FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    MODELSCOPE_CACHE=/app/.cache/modelscope

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ffmpeg \
        git \
        libasound2 \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        libstdc++6 \
        libxext6 \
        libxrender1 \
        portaudio19-dev \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements-docker.txt /tmp/requirements-docker.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /tmp/requirements-docker.txt

COPY models /opt/automatic-editing/models
COPY tts_ref /opt/automatic-editing/tts_ref
COPY docker/entrypoint.sh /usr/local/bin/automatic-editing-entrypoint

RUN chmod +x /usr/local/bin/automatic-editing-entrypoint \
    && mkdir -p /workspace /opt/automatic-editing/models /opt/automatic-editing/tts_ref \
    && mkdir -p /app/.cache/huggingface /app/.cache/modelscope

WORKDIR /workspace

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=3).read()"

ENTRYPOINT ["automatic-editing-entrypoint"]
CMD ["python", "-m", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "7860"]
