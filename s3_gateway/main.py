"""S3 Gateway – REST API for bucket/object management.

Storage layout
--------------
STORAGE_PATH/
  _buckets.json                  ← {bucket_id: {dir: "<uuid>", ...}, ...}
  <bucket-uuid>/
    _objects.json                ← {object_id: {file: "<uuid>.<ext>", size: N}, ...}
    <object-uuid>.<ext>          ← actual file bytes

User-supplied names (bucket_id, object_id) are validated and then used ONLY as
lookup keys in internal JSON metadata.  Actual filesystem paths are always
composed from UUIDs that *we* generate, so user input never reaches path
construction and CodeQL path-injection alerts do not apply.
"""

import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage")).resolve()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
JOBS_QUEUE = "image.jobs"

# Accepted name characters: alphanumeric, hyphen, underscore, dot
# This rejects path separators, null bytes, and traversal sequences.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")

_BUCKETS_META = STORAGE_PATH / "_buckets.json"


# ── Name validation ────────────────────────────────────────────────────────────

def _validate_name(name: str, label: str) -> str:
    """Raise HTTP 400 if *name* contains characters outside the safe set."""
    base = os.path.basename(name)  # strip any accidental path separators
    if not base or base != name or not _SAFE_NAME_RE.match(base):
        raise HTTPException(status_code=400, detail=f"Invalid {label} name: {name!r}")
    return base


# ── Metadata helpers ───────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2))


def _load_buckets() -> dict[str, Any]:
    return _read_json(_BUCKETS_META)


def _save_buckets(meta: dict[str, Any]) -> None:
    _write_json(_BUCKETS_META, meta)


def _objects_meta_path(bucket_dir: Path) -> Path:
    return bucket_dir / "_objects.json"


def _load_objects(bucket_dir: Path) -> dict[str, Any]:
    return _read_json(_objects_meta_path(bucket_dir))


def _save_objects(bucket_dir: Path, meta: dict[str, Any]) -> None:
    _write_json(_objects_meta_path(bucket_dir), meta)


# ── Internal path resolution (paths come from our metadata, NOT from user input) ─

def _bucket_dir_from_meta(bucket_id: str) -> Path:
    """Return the actual bucket directory for *bucket_id*.

    The directory name is a UUID we generated at creation time and stored in
    ``_buckets.json``.  *bucket_id* itself never becomes a path component.
    """
    buckets = _load_buckets()
    if bucket_id not in buckets:
        raise HTTPException(status_code=404, detail="Bucket not found")
    # 'dir' is a UUID string written by us – not user-controlled
    dir_name: str = buckets[bucket_id]["dir"]
    return STORAGE_PATH / dir_name


def _object_path_from_meta(bucket_id: str, object_id: str) -> tuple[Path, str]:
    """Return (file_path, original_filename) for an existing object.

    The filesystem path uses a UUID filename stored in our metadata, so
    *object_id* never becomes a path component.  We additionally apply
    ``os.path.basename`` to the stored filename as a final safety net.
    """
    bucket_dir = _bucket_dir_from_meta(bucket_id)
    objects = _load_objects(bucket_dir)
    if object_id not in objects:
        raise HTTPException(status_code=404, detail="Object not found")
    # 'file' is a UUID filename written by us – strip any unexpected path
    # components with os.path.basename before building the final path.
    raw_file: str = objects[object_id]["file"]
    stored_file = os.path.basename(raw_file)
    if not stored_file:
        raise HTTPException(status_code=500, detail="Internal storage error")
    return bucket_dir / stored_file, objects[object_id].get("filename", object_id)


# ── Application lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app_: "FastAPI"):
    app_.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    yield
    await app_.state.redis.aclose()


app = FastAPI(title="S3 Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ─────────────────────────────────────────────────────────────────────

class BucketCreate(BaseModel):
    bucket_id: str


class ProcessRequest(BaseModel):
    operation: str
    params: dict = {}


# ── Bucket endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets", status_code=201)
async def create_bucket(body: BucketCreate):
    bid = _validate_name(body.bucket_id, "bucket_id")
    buckets = _load_buckets()
    if bid in buckets:
        raise HTTPException(status_code=409, detail="Bucket already exists")
    dir_name = str(uuid.uuid4())  # internal UUID – never user-controlled
    bucket_dir = STORAGE_PATH / dir_name
    bucket_dir.mkdir(parents=True)
    buckets[bid] = {"dir": dir_name}
    _save_buckets(buckets)
    return {"bucket_id": bid}


@app.get("/buckets")
async def list_buckets():
    return [{"bucket_id": k} for k in _load_buckets()]


@app.delete("/buckets/{bucket_id}", status_code=204)
async def delete_bucket(bucket_id: str):
    _validate_name(bucket_id, "bucket_id")
    buckets = _load_buckets()
    if bucket_id not in buckets:
        raise HTTPException(status_code=404, detail="Bucket not found")
    dir_name: str = buckets[bucket_id]["dir"]
    bucket_dir = STORAGE_PATH / dir_name
    if bucket_dir.exists():
        import shutil
        shutil.rmtree(bucket_dir)
    del buckets[bucket_id]
    _save_buckets(buckets)
    return Response(status_code=204)


# ── Object endpoints ───────────────────────────────────────────────────────────

@app.post("/buckets/{bucket_id}/objects", status_code=201)
async def upload_object(bucket_id: str, file: UploadFile = File(...)):
    _validate_name(bucket_id, "bucket_id")
    original_name = _validate_name(file.filename or "upload", "object_id")
    bucket_dir = _bucket_dir_from_meta(bucket_id)

    ext = Path(original_name).suffix.lower()  # e.g. ".png"  – not used in path lookup
    stored_file = f"{uuid.uuid4()}{ext}"      # UUID filename – user can't influence path
    dest = bucket_dir / stored_file           # bucket_dir comes from our metadata
    content = await file.read()
    dest.write_bytes(content)

    objects = _load_objects(bucket_dir)
    objects[original_name] = {
        "file": stored_file,
        "filename": original_name,
        "size": len(content),
        "content_type": file.content_type or "application/octet-stream",
    }
    _save_objects(bucket_dir, objects)
    return {"object_id": original_name, "bucket_id": bucket_id}


@app.get("/buckets/{bucket_id}/objects")
async def list_objects(bucket_id: str):
    _validate_name(bucket_id, "bucket_id")
    bucket_dir = _bucket_dir_from_meta(bucket_id)
    objects = _load_objects(bucket_dir)
    return [
        {"object_id": oid, "size": info.get("size", 0)}
        for oid, info in objects.items()
    ]


@app.get("/buckets/{bucket_id}/objects/{object_id}")
async def download_object(bucket_id: str, object_id: str):
    _validate_name(bucket_id, "bucket_id")
    _validate_name(object_id, "object_id")
    file_path, _ = _object_path_from_meta(bucket_id, object_id)
    return FileResponse(str(file_path))


@app.delete("/buckets/{bucket_id}/objects/{object_id}", status_code=204)
async def delete_object(bucket_id: str, object_id: str):
    _validate_name(bucket_id, "bucket_id")
    _validate_name(object_id, "object_id")
    bucket_dir = _bucket_dir_from_meta(bucket_id)
    objects = _load_objects(bucket_dir)
    if object_id not in objects:
        raise HTTPException(status_code=404, detail="Object not found")
    stored_file = os.path.basename(objects[object_id]["file"])
    if stored_file:
        (bucket_dir / stored_file).unlink(missing_ok=True)
    del objects[object_id]
    _save_objects(bucket_dir, objects)
    return Response(status_code=204)


@app.post("/buckets/{bucket_id}/objects/{object_id}/process")
async def process_object(request: Request, bucket_id: str, object_id: str, body: ProcessRequest):
    _validate_name(bucket_id, "bucket_id")
    _validate_name(object_id, "object_id")
    # Verify both exist (raises 404 if not)
    _object_path_from_meta(bucket_id, object_id)

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
