import json
import os
import shutil
import uuid
from pathlib import Path

import aiofiles
import redis.asyncio as aioredis
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JOBS_QUEUE = "image.jobs"

app = FastAPI(title="S3 Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client: aioredis.Redis = None


@app.on_event("startup")
async def startup() -> None:
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)


@app.on_event("shutdown")
async def shutdown() -> None:
    await redis_client.aclose()


# ── Bucket models ──────────────────────────────────────────────────────────────

class BucketCreate(BaseModel):
    bucket_id: str


class ProcessRequest(BaseModel):
    operation: str
    params: dict = {}


# ── Bucket endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets", status_code=201)
async def create_bucket(body: BucketCreate):
    bucket_path = STORAGE_PATH / body.bucket_id
    if bucket_path.exists():
        raise HTTPException(status_code=409, detail="Bucket already exists")
    bucket_path.mkdir(parents=True)
    return {"bucket_id": body.bucket_id}


@app.get("/buckets")
async def list_buckets():
    buckets = [
        {"bucket_id": p.name}
        for p in STORAGE_PATH.iterdir()
        if p.is_dir()
    ]
    return buckets


@app.delete("/buckets/{bucket_id}", status_code=204)
async def delete_bucket(bucket_id: str):
    bucket_path = STORAGE_PATH / bucket_id
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    shutil.rmtree(bucket_path)


# ── Object endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets/{bucket_id}/objects", status_code=201)
async def upload_object(bucket_id: str, file: UploadFile = File(...)):
    bucket_path = STORAGE_PATH / bucket_id
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    dest = bucket_path / file.filename
    async with aiofiles.open(dest, "wb") as f:
        content = await file.read()
        await f.write(content)
    return {"object_id": file.filename, "bucket_id": bucket_id}


@app.get("/buckets/{bucket_id}/objects")
async def list_objects(bucket_id: str):
    bucket_path = STORAGE_PATH / bucket_id
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    objects = [
        {"object_id": p.name, "size": p.stat().st_size}
        for p in bucket_path.iterdir()
        if p.is_file()
    ]
    return objects


@app.get("/buckets/{bucket_id}/objects/{object_id}")
async def download_object(bucket_id: str, object_id: str):
    file_path = STORAGE_PATH / bucket_id / object_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Object not found")
    return FileResponse(str(file_path))


@app.delete("/buckets/{bucket_id}/objects/{object_id}", status_code=204)
async def delete_object(bucket_id: str, object_id: str):
    file_path = STORAGE_PATH / bucket_id / object_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Object not found")
    file_path.unlink()


@app.post("/buckets/{bucket_id}/objects/{object_id}/process")
async def process_object(bucket_id: str, object_id: str, body: ProcessRequest):
    bucket_path = STORAGE_PATH / bucket_id
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    file_path = bucket_path / object_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Object not found")

    job_id = str(uuid.uuid4())
    message = json.dumps({
        "job_id": job_id,
        "bucket_id": bucket_id,
        "object_id": object_id,
        "operation": body.operation,
        "params": body.params,
    })
    await redis_client.lpush(JOBS_QUEUE, message)
    return {"status": "processing_started", "job_id": job_id}
