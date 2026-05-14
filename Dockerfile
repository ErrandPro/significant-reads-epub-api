FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
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
