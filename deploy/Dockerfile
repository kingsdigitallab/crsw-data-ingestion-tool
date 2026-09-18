# CRSW web deposit service. Built from the repo root:
#   docker build -f deploy/Dockerfile -t crsw-deposit-web .
# No credentials are baked in; they arrive via environment at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Install deps first so source edits do not invalidate the layer.
COPY pyproject.toml ./
COPY crsw_deposit ./crsw_deposit
COPY crsw_web ./crsw_web
COPY promoter ./promoter
RUN pip install --no-cache-dir ".[web]"

USER app
EXPOSE 8000
# No temp dirs, no volumes: request bodies stream through to Ceph.
CMD ["uvicorn", "--factory", "crsw_web.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
