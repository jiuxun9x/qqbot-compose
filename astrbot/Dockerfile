ARG ASTRBOT_IMAGE
FROM ${ASTRBOT_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/qqbot-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/qqbot-requirements.txt \
    && rm /tmp/qqbot-requirements.txt
