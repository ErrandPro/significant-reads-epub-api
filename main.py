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
from tasks import convert_pdf_task, merge_pdf_task, split_pdf_task
from store import get_job, set_job, get_epub, delete_epub, JobStatus

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Document→EPUB API", version="3.3.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png"}
ALLOWED_DISPLAY    = "PDF, DOCX, DOC, JPG, JPEG, or PNG"


@app.get("/health")
def health():
    return {"status": "ok", "version": "3.3.0"}


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
    pdf: UploadFile = File(...),
    title:  str = Form(default=""),
    author: str = Form(default=""),
    subtitle: str = Form(default=""),
    copyright: str = Form(default=""),
    dedication: str = Form(default=""),
    acknowledgements: str = Form(default=""),
    foreword: str = Form(default=""),
    target: str | None = Form(default=None),
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

    # ── Target validation ───────────────────────────────────────────────
    if target is None:
        if ext == ".pdf":
            target = "docx"
        elif ext in (".jpg", ".jpeg", ".png"):
            target = "pdf"
        else:
            target = "epub"

    VALID_TARGETS_BY_EXT = {
        ".pdf":  {"docx", "jpg"},     # PDF can now go to DOCX or JPG
        ".docx": {"epub", "pdf"},
        ".doc":  {"epub", "pdf"},
        ".jpg":  {"pdf"},
        ".jpeg": {"pdf"},
        ".png":  {"pdf"},
    }
    if target not in VALID_TARGETS_BY_EXT[ext]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot convert '{ext}' to '{target}'. Allowed: {', '.join(sorted(VALID_TARGETS_BY_EXT[ext]))}.",
        )

    # ── Front matter is only required for EPUB (book) output ──────────
    is_book = target == "epub"

    if is_book:
        if not title.strip():
            raise HTTPException(status_code=400, detail="Title is required when converting to EPUB.")
        if not author.strip():
            raise HTTPException(status_code=400, detail="Author is required when converting to EPUB.")
    else:
        if not title.strip():
            title = os.path.splitext(pdf.filename)[0]
        if not author.strip():
            author = "Unknown Author"

    # ── Size check ─────────────────────────────────────────────────────────
    raw = await pdf.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds 50 MB limit.",
        )

    # ── Queue the job ──────────────────────────────────────────────────────
    job_id   = str(uuid.uuid4())
    file_b64 = base64.b64encode(raw).decode("utf-8")

    logger.info(
        f"job_id={job_id} filename={pdf.filename} "
        f"ext={ext} target={target} size={len(raw)}"
    )

    set_job(job_id, {
        "status": JobStatus.QUEUED,
        "title": title,
        "author": author,
        "output_ext": {"docx": ".docx", "epub": ".epub", "pdf": ".pdf", "jpg": ".jpg"}[target],
    })

    # Pass the file extension and target so the worker knows which pipeline to run
    convert_pdf_task.delay(job_id, file_b64, title, author, ext, subtitle, copyright, dedication, acknowledgements, foreword, target)

    return JSONResponse(
        {"job_id": job_id, "status": JobStatus.QUEUED},
        status_code=202,
    )


@app.post("/merge")
@limiter.limit("10/minute")
async def merge_pdf(
    request: Request,
    files: list[UploadFile] = File(...),
):
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Upload at least 2 PDF files to merge.")

    files_b64 = []
    total_size = 0
    for f in files:
        if not f.filename or os.path.splitext(f.filename.lower())[1] != ".pdf":
            raise HTTPException(status_code=400, detail=f"'{f.filename}' is not a PDF file.")
        raw = await f.read()
        total_size += len(raw)
        if total_size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Combined files exceed 50 MB limit.")
        files_b64.append(base64.b64encode(raw).decode("utf-8"))

    job_id = str(uuid.uuid4())
    logger.info(f"job_id={job_id} action=merge files={len(files)} size={total_size}")

    set_job(job_id, {
        "status": JobStatus.QUEUED,
        "title": "merged",
        "output_ext": ".pdf",
    })

    merge_pdf_task.delay(job_id, files_b64)

    return JSONResponse({"job_id": job_id, "status": JobStatus.QUEUED}, status_code=202)


@app.post("/split")
@limiter.limit("10/minute")
async def split_pdf_endpoint(
    request: Request,
    pdf: UploadFile = File(...),
):
    if not pdf.filename or os.path.splitext(pdf.filename.lower())[1] != ".pdf":
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    raw = await pdf.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    job_id = str(uuid.uuid4())
    file_b64 = base64.b64encode(raw).decode("utf-8")
    logger.info(f"job_id={job_id} action=split filename={pdf.filename} size={len(raw)}")

    set_job(job_id, {
        "status": JobStatus.QUEUED,
        "title": os.path.splitext(pdf.filename)[0],
        "output_ext": ".zip",
    })

    split_pdf_task.delay(job_id, file_b64)

    return JSONResponse({"job_id": job_id, "status": JobStatus.QUEUED}, status_code=202)


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
    output_ext = job.get("output_ext", ".epub")

    delete_epub(job_id)

    if output_ext == ".docx":
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename   = f"{safe_title}.docx"
    elif output_ext == ".pdf":
        media_type = "application/pdf"
        filename   = f"{safe_title}.pdf"
    elif output_ext == ".jpg":
        media_type = "image/jpeg"
        filename   = f"{safe_title}.jpg"
    elif output_ext == ".zip":
        media_type = "application/zip"
        filename   = f"{safe_title}.zip"
    else:
        media_type = "application/epub+zip"
        filename   = f"{safe_title}.epub"

    return Response(
        content=epub_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
