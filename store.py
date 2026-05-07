import json
import os
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = 60 * 60 * 6  # 6 hours


def _clean_url(url: str) -> str:
    """Remove ssl_cert_reqs query param — redis-py wants it as a kwarg, not in the URL."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("ssl_cert_reqs", None)
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


_redis_url = _clean_url(REDIS_URL)

if _redis_url.startswith("rediss://"):
    _client = redis.from_url(
        _redis_url,
        decode_responses=True,
        ssl_cert_reqs=None,
    )
else:
    _client = redis.from_url(_redis_url, decode_responses=True)


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
