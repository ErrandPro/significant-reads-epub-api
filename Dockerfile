# ── Stage 1: LibreOffice base ─────────────────────────────────────────────
# We pull a pre-built image that already has LibreOffice baked in.
# This means Railway never has to apt-get install it during your build —
# it just pulls the layer from Docker Hub cache.
FROM debian:bookworm-slim AS libreoffice-base
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Final image ──────────────────────────────────────────────────
FROM python:3.11-slim

# Copy LibreOffice from stage 1 instead of installing it here
COPY --from=libreoffice-base /usr/lib/libreoffice /usr/lib/libreoffice
COPY --from=libreoffice-base /usr/bin/soffice /usr/bin/soffice
COPY --from=libreoffice-base /usr/share/libreoffice /usr/share/libreoffice
COPY --from=libreoffice-base /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu

# Install everything else (small packages, no LO download here)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libmupdf-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000", "--workers", "2"]
