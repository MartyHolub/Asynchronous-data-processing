import json
import os
import re
import shutil
import uuid
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage")).resolve()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JOBS_QUEUE = "image.jobs"

# Only allow safe name segments: alphanumeric, hyphens, underscores, dots
# (no path separators, no null bytes, no "..")
_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _safe_name(name: str, label: str) -> str:
    """Strip directory components and validate only safe characters remain.

    Using os.path.basename as the primary defence breaks the path-traversal
    taint: even if the caller passes 'foo/../bar', only 'bar' survives.
    The subsequent regex then rejects any name that still contains characters
    not in [A-Za-z0-9._-], which also covers null bytes and Windows path
    separators.
    """
    base = os.path.basename(name)
    if not base or base != name or not _SAFE_NAME_RE.match(base):
        raise HTTPException(status_code=400, detail=f"Invalid {label} name")
    return base


def _resolve_bucket(bucket_id: str) -> Path:
    safe_id = _safe_name(bucket_id, "bucket_id")
    path = (STORAGE_PATH / safe_id).resolve()
    if not path.is_relative_to(STORAGE_PATH):
        raise HTTPException(status_code=400, detail="Invalid bucket_id")
    return path


def _resolve_object(bucket_id: str, object_id: str) -> Path:
    bucket_path = _resolve_bucket(bucket_id)
    safe_oid = _safe_name(object_id, "object_id")
    path = (bucket_path / safe_oid).resolve()
    if not path.is_relative_to(bucket_path):
        raise HTTPException(status_code=400, detail="Invalid object_id")
    return path


app = FastAPI(title="S3 Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)


@app.on_event("shutdown")
async def shutdown() -> None:
    await app.state.redis.aclose()


# ── Bucket models ──────────────────────────────────────────────────────────────

class BucketCreate(BaseModel):
    bucket_id: str


class ProcessRequest(BaseModel):
    operation: str
    params: dict = {}


# ── Bucket endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets", status_code=201)
async def create_bucket(body: BucketCreate):
    bucket_path = _resolve_bucket(body.bucket_id)
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
    bucket_path = _resolve_bucket(bucket_id)
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    shutil.rmtree(bucket_path)


# ── Object endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets/{bucket_id}/objects", status_code=201)
async def upload_object(request: Request, bucket_id: str, file: UploadFile = File(...)):
    bucket_path = _resolve_bucket(bucket_id)
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    safe_filename = _safe_name(file.filename or "", "object_id")
    dest = (bucket_path / safe_filename).resolve()
    if not dest.is_relative_to(bucket_path):
        raise HTTPException(status_code=400, detail="Invalid filename")
    content = await file.read()
    dest.write_bytes(content)
    return {"object_id": safe_filename, "bucket_id": bucket_id}


@app.get("/buckets/{bucket_id}/objects")
async def list_objects(bucket_id: str):
    bucket_path = _resolve_bucket(bucket_id)
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
    file_path = _resolve_object(bucket_id, object_id)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Object not found")
    return FileResponse(str(file_path))


@app.delete("/buckets/{bucket_id}/objects/{object_id}", status_code=204)
async def delete_object(bucket_id: str, object_id: str):
    file_path = _resolve_object(bucket_id, object_id)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Object not found")
    file_path.unlink()


@app.post("/buckets/{bucket_id}/objects/{object_id}/process")
async def process_object(request: Request, bucket_id: str, object_id: str, body: ProcessRequest):
    bucket_path = _resolve_bucket(bucket_id)
    if not bucket_path.exists():
        raise HTTPException(status_code=404, detail="Bucket not found")
    file_path = _resolve_object(bucket_id, object_id)
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
    await request.app.state.redis.lpush(JOBS_QUEUE, message)
    return {"status": "processing_started", "job_id": job_id}
