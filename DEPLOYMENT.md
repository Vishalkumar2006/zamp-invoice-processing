# Deployment

This app is a single FastAPI process. A worker thread inside it processes uploads one at a time. State lives in one
SQLite database plus the uploaded PDFs, both under `DATA_DIR` (`/data` in the image). Deploy it as **exactly one
container with one persistent volume mounted at `/data`, behind HTTPS, with basic auth turned on.** It needs no
other services.

## What the image already contains

- `python:3.12-slim`, the app, **Poppler (`pdftoppm`) and Tesseract (`tesseract-ocr`, `tesseract-ocr-eng`)**, so
  scanned PDFs are read inside the container. `GET /health` reports the tool versions and `"status": "degraded"` if
  either is missing.
- `CMD uvicorn app.main:app --host 0.0.0.0 --port 8000`. The container listens on **8000**.
- `VOLUME /data`. The SQLite database (`app.db`, WAL mode) and the stored PDFs (`uploads/`) are written there.
- The test suite (`docker run --rm <image> python -m pytest -q`) runs inside the same image.

## Requirements for any host

| Requirement | Why |
|---|---|
| **One instance only** (no autoscaling, no second replica, no overlapping rolling deploy) | The worker is in-process and SQLite has no cross-host locking. Two instances on one database are not supported. Use stop-then-start ("recreate") deploys. |
| **Persistent volume at `/data`** on local or block storage, not a network file share | The ledger, decisions, evidence and original PDFs must survive restarts and redeploys. A container's own filesystem is ephemeral, and SQLite locking is unreliable on NFS/SMB. |
| **HTTPS terminated by the platform or a reverse proxy** | Invoices carry vendor and bank data, and basic auth sends credentials with every request. |
| **`APP_PASSWORD` set** | Without it the app is open to anyone who has the URL (see below). |
| ≥ 512 MB RAM (1 GB recommended), 1 shared vCPU | OCR runs at 300 dpi. A scanned page takes about 1.5–17 s on a laptop-class CPU. |
| Small disk (1 GB is plenty for a demo) | Each run stores its PDF, typically a few KB to a few hundred KB. |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | empty | **Set this for any hosted instance.** When non-empty, every route except `GET /health` requires HTTP basic auth. Use a long random value and keep it in the platform's secret store. |
| `APP_USERNAME` | `ap` | Basic-auth user name. |
| `DATA_DIR` | `/data` (image) | Location of `app.db` and `uploads/`. Must be the mounted volume. |
| `MAX_UPLOAD_BYTES` | `20971520` (20 MB) | Upload size limit. |
| `MAX_PAGES` | `10` | Pages read per PDF. |
| `OCR_DPI` | `300` | Rasterisation resolution for scans. |
| `OCR_PAGE_TIMEOUT_S` | `60` | Per-page OCR timeout. A timeout fails the run safely, with no allocation. |
| `MAX_RUN_ATTEMPTS` | `2` | Attempts before a run is marked `FAILED`. |
| `WORKER_POLL_S` | `0.25` | Queue polling interval. |
| `MIN_FIELD_CONFIDENCE` | `80` | OCR confidence floor for critical fields. **Do not lower it.** It is part of the decision policy. |

Workspace settings (`date_order`, `default_currency`) are not environment variables. They are chosen per
procurement import, in the import form or as form fields on `POST /procurement/import`.

## Recommended: Fly.io (one Machine + one volume)

This is a simple fit: it runs the existing Dockerfile unchanged, gives HTTPS on `*.fly.dev` automatically, keeps
secrets out of the repo and attaches a persistent volume. Any platform that meets the requirements above works
the same way (Railway with a volume, Render with a persistent disk, or a small VM; see the alternative below).

1. Install `flyctl` and sign in (`fly auth login`). This needs a Fly.io account with a payment method.
2. From the repository root, create `fly.toml` (not committed; adjust the app name and region):

   ```toml
   app = "zamp-ps1-<your-suffix>"
   primary_region = "iad"

   [build]
     dockerfile = "Dockerfile"

   [env]
     DATA_DIR = "/data"

   [mounts]
     source = "zamp_data"
     destination = "/data"

   [http_service]
     internal_port = 8000
     force_https = true
     auto_stop_machines = "off"     # keep the worker running for the demo
     auto_start_machines = true
     min_machines_running = 1

     [[http_service.checks]]
       grace_period = "20s"
       interval = "30s"
       timeout = "5s"
       method = "GET"
       path = "/health"

   [[vm]]
     size = "shared-cpu-1x"
     memory = "1gb"
   ```

3. Create the app, the volume and the secret, then deploy:

   ```bash
   fly apps create zamp-ps1-<your-suffix>
   fly volumes create zamp_data --region iad --size 1
   fly secrets set APP_USERNAME=ap APP_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
   fly deploy --ha=false            # one Machine; do not scale above 1
   fly scale count 1
   ```

4. Verify:

   ```bash
   curl https://zamp-ps1-<your-suffix>.fly.dev/health   # 200, "status":"ok", tesseract + pdftoppm listed,
                                                        # policy_version matches the commit
   curl -i https://zamp-ps1-<your-suffix>.fly.dev/      # 401 without credentials
   ```

   Then open the site in a browser, log in, import a **synthetic** CSV set and upload a PDF.

5. Persistence check (do this once before recording): process one invoice, run `fly machine restart <id>` (or
   `fly deploy` again), and confirm the run, its decision and the ledger are still shown in History.

## Alternative: a small VM with Docker Compose and Caddy

On any Linux VM with Docker and a DNS name pointing at it:

```bash
git clone https://github.com/Vishalkumar2006/zamp-invoice-processing.git && cd zamp-invoice-processing
printf 'APP_USERNAME=ap\nAPP_PASSWORD=%s\n' "$(openssl rand -base64 24)" > .env   # never commit .env
docker compose up --build -d          # named volume zamp_data at /data, restart: unless-stopped, health check
```

Put Caddy in front for automatic HTTPS. A `Caddyfile` with `your.domain { reverse_proxy localhost:8000 }` is
enough. Then close port 8000 to the internet, so only 443 (Caddy) is exposed. Compose already defines the
volume, the restart policy and the `/health` check.

## Access control and sensitive data

- **Basic auth is on only when `APP_PASSWORD` is set**, and a missing secret means an open app. After every deploy,
  check that `/` returns 401 without credentials. Use HTTPS only. The credential is shared: there are no
  per-user accounts or audit identities.
- **Full routing and account numbers stay inside the database and the original PDFs.** The API and UI show
  masked values (`****1234`), including inside evidence text. Treat the volume, its snapshots and any downloaded
  PDFs as confidential.
- **Use synthetic or public demo data on a hosted instance.** Do not upload private evaluation fixtures or real
  vendor bank details.
- To wipe the data, delete and recreate the volume (`fly volumes destroy <id>`; `docker compose down -v` on a VM).

## Restarts and persistence

- Decisions, allocations, evidence, events and PDFs are committed to SQLite on `/data`. A restart, crash or
  redeploy on the same volume keeps every processed invoice, its stored decision and the ledger.
- Runs that were `QUEUED` or `RUNNING` when the process stopped are re-queued at startup. A run whose decision
  was already committed is marked completed, never decided twice. `POST /runs/{id}/finalize` is idempotent.
- Back up by snapshotting the volume (Fly takes daily volume snapshots), or by copying `/data` while the app is
  stopped.

## Health checks and troubleshooting

| Symptom | Check |
|---|---|
| Platform health check failing | `GET /health` must return 200. It answers before auth. Look at the logs (`fly logs`, `docker compose logs app`). |
| `"status": "degraded"` | Tesseract or Poppler is missing from the image that was deployed. Rebuild from the repo `Dockerfile`. Scanned PDFs cannot be read until it is fixed. |
| Every page returns 401 | `APP_PASSWORD` is set. Log in with `APP_USERNAME`/`APP_PASSWORD`, or check the secret value. |
| History is empty after a deploy | The volume is not mounted at `/data`, or a new volume was created. `GET /health` shows `data_dir`; check the platform's mount configuration. |
| A run ends `FAILED` | A technical error (unreadable PDF, OCR timeout). It wrote no allocation. The error is on the run page. `POST /runs/{id}/retry` retries once the cause is fixed. |
| "database is locked" in the logs | More than one instance is using the database. Scale back to exactly one. |
| Slow scanned uploads | OCR is CPU-bound. Use a larger CPU or accept a few seconds per page. Native-text PDFs take well under a second. |

## Limitations and costs

- One replica on SQLite: it suits a demo or a small team, not horizontal scaling.
- Basic auth with one shared credential. Add an identity provider before any real use.
- Hosted platforms charge for an always-on instance and a persistent volume. Free tiers typically have no
  persistent disk or sleep on idle. Expect a small monthly cost (a few US dollars for one 1 GB instance and a
  1 GB volume at the time of writing). Check the platform's current pricing before creating resources.
- OCR quality depends on scan quality. Low-confidence critical fields are held for review, by design.
