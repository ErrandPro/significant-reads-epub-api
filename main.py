import os
import uuid
import base64
import logging
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from tasks import convert_pdf_task
from store import get_job, set_job, get_epub, delete_epub, JobStatus

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Document→EPUB API", version="3.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc"}
ALLOWED_DISPLAY    = "PDF, DOCX, or DOC"


@app.get("/health")
def health():
    return {"status": "ok", "version": "3.1.0"}


@app.get("/ready")
def ready():
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
    pdf: UploadFile = File(...),       # field name kept as "pdf" for backward compatibility
    title:  str = Form(...),
    author: str = Form(...),
):
    # ── File type validation ───────────────────────────────────────────────
    if not pdf.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    ext = os.path.splitext(pdf.filename.lower())[1]   # e.g. ".pdf", ".docx", ".doc"

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Please upload a {ALLOWED_DISPLAY} file.",
        )

    # ── Size check ─────────────────────────────────────────────────────────
    raw = await pdf.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds 50 MB limit.",
        )

    # ── Queue the job ──────────────────────────────────────────────────────
    job_id  = str(uuid.uuid4())
    file_b64 = base64.b64encode(raw).decode("utf-8")

    logger.info(
        f"job_id={job_id} filename={pdf.filename} "
        f"ext={ext} size={len(raw)}"
    )

    set_job(job_id, {"status": JobStatus.QUEUED, "title": title, "author": author})

    # Pass the file extension so the worker knows which pipeline to run
    convert_pdf_task.delay(job_id, file_b64, title, author, ext)

    return JSONResponse(
        {"job_id": job_id, "status": JobStatus.QUEUED},
        status_code=202,
    )


@app.get("/status/{job_id}")
async def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/download/{job_id}")
async def download_epub(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.get("status") != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Job not ready: {job.get('status')}",
        )
    epub_bytes = get_epub(job_id)
    if not epub_bytes:
        raise HTTPException(status_code=410, detail="EPUB expired or missing.")

    safe_title = job.get("title", "book").replace(" ", "_")

    delete_epub(job_id)

    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.epub"'},
    )
