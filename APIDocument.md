# API Documentation

Base URL: `http://localhost:8000`. Interactive Swagger UI (auto-generated
from the same code, always in sync): **`/docs`**. OpenAPI JSON: `/openapi.json`.

All endpoints except `/health`, `/api/v1/auth/register`, and
`/api/v1/auth/login` require:

```
Authorization: Bearer <access_token>
```

## Conventions

- **Validation** — every request body is a Pydantic model; invalid input
  returns `422` with a field-level error list (FastAPI default).
- **Errors** — handled failures return a consistent envelope:
  `{"error": "<message>", "code": "<http status>"}` (see
  `app/main.py::http_exception_handler`).
- **Pagination** — list-of-many endpoints (currently: job listing) accept
  `?page=1&page_size=20` (`page_size` capped at 200) and respond with:
  ```json
  { "items": [...], "total": 137, "page": 1, "page_size": 20, "pages": 7 }
  ```
- **Filtering** — job listing accepts `?status=` and `?job_type=`.
- **Rate limiting** — 300 requests/minute per client IP by default
  (slowapi), returns `429` when exceeded.
- **Idempotency** — pass `idempotency_key` when creating a job; a repeat
  POST with the same key on the same queue returns the original job
  instead of creating a duplicate (`409`-free, safe to retry client-side).

---

## Auth

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/auth/register` | Create a user + a new organization they own. Body: `email, password, full_name, organization_name`. |
| POST | `/api/v1/auth/login` | OAuth2 password flow (`username`=email, `password`). Returns `{access_token, token_type}`. |
| GET | `/api/v1/auth/me` | Current authenticated user. |

## Projects

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/projects` | List projects across every org the caller belongs to. |
| POST | `/api/v1/projects` | Create a project. Body: `name, description?`. |

## Queues

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/projects/{project_id}/queues` | List queues in a project. |
| POST | `/api/v1/projects/{project_id}/queues` | Create a queue. Body: `name, priority?, max_concurrency?, retry_policy? {strategy, base_delay_seconds, multiplier, max_delay_seconds, max_retries}`. |
| PATCH | `/api/v1/projects/{project_id}/queues/{queue_id}` | Partial update: `priority?, max_concurrency?, is_paused?`. |
| POST | `.../queues/{queue_id}/pause` | Pause a queue (workers stop claiming from it). |
| POST | `.../queues/{queue_id}/resume` | Resume a paused queue. |
| GET | `.../queues/{queue_id}/stats` | Counts by status, average execution duration, throughput in the last hour. |

## Jobs

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/queues/{queue_id}/jobs` | Submit a job. See "Job types" below. |
| POST | `/api/v1/queues/{queue_id}/jobs/batch` | Submit a batch: `{batch_name, jobs: [JobCreate, ...]}`. Groups jobs under a `Batch` row. |
| GET | `/api/v1/queues/{queue_id}/jobs?status=&job_type=&page=&page_size=` | Paginated, filterable job listing. |
| GET | `/api/v1/queues/{queue_id}/jobs/{job_id}` | Job detail including every execution attempt. |
| POST | `.../jobs/{job_id}/cancel` | Cancel a job that hasn't finished (`409` if already completed/dead-lettered/cancelled). |

### Job types (`JobCreate.job_type`)

| Type | Required fields | Behavior |
|---|---|---|
| `immediate` | — | `run_at` defaults to now; claimable as soon as a worker polls. |
| `delayed` | `run_at` | Created as `SCHEDULED`; promoted to `QUEUED` once `run_at` passes. |
| `scheduled` | `run_at` | Same mechanics as `delayed`; separate label for a one-off future run vs. a short delay. |
| `recurring` | — (created via the scheduled-jobs endpoints below, not directly) | Spawned automatically by a `ScheduledJob` cron template. |
| `batch` | — | Set automatically by `POST .../jobs/batch`; jobs execute independently, `Batch` just groups them for reporting. |

Example — submit an immediate job:
```bash
curl -X POST localhost:8000/api/v1/queues/1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "send_email", "payload": {"to": "a@b.com"}, "priority": 5, "max_retries": 3}'
```

Example — submit a delayed job 10 minutes out:
```bash
curl -X POST localhost:8000/api/v1/queues/1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "reminder", "job_type": "delayed", "run_at": "2026-08-24T15:00:00Z", "payload": {}}'
```

## Recurring (cron) job definitions

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/queues/{queue_id}/scheduled-jobs` | Create a cron template: `{name, cron_expression, payload?, timezone?}`. `cron_expression` is validated (`croniter`) and rejected with `422` if malformed. |
| GET | `/api/v1/queues/{queue_id}/scheduled-jobs` | List cron templates for a queue. |
| POST | `.../scheduled-jobs/{id}/pause` | Stop spawning new job instances from this template. |

## Dead Letter Queue

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/queues/{queue_id}/dead-letter` | List permanently-failed jobs, most recent first. |
| POST | `.../dead-letter/{entry_id}/requeue` | Re-submit a fresh job with the same payload; marks the DLQ entry `requeued_at`. |

## Workers (called by the worker process, not the dashboard)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/workers/register` | Register a worker instance: `{name, hostname, pid, queues: [...], concurrency_limit}`. |
| POST | `/api/v1/workers/{id}/heartbeat` | Report liveness: `{status, active_jobs, cpu_percent?, memory_mb?}`. Appends a `WorkerHeartbeat` row and updates the worker's latest snapshot. |
| POST | `/api/v1/workers/claim` | **Atomic claim.** `{worker_id, queue_names: [...], max_jobs}` → array of claimed jobs. See `docs/DESIGN_DECISIONS.md` for the locking strategy. |
| POST | `/api/v1/workers/{id}/jobs/{job_id}/start` | Mark a claimed job `RUNNING` and record a new `JobExecution` attempt. |
| POST | `/api/v1/workers/{id}/jobs/{job_id}/result` | Report outcome: `{status: succeeded\|failed\|timed_out, result?, error_message?, error_stacktrace?}`. Drives the retry/DLQ state machine. |
| GET | `/api/v1/workers` | List all registered workers (used by the dashboard). |

## Dashboard

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/dashboard/summary` | Global job counts by status, worker counts by status, hourly completed-job throughput for the last 24h. |
| WS | `/api/v1/dashboard/ws` | Live event stream. Frame shapes: `worker_registered`, `jobs_claimed`, `job_started`, `job_finished` (each includes relevant ids/status). |

## Error responses

| Status | Meaning |
|---|---|
| 400/422 | Validation error (bad body, invalid cron expression, missing `run_at` for a delayed job, etc). |
| 401 | Missing/invalid/expired token. |
| 403 | Authenticated but not a member of the resource's organization, or a `viewer` attempting a write. |
| 404 | Resource not found (or not owned by an org the caller belongs to). |
| 409 | Conflicting state transition (e.g. cancelling an already-completed job). |
| 429 | Rate limit exceeded. |
