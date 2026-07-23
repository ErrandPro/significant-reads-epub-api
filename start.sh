#!/bin/bash
celery -A tasks worker --loglevel=info --concurrency=1 &
uvicorn main:app --host 0.0.0.0 --port 10000 --workers 1
