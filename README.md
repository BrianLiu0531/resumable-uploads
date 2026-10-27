# Resumable Uploads

![CI](https://github.com/BrianLiu0531/resumable-uploads/actions/workflows/ci.yml/badge.svg)

Upload large files over unreliable connections. Pause, lose the network or close the tab, and the upload continues from the last byte the server stored. It implements the open **[tus 1.0](https://tus.io/protocols/resumable-upload)** protocol in **Django**, stores files in **Amazon S3 with multipart uploads**, and ships a **React** client with a live protocol log.

**Live demo:** _https://resumable-uploads.onrender.com_ (free instance: the first request after idling takes about a minute; files are deleted after 24 hours)

**Stack:** Python · Django · PostgreSQL · Amazon S3 (multipart) · React · TypeScript · tus-js-client · Terraform · Docker · GitHub Actions

## How tus works

```
POST  /files/                     Upload-Length: 12595257          → 201 Location: /files/<id>
PATCH /files/<id>  Upload-Offset: 0        (5 MiB of bytes)        → 204 Upload-Offset: 5242880
PATCH /files/<id>  Upload-Offset: 5242880  ── connection drops ──
HEAD  /files/<id>                                                   → 200 Upload-Offset: 7340032   ← kept what arrived
PATCH /files/<id>  Upload-Offset: 7340032  (rest of the file)      → 204 Upload-Offset: 12595257
```

The client always asks the server where to continue, so an upload survives anything that interrupts it: a dropped connection, a paused upload, or a closed tab (tus-js-client remembers the upload URL per file).

How much of an interrupted request is kept depends on what sits in front of the app. Served directly, the server keeps every byte that arrived. Behind a proxy that buffers request bodies, as Render's does, an aborted request never reaches the app, so only the in-flight chunk (5 MiB) is resent. Both cases are handled, and the live demo shows the second.

Supported: core protocol plus the **creation**, **expiration**, **termination** and **checksum** (md5/sha1/sha256) extensions, and `X-HTTP-Method-Override` for proxies that block `PATCH`.

## The parts that are easy to get wrong

| Problem | What naïve code does | What this does |
|---|---|---|
| **Concurrent writes.** Two requests for the same upload at once: a retry overlapping the original, or two tabs. | Checks the offset, then appends. Both pass the check and the chunk is written twice, so the file is silently corrupted. | `SELECT … FOR UPDATE NOWAIT` on the upload row, then the offset check, then the write, all in one transaction. The loser gets `409` or `423`, asks for the real offset and retries. A test sends the same chunk from two threads and checks it is stored once. |
| **Dropped connections.** | Throws away a half-received chunk, or keeps it without updating the offset. | Keeps the bytes that arrived and advances the offset to match, as tus intends. If a checksum was promised, a partial body cannot match it, so nothing is stored. |
| **Slow clients holding locks.** | Streams the request body while holding a database transaction. | The body is received into a temporary file first. The lock is held only for the write. |
| **S3's 5 MiB minimum part size.** Clients send chunks of any size. | Uploads each chunk as a part, which fails at completion, or buffers on local disk, which is lost on restart. | Full 5 MiB parts go straight to the multipart upload. Leftover bytes are kept as a separate pending object (`<key>.part.<offset>`) and prepended to the next chunk. The server holds no state between requests. |
| **Crash between S3 and the database.** | A retry appends the chunk again, or loses the pending bytes. | Part numbers come from the committed parts, and S3 replaces a re-uploaded part, so a retry rewrites the same part. Each pending object has its own key and the old one is deleted only after the commit. If S3 completed the object but the commit failed, the retry recognises the finished object. Both crash points are tested. |
| **Hostile uploads.** | Serves uploaded files inline from the app's origin. | Files download with `Content-Disposition: attachment` and `application/octet-stream`, from short-lived presigned S3 URLs. Filenames are sanitised. |

This is a ground-up rewrite of a resumable-upload prototype I wrote in an earlier role. That prototype had exactly the check-then-append race above.

## Keeping a public demo inside the free tier

The demo is open to anyone, so cost is capped by design rather than by hope:

| Control | Where | Effect |
|---|---|---|
| 50 MiB per file, 16 MiB per request | app | No single upload is large |
| **1 GiB per rolling 24 h, all visitors combined** | app (`DAILY_BYTE_QUOTA`) | Caps how much can ever be stored |
| 20 uploads per hour per client (IP hashed, never stored) | app | Stops one visitor using the quota |
| Uploads and files deleted after 24 h | app (`UPLOAD_TTL_HOURS`, cleanup on each new upload) | Storage never accumulates |
| **Objects expire after 1 day; unfinished multipart uploads are aborted after 1 day** | S3 lifecycle rules | A safety net if the app's cleanup never runs. Unfinished multipart parts are billed but invisible in the console |
| No versioning, no public access, SSE-S3 | S3 bucket | Deleted data is really gone; nothing is publicly readable |
| US$5 monthly budget alert | AWS Budgets | Email before anything surprising happens |

Worst case, about 1 GiB stored for a day plus a few thousand requests a month comes to **a few US cents a month**. Upload traffic into S3 is free, and downloads fall within the 100 GB/month of free data transfer out.

## Tests

```bash
cd backend && pytest        # 57 tests against PostgreSQL, each run against local storage and S3 (moto)
cd frontend && npm test
```

- **Protocol:** options, version negotiation, creation validation, reassembly from any chunking (S3 parts exactly 5 MiB), HEAD for resuming, offset conflicts, content type, oversize bodies, checksums, termination (multipart upload aborted, no objects left), expiry and cleanup, empty files, method override, downloads.
- **Robustness:**
  - bytes are kept after a mid-request disconnect
  - a disconnect with a checksum stores nothing
  - **two concurrent writers of the same chunk**
  - a crash between storage and commit, and between S3 completion and commit
  - per-client rate limits, the daily quota, IPs not stored, filename sanitising
- **End to end, in Chrome:** a 12 MB file throttled to 1.5 MB/s, with the connection dropped at 30%. Locally, the server kept the 3.9 MB that had arrived and the client resumed from there. On the live demo, the dropped chunk was resent. In both cases the object in S3 and the downloaded file had the original SHA-256.

The first live run found a bug the tests had missed. Render sets `AWS_REGION`, but boto3 only reads `AWS_DEFAULT_REGION`, so presigned download URLs were signed for `us-east-1` and rejected by S3 (uploads still worked because S3 redirects). The region is now passed explicitly, and the S3 tests run with only `AWS_REGION` set and no local AWS config, as on Render. A test checks the region in the signature.

## Running locally

```bash
docker compose up -d                       # PostgreSQL on :5440
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # bin/ on macOS/Linux
DEBUG=1 python manage.py migrate && DEBUG=1 python manage.py runserver 8000
cd frontend && npm install && npm run dev  # http://localhost:5190, proxies /files and /api to Django
```

Uploads go to `backend/media/` unless `UPLOAD_STORAGE=s3`. All settings are in [`.env.example`](.env.example).

## Deploying

1. **AWS** (once): `terraform -chdir=terraform apply` creates the bucket (with the lifecycle rules) and an IAM user that can only touch `uploads/*` in it. On Windows with HTTPS-scanning antivirus, use `scripts/terraform-in-docker.sh`. Then `scripts/create-access-key.sh ../deploy.env` creates the user's access key and writes it outside the repository, so it never enters Terraform state or git.
2. **Render:** New → Blueprint → this repo. Fill in `DATABASE_URL` (PostgreSQL, e.g. Neon), `S3_BUCKET`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The Docker image builds the React client, runs migrations on start and serves everything from one origin.

## Layout

```
backend/uploads/tus.py      the tus protocol: creation, HEAD, PATCH (lock → offset check → write), DELETE
backend/uploads/storage.py  local and S3 multipart storage, both retry-safe
backend/uploads/util.py     client hashing, filename sanitising, expiry cleanup
backend/uploads/tests/      protocol and robustness tests
frontend/src/App.tsx        tus-js-client UI: progress, pause/resume, simulated network drop, protocol log
terraform/main.tf           S3 bucket, lifecycle rules, least-privilege IAM user
```
