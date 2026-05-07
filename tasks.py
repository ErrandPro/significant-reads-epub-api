import os
import logging
import time
import ssl
from celery import Celery
from store import set_job, get_job, JobStatus
from processor import extract_text_from_pdf, ocr_pdf_if_needed
from epub_builder import build_epub

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_ssl_options = (
    {"ssl_cert_reqs": ssl.CERT_NONE}
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
