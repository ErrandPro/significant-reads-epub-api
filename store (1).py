import json
import os
import ssl
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = 60 * 60 * 6  # 6 hours

if REDIS_URL.startswith("rediss://"):
    _ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    _ssl_context.check_hostname = False
    _ssl_context.verify_mode = ssl.CERT_NONE
    _client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        ssl_context=_ssl_context,
    )
else:
    _client = redis.from_url(REDIS_URL, decode_responses=True)


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
