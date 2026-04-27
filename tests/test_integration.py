import asyncio
import json
import subprocess
import sys
import time
import io
import uuid
import os
import pytest
import pytest_asyncio
import aiohttp
import redis.asyncio as aioredis
import numpy as np
from PIL import Image

S3_URL = os.getenv("S3_GATEWAY_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JOBS_QUEUE = "image.jobs"
DONE_QUEUE = "image.done"
NUM_JOBS = 10


def make_test_image_bytes() -> bytes:
    arr = np.zeros((100, 100, 3), dtype=np.uint8)
    arr[:, :, 0] = 200  # red
    arr[:, :, 1] = 100  # green
    arr[:, :, 2] = 50   # blue
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def redis_client():
    import redis
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    # Clear queues before tests
    client.delete(JOBS_QUEUE)
    client.delete(DONE_QUEUE)
    yield client
    client.close()


@pytest.mark.asyncio
async def test_worker_processes_10_jobs(redis_client):
    bucket_id = f"test-bucket-{uuid.uuid4().hex[:8]}"
    object_id = "test_image.png"

    # 1. Create bucket
    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{S3_URL}/buckets", json={"bucket_id": bucket_id})
        assert resp.status in (200, 201)

        # 2. Upload test image
        form = aiohttp.FormData()
        form.add_field("file", make_test_image_bytes(), filename=object_id, content_type="image/png")
        resp = await session.post(f"{S3_URL}/buckets/{bucket_id}/objects", data=form)
        assert resp.status in (200, 201)

    # 3. Send 10 jobs
    operations = [
        ("grayscale", {}),
        ("grayscale", {}),
        ("grayscale", {}),
        ("invert", {}),
        ("invert", {}),
        ("flip_horizontal", {}),
        ("flip_horizontal", {}),
        ("brightness", {"value": 30}),
        ("brightness", {"value": -20}),
        ("crop", {"top": 10, "bottom": 10, "left": 10, "right": 10}),
    ]
    assert len(operations) == NUM_JOBS

    job_ids = []
    for op, params in operations:
        job_id = str(uuid.uuid4())
        job_ids.append(job_id)
        msg = json.dumps({
            "job_id": job_id,
            "bucket_id": bucket_id,
            "object_id": object_id,
            "operation": op,
            "params": params,
        })
        redis_client.lpush(JOBS_QUEUE, msg)

    # 4. Start worker
    worker_env = os.environ.copy()
    worker_env["REDIS_URL"] = REDIS_URL
    worker_env["S3_GATEWAY_URL"] = S3_URL
    worker_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "../worker/worker.py")],
        env=worker_env,
    )

    try:
        # 5. Collect results (timeout 60s)
        results = []
        deadline = time.time() + 60
        while len(results) < NUM_JOBS and time.time() < deadline:
            item = redis_client.brpop(DONE_QUEUE, timeout=5)
            if item:
                _, raw = item
                results.append(json.loads(raw))

        # 6. Assert all 10 completed successfully
        assert len(results) == NUM_JOBS, f"Expected {NUM_JOBS} results, got {len(results)}"
        for r in results:
            assert r["status"] == "success", f"Job failed: {r}"
    finally:
        worker_proc.terminate()
        worker_proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_worker_handles_invalid_operation(redis_client):
    """Worker must not crash on unknown operation; it sends an error result."""
    bucket_id = f"test-bucket-{uuid.uuid4().hex[:8]}"
    object_id = "test_image.png"

    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{S3_URL}/buckets", json={"bucket_id": bucket_id})
        assert resp.status in (200, 201)
        form = aiohttp.FormData()
        form.add_field("file", make_test_image_bytes(), filename=object_id, content_type="image/png")
        resp = await session.post(f"{S3_URL}/buckets/{bucket_id}/objects", data=form)
        assert resp.status in (200, 201)

    job_id = str(uuid.uuid4())
    msg = json.dumps({
        "job_id": job_id,
        "bucket_id": bucket_id,
        "object_id": object_id,
        "operation": "exploit-op",
        "params": {},
    })
    redis_client.lpush(JOBS_QUEUE, msg)

    worker_env = os.environ.copy()
    worker_env["REDIS_URL"] = REDIS_URL
    worker_env["S3_GATEWAY_URL"] = S3_URL
    worker_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "../worker/worker.py")],
        env=worker_env,
    )

    try:
        item = redis_client.brpop(DONE_QUEUE, timeout=15)
        assert item is not None
        result = json.loads(item[1])
        assert result["job_id"] == job_id
        assert result["status"] == "error"
        assert "exploit-op" in result["error"]
    finally:
        worker_proc.terminate()
        worker_proc.wait(timeout=10)
