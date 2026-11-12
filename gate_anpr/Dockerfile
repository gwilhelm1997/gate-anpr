ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie-2026.11.0
FROM ${BUILD_FROM}

ENV HOME=/root PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app

# ONNX Runtime ships glibc wheels only, hence the Debian base rather than Alpine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Bake every selectable model into the image: no downloads at run time, works offline.
RUN /opt/venv/bin/python -c "from fast_alpr import ALPR; \
[ALPR(detector_model=d, ocr_model=o, ocr_device='cpu') \
 for d in ('yolo-v9-t-384-license-plate-end2end', 'yolo-v9-t-512-license-plate-end2end', 'yolo-v9-t-640-license-plate-end2end') \
 for o in ('cct-s-v2-global-model', 'cct-xs-v2-global-model')]"

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ARG BUILD_VERSION=dev
ENV GATE_ANPR_VERSION=${BUILD_VERSION}
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="amd64|aarch64"

CMD ["/run.sh"]
