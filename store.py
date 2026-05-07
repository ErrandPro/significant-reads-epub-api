import json
import os
import redis
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = 60 * 60 * 6  # 6 hours


def _clean_redis_url(url: str) -> str:
    """Strip ssl_cert_reqs query param that Upstash embeds in the URL,
    since redis-py doesn't accept it as a string — we pass it as a kwarg instead."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("ssl_cert_reqs", None)
    cleaned = parsed._replace(query=urlencode(qs, doseq=True))
    return urlunparse(cleaned)


_clean_url = _clean_redis_url(REDIS_URL)

if _clean_url.startswith("rediss://"):
    _client = redis.from_url(
        _clean_url,
        decode_responses=True,
        ssl_cert_reqs=None,
    )
else:
    _client = redis.from_url(_clean_url, decode_responses=True)


class JobStatus:
    QUEUED = "queued"
    EXTRACTING = "extracting"
    BUILDING = "building"
    DONE = "done"
    FAILED = "failed"


def _key(job_id: str) -> str:
    return f"job:{job_id}"


def set_job(job_id: str, data: dict) -> None:
    _client.setex(_key(job_id), JOB_TTL_SECONDS, json.dumps(data))


def get_job(job_id: str) -> dict | None:
    raw = _client.get(_key(job_id))
    return json.loads(raw) if raw else None


def delete_job(job_id: str) -> None:
    _client.delete(_key(job_id))
