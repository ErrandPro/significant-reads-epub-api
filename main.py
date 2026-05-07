import os
import uuid
import base64
import logging
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from tasks import convert_pdf_task
from store import get_job, set_job, JobStatus

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="PDF→EPUB API", version="3.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/ready")
def ready():
    """Readiness check — verifies Redis/Celery broker is reachable."""
    try:
        from tasks import celery_app
        celery_app.control.inspect(timeout=1).ping()
        return {"status": "ready"}
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(status_code=503, detail="Worker unavailable")


@app.post("/convert")
@limiter.limit("10/minute")
async def convert_pdf(
    request: Request,
    pdf: UploadFile = File(...),
    title: str = Form(...),
    author: str = Form(...),
):
    """
    Accepts a PDF and enqueues an async conversion job.
    Returns a job_id immediately — poll /status/{job_id} for progress.
    """
    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF.")
    raw = await pdf.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds 50 MB limit.")

    job_id = str(uuid.uuid4())
    logger.info(f"job_id={job_id} filename={pdf.filename} size={len(raw)}")

    # Encode PDF as base64 and pass via Redis — worker lives in a separate container
    # and cannot access this container's /tmp filesystem.
    pdf_b64 = base64.b64encode(raw).decode("utf-8")

    set_job(job_id, {"status": JobStatus.QUEUED, "title": title, "author": author})
    convert_pdf_task.delay(job_id, pdf_b64, title, author)
    return JSONResponse({"job_id": job_id, "status": JobStatus.QUEUED}, status_code=202)


@app.get("/status/{job_id}")
async def job_status(job_id: str):
    """Poll conversion progress. When status=done, call /download/{job_id}."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/download/{job_id}")
async def download_epub(job_id: str):
    """Stream the finished EPUB. Only available when status=done."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.get("status") != JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"Job not ready: {job.get('status')}")
    epub_path = job.get("epub_path")
    if not epub_path or not os.path.exists(epub_path):
        raise HTTPException(status_code=410, detail="EPUB expired or missing.")
    safe_title = job.get("title", "book").replace(" ", "_")
    return FileResponse(
        epub_path,
        media_type="application/epub+zip",
        filename=f"{safe_title}.epub",
    )
