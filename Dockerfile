# OrderInbox AI — the appliance
#
# One container, one data volume. Point it at your inbox (or run `orderinbox
# demo` for the two-minute demo) and open http://localhost:8501.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ORDERINBOX_HOME=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY orderinbox ./orderinbox
COPY demo ./demo
RUN pip install --no-cache-dir .[dev]

# tesseract + poppler make scanned-PDF OCR and robust PDF text extraction work
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

VOLUME /data
EXPOSE 8501

# Default: start the console on existing data. For the demo, run:
#   docker compose run orderinbox orderinbox demo
CMD ["orderinbox", "serve"]
