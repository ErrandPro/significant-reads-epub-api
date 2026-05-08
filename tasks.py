import os
import base64
import logging
import time
from celery import Celery
from store import set_job, get_job, JobStatus, store_epub, EPUB_TTL_SECONDS
from processor import extract_text_from_pdf, ocr_pdf_if_needed, extract_rich_chapters
from epub_builder import build_epub

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _ensure_ssl_param(url: str) -> str:
    if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}ssl_cert_reqs=CERT_NONE"
    return url


_broker_url = _ensure_ssl_param(REDIS_URL)
_ssl_options = {"ssl_cert_reqs": None} if REDIS_URL.startswith("rediss://") else {}

celery_app = Celery("converter")
celery_app.config_from_object({
    "broker_url": _broker_url,
    "result_backend": _broker_url,
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
def convert_pdf_task(self, job_id: str, pdf_b64: str, title: str, author: str):
    t0 = time.time()
    out_dir = f"/tmp/epub_out_{job_id}"
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = f"/tmp/pdf_in_{job_id}.pdf"

    with open(pdf_path, "wb") as f:
        f.write(base64.b64decode(pdf_b64))

    try:
        # ── Stage 1: extraction ──────────────────────────────────────────
        _update(job_id, status=JobStatus.EXTRACTING, progress=10)
        logger.info(f"job_id={job_id} stage=extract")

        # Try rich extraction first (images + tables + formatting).
        # Falls back to plain-text pipeline automatically if quality is poor.
        rich_chapters = _try_rich_extraction(pdf_path, job_id)

        # Always extract plain text too — used as fallback and for word-count logging.
        text = _extract_text(pdf_path, job_id)

        # ── Stage 2: build EPUB ──────────────────────────────────────────
        _update(job_id, status=JobStatus.BUILDING, progress=60)
        logger.info(
            f"job_id={job_id} stage=build "
            f"words={len(text.split())} "
            f"rich={'yes' if rich_chapters else 'no'}"
        )

        epub_path = build_epub(
            text,
            title,
            author,
            out_dir,
            pdf_path=pdf_path,
            rich_chapters=rich_chapters,
        )

        if not os.path.exists(epub_path):
            raise RuntimeError("EPUB file was not created.")

        with open(epub_path, "rb") as f:
            epub_bytes = f.read()
        store_epub(job_id, epub_bytes)

        elapsed = round(time.time() - t0, 1)
        _update(job_id, status=JobStatus.DONE, progress=100, elapsed_seconds=elapsed)
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
        import shutil
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)


# ── Extraction helpers ────────────────────────────────────────────────────────

def _try_rich_extraction(
    pdf_path: str, job_id: str
) -> list[tuple[str, list[dict]]] | None:
    """
    Attempt rich chapter extraction (images, tables, formatting).
    Returns None on any failure so the caller can fall back gracefully.
    """
    try:
        chapters = extract_rich_chapters(pdf_path)
        if chapters:
            logger.info(
                f"job_id={job_id} rich_extractor=ok chapters={len(chapters)}"
            )
        else:
            logger.info(f"job_id={job_id} rich_extractor=fallback")
        return chapters
    except Exception as e:
        logger.warning(f"job_id={job_id} rich_extractor_failed={e}")
        return None


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
