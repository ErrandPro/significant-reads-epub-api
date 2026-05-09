import os
import base64
import logging
import time
from celery import Celery
from store import set_job, get_job, JobStatus, store_epub, EPUB_TTL_SECONDS
from processor import (
    extract_text_from_pdf,
    ocr_pdf_if_needed,
    extract_rich_chapters,
    extract_rich_chapters_from_docx,
    extract_text_from_docx,
    convert_doc_to_docx,
)
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
def convert_pdf_task(self, job_id: str, file_b64: str, title: str, author: str, ext: str = ".pdf"):
    t0 = time.time()
    out_dir = f"/tmp/epub_out_{job_id}"
    os.makedirs(out_dir, exist_ok=True)

    in_path = f"/tmp/file_in_{job_id}{ext}"
    with open(in_path, "wb") as f:
        f.write(base64.b64decode(file_b64))

    try:
        if ext == ".pdf":
            epub_path = _pipeline_pdf(job_id, in_path, title, author, out_dir)
        elif ext == ".docx":
            epub_path = _pipeline_docx(job_id, in_path, title, author, out_dir)
        elif ext == ".doc":
            epub_path = _pipeline_doc(job_id, in_path, title, author, out_dir)
        else:
            raise ValueError(f"Unsupported file type: {ext}")

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
        if os.path.exists(in_path):
            os.remove(in_path)
        import shutil
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)


# ── Pipelines ─────────────────────────────────────────────────────────────────

def _pipeline_pdf(job_id: str, pdf_path: str, title: str, author: str, out_dir: str) -> str:
    _update(job_id, status=JobStatus.EXTRACTING, progress=10)
    logger.info(f"job_id={job_id} stage=extract type=pdf")

    rich_chapters = _try_rich_extraction(pdf_path, job_id)
    text = _extract_text(pdf_path, job_id)

    _update(job_id, status=JobStatus.BUILDING, progress=60)
    logger.info(
        f"job_id={job_id} stage=build words={len(text.split())} "
        f"rich={'yes' if rich_chapters else 'no'}"
    )

    return build_epub(
        text, title, author, out_dir,
        pdf_path=pdf_path,
        rich_chapters=rich_chapters,
    )


def _pipeline_docx(job_id: str, docx_path: str, title: str, author: str, out_dir: str) -> str:
    _update(job_id, status=JobStatus.EXTRACTING, progress=10)
    logger.info(f"job_id={job_id} stage=extract type=docx")

    rich_chapters = _try_rich_extraction_docx(docx_path, job_id)
    text = _extract_text_docx(docx_path, job_id)

    _update(job_id, status=JobStatus.BUILDING, progress=60)
    logger.info(
        f"job_id={job_id} stage=build words={len(text.split())} "
        f"rich={'yes' if rich_chapters else 'no'}"
    )

    return build_epub(
        text, title, author, out_dir,
        docx_path=docx_path,   # ← enables cover extraction from Word files
        rich_chapters=rich_chapters,
    )


def _pipeline_doc(job_id: str, doc_path: str, title: str, author: str, out_dir: str) -> str:
    _update(job_id, status=JobStatus.EXTRACTING, progress=5)
    logger.info(f"job_id={job_id} stage=doc_to_docx")

    # convert_doc_to_docx takes only doc_path and returns the .docx path
    docx_path = convert_doc_to_docx(doc_path)
    return _pipeline_docx(job_id, docx_path, title, author, out_dir)


# ── Extraction helpers — PDF ──────────────────────────────────────────────────

def _try_rich_extraction(pdf_path: str, job_id: str) -> list[tuple[str, list[dict]]] | None:
    try:
        chapters = extract_rich_chapters(pdf_path)
        if chapters:
            logger.info(f"job_id={job_id} rich_extractor=ok chapters={len(chapters)}")
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


# ── Extraction helpers — DOCX ─────────────────────────────────────────────────

def _try_rich_extraction_docx(docx_path: str, job_id: str) -> list[tuple[str, list[dict]]] | None:
    try:
        chapters = extract_rich_chapters_from_docx(docx_path)
        if chapters:
            logger.info(f"job_id={job_id} rich_extractor_docx=ok chapters={len(chapters)}")
        else:
            logger.info(f"job_id={job_id} rich_extractor_docx=fallback")
        return chapters
    except Exception as e:
        logger.warning(f"job_id={job_id} rich_extractor_docx_failed={e}")
        return None


def _extract_text_docx(docx_path: str, job_id: str) -> str:
    try:
        text = extract_text_from_docx(docx_path)
        if text and len(text.strip()) > 100:
            logger.info(f"job_id={job_id} extractor=docx chars={len(text)}")
            return text
    except Exception as e:
        logger.warning(f"job_id={job_id} docx_extractor_failed={e}")
    return ""
