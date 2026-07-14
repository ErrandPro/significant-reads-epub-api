---
title: significant-reads-epub-api
emoji: 📚
sdk: docker
app_port: 7860
---

# PDF → EPUB Converter — V3

## What's new in V3

### Architecture
- **Async job queue** (Celery + Redis) — `/convert` returns immediately with a `job_id`; no more 30-second gateway timeouts on large books
- **Status polling** — `GET /status/{job_id}` returns `queued → extracting → building → done`
- **Separate worker service** on Render — API and CPU-heavy work run independently

### Extraction
- **3-stage pipeline**: PyMuPDF → pdfminer → Tesseract OCR
  - PyMuPDF is 3–5× faster than pdfminer and preserves reading order better
  - pdfminer catches complex font encodings PyMuPDF misses
  - Tesseract handles scanned / image-only PDFs
- **Cover image** extracted from page 1 and embedded in the EPUB

### Reliability
- **Rate limiting**: 10 requests/minute per IP
- **Auto-retry**: failed jobs retry up to 2× with 10 s delay
- **Soft/hard time limits**: 5 min soft, 6 min hard — prevents zombie jobs
- **Job TTL**: Redis entries expire after 6 hours; EPUB files cleaned up automatically

### Observability
- **Structured JSON logging** with `job_id`, `extractor`, `elapsed_seconds`
- `/health` + `/ready` endpoints (readiness checks the broker)

---

## API reference

### POST /convert
Upload a PDF and receive a `job_id`.

```
curl -X POST https://your-api.onrender.com/convert \
  -F pdf=@mybook.pdf \
  -F title="My Book" \
  -F author="Jane Smith"
```

Response `202 Accepted`:
```json
{"job_id": "abc-123", "status": "queued"}
```

### GET /status/{job_id}
Poll until `status` is `done` or `failed`.

```json
{"status": "building", "progress": 60, "title": "My Book"}
```

### GET /download/{job_id}
Streams the EPUB. Call only when `status == "done"`.

```
curl -OJ https://your-api.onrender.com/download/abc-123
```

---

## Migration from V2

| V2 | V3 |
|---|---|
| `POST /convert` → EPUB bytes | `POST /convert` → `job_id` (202) |
| Synchronous, 30 s timeout | Async, polls `/status` |
| Single pdfminer extractor | PyMuPDF → pdfminer → OCR |
| No rate limiting | 10 req/min per IP |
| Free Render plan | Starter plan + Redis |

### Client update (example)

```javascript
// V3 client
async function convertPdf(file, title, author) {
  const form = new FormData();
  form.append("pdf", file);
  form.append("title", title);
  form.append("author", author);

  const { job_id } = await fetch("/convert", { method: "POST", body: form })
    .then(r => r.json());

  // Poll every 3 seconds
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    const status = await fetch(`/status/${job_id}`).then(r => r.json());
    if (status.status === "done") {
      window.location.href = `/download/${job_id}`;
      break;
    }
    if (status.status === "failed") throw new Error(status.error);
  }
}
```

---

## Local development

```bash
# Start Redis
docker run -p 6379:6379 redis:7-alpine

# Terminal 1 — API
uvicorn main:app --reload --port 10000

# Terminal 2 — Worker
celery -A tasks worker --loglevel=info --concurrency=2
```

## Deployment

Push to GitHub. Render will auto-deploy all three services defined in `render.yaml`.

Set one secret in Render dashboard: `REDIS_URL` is auto-injected from the Redis service.
```
