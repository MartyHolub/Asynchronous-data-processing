# Asynchronous Image Processing

A microservice system for asynchronous image processing built with **FastAPI**, **Redis**, and **React**.

---

## Architecture

```
┌─────────────┐     HTTP      ┌──────────────┐    Redis Queue    ┌────────────┐
│   Frontend  │ ────────────► │  S3 Gateway  │ ────────────────► │   Worker   │
│  (React/    │               │  (FastAPI)   │                   │  (Python)  │
│   Vite)     │ ◄──────────── │              │ ◄──────────────── │            │
└─────────────┘               └──────────────┘   Result Queue    └────────────┘
                                      │
                                      ▼
                               ./storage/ (files)
```

| Component    | Technology        | Role                                                         |
|-------------|-------------------|--------------------------------------------------------------|
| `s3_gateway` | FastAPI + Python  | REST API: bucket & object management, enqueues image jobs    |
| `worker`     | Python + aiohttp  | Consumes jobs from Redis, processes images, uploads results  |
| `frontend`   | React + Vite      | Web UI for managing buckets, uploading images, triggering jobs |
| Redis        | Redis 7           | Message broker between S3 Gateway and Worker                 |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/MartyHolub/Asynchronous-data-processing.git
cd Asynchronous-data-processing

# Start all services
docker compose up --build
```

Once running:
- **Frontend UI** → http://localhost:5173 *(if running the dev server separately, see below)*
- **S3 Gateway API** → http://localhost:8000
- **API docs (Swagger)** → http://localhost:8000/docs

### Running the Frontend in Development Mode

```bash
cd frontend
npm install
npm run dev
```

The dev server proxies `/api` requests to the S3 Gateway at `http://localhost:8000`.

---

## How to Use (Web UI)

1. **Create a bucket** – Enter a name in the *Bucket* field (e.g. `my-bucket`) and click **Create / Select**.
2. **Upload an image** – In the *Upload Image* section click the file picker and choose a PNG or JPEG file.
3. **Process an image** – Each uploaded image shows operation buttons: `Grayscale`, `Invert`, `Flip H`, `Brighten +50`, `Crop 20px`. Click one to submit a job.
4. **View results** – After a few seconds click **🔄 Refresh** to see the processed image appear alongside the original.
5. **Track jobs** – The *Processing Jobs* table at the bottom shows every submitted job with its status and job ID.

---

## REST API Reference

All endpoints are served by the **S3 Gateway** at `http://localhost:8000`.

### Buckets

| Method   | Endpoint               | Description                  |
|----------|------------------------|------------------------------|
| `POST`   | `/buckets`             | Create a new bucket          |
| `GET`    | `/buckets`             | List all buckets             |
| `DELETE` | `/buckets/{bucket_id}` | Delete a bucket and its files|

**Create bucket example:**
```bash
curl -X POST http://localhost:8000/buckets \
  -H "Content-Type: application/json" \
  -d '{"bucket_id": "my-bucket"}'
```

### Objects

| Method   | Endpoint                                          | Description              |
|----------|---------------------------------------------------|--------------------------|
| `POST`   | `/buckets/{bucket_id}/objects`                    | Upload a file            |
| `GET`    | `/buckets/{bucket_id}/objects`                    | List objects in a bucket |
| `GET`    | `/buckets/{bucket_id}/objects/{object_id}`        | Download a file          |
| `DELETE` | `/buckets/{bucket_id}/objects/{object_id}`        | Delete a file            |

**Upload file example:**
```bash
curl -X POST http://localhost:8000/buckets/my-bucket/objects \
  -F "file=@photo.png"
```

### Processing

| Method | Endpoint                                               | Description              |
|--------|--------------------------------------------------------|--------------------------|
| `POST` | `/buckets/{bucket_id}/objects/{object_id}/process`     | Submit an image job      |

**Request body:**
```json
{
  "operation": "grayscale",
  "params": {}
}
```

**Response:**
```json
{
  "status": "processing_started",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

The processed image is saved back to the same bucket under a new name:  
`{original_stem}_{operation}{extension}` — e.g. `photo_grayscale.png`.

---

## Supported Image Operations

| Operation         | `"operation"` value  | Parameters                                                    |
|-------------------|----------------------|---------------------------------------------------------------|
| Grayscale         | `grayscale`          | none                                                          |
| Invert colours    | `invert`             | none                                                          |
| Flip horizontal   | `flip_horizontal`    | none                                                          |
| Adjust brightness | `brightness`         | `value` (integer, positive = brighter, negative = darker)     |
| Crop              | `crop`               | `top`, `bottom`, `left`, `right` (pixels to remove per edge)  |

**Brightness example:**
```bash
curl -X POST http://localhost:8000/buckets/my-bucket/objects/photo.png/process \
  -H "Content-Type: application/json" \
  -d '{"operation": "brightness", "params": {"value": -30}}'
```

**Crop example:**
```bash
curl -X POST http://localhost:8000/buckets/my-bucket/objects/photo.png/process \
  -H "Content-Type: application/json" \
  -d '{"operation": "crop", "params": {"top": 10, "bottom": 10, "left": 10, "right": 10}}'
```

---

## Running Tests

Integration tests require the S3 Gateway and Redis to be running.

```bash
# Start services
docker compose up -d redis s3_gateway

# (Recommended) create and activate a local virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install test dependencies
python -m pip install -r tests/requirements.txt

# Run tests
python -m pytest tests/
```

The test suite sends 10 concurrent image-processing jobs and verifies that all complete successfully.

If you see a `bad interpreter` error for `pytest`, your virtual environment likely points to a removed Python binary. Recreate the venv (`rm -rf .venv && python3 -m venv .venv`) and run tests with `python -m pytest` to ensure the active interpreter is used.

If pytest reports `collected 0 items / 1 skipped`, install test dependencies from `tests/requirements.txt` in the currently active environment.

---

## AI Report

GitHub Copilot (Claude Sonnet model) was used to write this `README.md` file. The AI explored the repository structure — reading `docker-compose.yml`, `s3_gateway/main.py`, `worker/worker.py`, `frontend/src/App.jsx`, and the test file — to understand the architecture and then generated the full documentation. No AI was used for any of the application source code.
