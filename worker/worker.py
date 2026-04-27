import asyncio
import json
import os
import io
import uuid
import logging
from pathlib import Path

import aiohttp
import numpy as np
from PIL import Image
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
S3_GATEWAY_URL = os.getenv("S3_GATEWAY_URL", "http://localhost:8000")
JOBS_QUEUE = "image.jobs"
DONE_QUEUE = "image.done"


def invert(img_array: np.ndarray) -> np.ndarray:
    return 255 - img_array


def flip_horizontal(img_array: np.ndarray) -> np.ndarray:
    return img_array[:, ::-1, :]


def crop(img_array: np.ndarray, params: dict) -> np.ndarray:
    top = int(params.get("top", 0))
    bottom = int(params.get("bottom", 0))
    left = int(params.get("left", 0))
    right = int(params.get("right", 0))
    h, w = img_array.shape[:2]
    if top + bottom >= h or left + right >= w:
        raise ValueError(
            f"Crop parameters exceed image dimensions ({w}x{h}): "
            f"top={top}, bottom={bottom}, left={left}, right={right}"
        )
    row_end = h - bottom if bottom > 0 else h
    col_end = w - right if right > 0 else w
    return img_array[top:row_end, left:col_end, :]


def brightness(img_array: np.ndarray, params: dict) -> np.ndarray:
    value = int(params.get("value", 50))
    tmp = img_array.astype(np.int16) + value
    return np.clip(tmp, 0, 255).astype(np.uint8)


def grayscale(img_array: np.ndarray) -> np.ndarray:
    gray = (
        0.299 * img_array[:, :, 0].astype(np.float32)
        + 0.587 * img_array[:, :, 1].astype(np.float32)
        + 0.114 * img_array[:, :, 2].astype(np.float32)
    )
    gray_uint8 = gray.astype(np.uint8)
    return np.stack([gray_uint8, gray_uint8, gray_uint8], axis=-1)


OPERATIONS = {
    "invert": lambda arr, p: invert(arr),
    "flip_horizontal": lambda arr, p: flip_horizontal(arr),
    "crop": crop,
    "brightness": brightness,
    "grayscale": lambda arr, p: grayscale(arr),
}


async def download_image(session: aiohttp.ClientSession, bucket_id: str, object_id: str) -> np.ndarray:
    url = f"{S3_GATEWAY_URL}/buckets/{bucket_id}/objects/{object_id}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.array(img)


async def upload_image(
    session: aiohttp.ClientSession,
    bucket_id: str,
    object_id: str,
    img_array: np.ndarray,
    original_object_id: str,
) -> None:
    img = Image.fromarray(img_array)
    buf = io.BytesIO()
    ext = Path(original_object_id).suffix.lower() or ".png"
    fmt = "JPEG" if ext in (".jpg", ".jpeg") else "PNG"
    img.save(buf, format=fmt)
    buf.seek(0)
    url = f"{S3_GATEWAY_URL}/buckets/{bucket_id}/objects"
    form = aiohttp.FormData()
    form.add_field(
        "file",
        buf,
        filename=object_id,
        content_type="image/jpeg" if fmt == "JPEG" else "image/png",
    )
    async with session.post(url, data=form) as resp:
        resp.raise_for_status()


async def process_job(session: aiohttp.ClientSession, redis_client, message: dict) -> None:
    job_id = message.get("job_id", str(uuid.uuid4()))
    bucket_id = message["bucket_id"]
    object_id = message["object_id"]
    operation = message.get("operation", "")
    params = message.get("params", {}) or {}

    result = {"job_id": job_id, "bucket_id": bucket_id, "object_id": object_id, "status": "error", "error": None}

    try:
        op_fn = OPERATIONS.get(operation)
        if op_fn is None:
            raise ValueError(f"Unknown operation: '{operation}'")

        img_array = await download_image(session, bucket_id, object_id)
        processed = op_fn(img_array, params)

        ext = Path(object_id).suffix
        stem = Path(object_id).stem
        new_object_id = f"{stem}_{operation}{ext}"
        await upload_image(session, bucket_id, new_object_id, processed, object_id)

        result["status"] = "success"
        result["result_object_id"] = new_object_id
        logger.info("Job %s completed: %s -> %s", job_id, object_id, new_object_id)

    except Exception as exc:
        result["error"] = str(exc)
        logger.error("Job %s failed: %s", job_id, exc)

    await redis_client.lpush(DONE_QUEUE, json.dumps(result))


async def main() -> None:
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Worker started. Listening on '%s'…", JOBS_QUEUE)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                item = await redis_client.brpop(JOBS_QUEUE, timeout=5)
                if item is None:
                    continue
                _, raw = item
                message = json.loads(raw)
                logger.info("Received job: %s", message)
                await process_job(session, redis_client, message)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in main loop: %s", exc)


if __name__ == "__main__":
    asyncio.run(main())
