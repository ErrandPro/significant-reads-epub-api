import json
import os
import redis
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = 60 * 60 * 6   # 6 hours
EPUB_TTL_SECONDS = 60 * 60 * 6  # 6 hours


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("ssl_cert_reqs", None)
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


_redis_url = _clean_url(REDIS_URL)

if _redis_url.startswith("rediss://"):
    _client = redis.from_url(_redis_url, decode_responses=True, ssl_cert_reqs=None)
    # Second client for binary data (EPUBs)
    _bclient = redis.from_url(_redis_url, decode_responses=False, ssl_cert_reqs=None)
else:
    _client = redis.from_url(_redis_url, decode_responses=True)
    _bclient = redis.from_url(_redis_url, decode_responses=False)


class JobStatus:
    QUEUED = "queued"
    EXTRACTING = "extracting"
    BUILDING = "building"
    DONE = "done"
    FAILED = "failed"


def _key(job_id: str) -> str:
    return f"job:{job_id}"

def _epub_key(job_id: str) -> str:
    return f"epub:{job_id}"


def set_job(job_id: str, data: dict) -> None:
    _client.setex(_key(job_id), JOB_TTL_SECONDS, json.dumps(data))


def get_job(job_id: str) -> dict | None:
    raw = _client.get(_key(job_id))
    return json.loads(raw) if raw else None


def delete_job(job_id: str) -> None:
    _client.delete(_key(job_id))


def store_epub(job_id: str, epub_bytes: bytes) -> None:
    """Store raw EPUB bytes in Redis so any container can serve them."""
    _bclient.setex(_epub_key(job_id), EPUB_TTL_SECONDS, epub_bytes)


def get_epub(job_id: str) -> bytes | None:
    """Retrieve raw EPUB bytes from Redis."""
    return _bclient.get(_epub_key(job_id))


def delete_epub(job_id: str) -> None:
    _bclient.delete(_epub_key(job_id))
