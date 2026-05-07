import os
import ssl
import logging
import time
from celery import Celery
from store import set_job, get_job, JobStatus
from processor import extract_text_from_pdf, ocr_pdf_if_needed
from epub_builder import build_epub

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_ssl_options = (
    {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    if REDIS_URL.startswith("rediss://")
    else {}
)

celery_app = Celery("converter")
celery_app.config_from_object({
    "broker_url": REDIS_URL,
    "result_backend": REDIS_URL,
    "broker_use_ssl": _ssl_options,
    "redis_backend_use_ssl": _ssl_options,
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],
    "task_acks_late": True,
    "worker_prefetch_multiplier": 1,
    "task_track_started": True,
    "broker_connection_retry_on_startup": True,
})

def _update(job_id: str, **kwargs):
    job = get_job(job_id) or {}
    job.update(kwargs)
    set_job(job_id, job)

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    soft_time_limit=300,
    time_limit=360,
)
def convert_pdf_task(self, job_id: str, pdf_path: str, title: str, author: str):
    t0 = time.time()
    out_dir = f"/tmp/epub_out_{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    try:
        _update(job_id, status=JobStatus.EXTRACTING, progress=10)
        logger.info(f"job_id={job_id} stage=extract")
        text = _extract_text(pdf_path, job_id)
        _update(job_id, status=JobStatus.BUILDING, progress=60)
        logger.info(f"job_id={job_id} stage=build words={len(text.split())}")
        epub_path = build_epub(text, title, author, out_dir)
        if not os.path.exists(epub_path):
            raise RuntimeError("EPUB file was not created.")
        elapsed = round(time.time() - t0, 1)
        _update(
            job_id,
            status=JobStatus.DONE,
            progress=100,
            epub_path=epub_path,
            elapsed_seconds=elapsed,
        )
        logger.info(f"job_id={job_id} status=done elapsed={elapsed}s")
    except Exception as exc:
        logger.error(f"job_id={job_id} error={exc}", exc_info=True)
        _update(job_id, status=JobStatus.FAILED, error=str(exc))
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            _update(job_id, status=JobStatus.FAILED, error=f"Max retries exceeded: {exc}")
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

def _extract_text(pdf_path: str, job_id: str) -> str:
    try:
        text = _pymupdf_extract(pdf_path)
        if text and len(text.strip()) > 100:
            logger.info(f"job_id={job_id} extractor=pymupdf chars={len(text)}")
            return text
    except Exception as e:
        logger.warning(f"job_id={job_id} pymupdf_failed={e}")
    try:
        text = extract_text_from_pdf(pdf_path)
        if text and len(text.strip()) > 100:
            logger.info(f"job_id={job_id} extractor=pdfminer chars={len(text)}")
            return text
    except Exception as e:
        logger.warning(f"job_id={job_id} pdfminer_failed={e}")
    logger.info(f"job_id={job_id} extractor=ocr")
    return ocr_pdf_if_needed(pdf_path)

def _pymupdf_extract(pdf_path: str) -> str:
    import fitz
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        blocks = page.get_text("dict", sort=True)["blocks"]
        page_text = []
        for block in blocks:
            if block.get("type") == 0:
                for line in block.get("lines", []):
                    line_text = " ".join(
                        span["text"] for span in line.get("spans", [])
                    )
                    page_text.append(line_text.strip())
        pages.append("\n".join(page_text))
    doc.close()
    return "\n\n".join(pages)
