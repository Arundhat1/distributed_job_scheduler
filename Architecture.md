# Architecture
 
## Component overview
 
![Architecture Component Overview](watermarked_img_11697363755003921479.png)
 
## Why this shape
 
**Workers are HTTP clients of the API, not direct DB clients.** A worker
never touches Postgres — it calls `/api/v1/workers/claim`,
`/jobs/{id}/start`, and `/jobs/{id}/result`. This keeps the atomic-claim
logic, retry/DLQ transitions, and authorization in one place (the API
process) instead of duplicated in every worker, and lets workers be
deployed as thin, disposable processes (or containers, or serverless
tasks) that only need an API token, not a database credential.
 
**The scheduler is a separate concern from request handling.** Promoting
due jobs and evaluating cron schedules is a background loop, not
something triggered by a request. It's implemented once
(`app/services/scheduler_service.py`) and can run two ways:
- *embedded* — an `asyncio` task started in the API process's lifespan
  hook, for single-container/dev convenience (`docker compose up` with
  nothing else running still schedules things correctly).
- *standalone* — `scripts/run_scheduler.py`, for production, so
  scheduling work doesn't compete with request-handling CPU/IO and can be
  restarted or scaled independently of the API.
Both are safe to run simultaneously: every scheduler write is a targeted,
`WHERE`-guarded `UPDATE` (e.g. "promote all `SCHEDULED` jobs whose
`run_at <= now()`"), which is naturally idempotent — running it twice in
the same instant just updates zero extra rows the second time.
 
**Real-time updates use a single pub/sub layer, not per-page polling
logic.** Every router that mutates job/worker state calls
`ws_manager.broadcast(...)`. The dashboard's `useLiveEvents` hook opens one
WebSocket and every page subscribes to the same event stream, falling back
to a periodic REST refresh as a backstop if the socket drops. This is
explicitly called out as a single-process in-memory implementation (see
`docs/DESIGN_DECISIONS.md`) with a stated upgrade path to Redis pub/sub for
multi-instance API deployments.
 
**Everything that can race is resolved in Postgres, not in application
code.** Concurrency safety (atomic claim), idempotent job submission
(unique `(queue_id, idempotency_key)` constraint), and idempotent
scheduling (guarded UPDATEs) all lean on database guarantees rather than
distributed locks, a message queue, or in-memory coordination — see
"Why Postgres SKIP LOCKED instead of a message broker" in
`docs/DESIGN_DECISIONS.md`.
 
## Request/claim flow (sequence)
 
```mermaid
sequenceDiagram
    participant U as User (dashboard)
    participant API as API
    participant DB as Postgres
    participant W as Worker process
 
    U->>API: POST /queues/{id}/jobs
    API->>DB: INSERT job (status=QUEUED or SCHEDULED)
    API-->>U: 201 Job
 
    loop poll every 1s
        W->>API: POST /workers/claim {queue_names, max_jobs}
        API->>DB: SELECT ... FOR UPDATE SKIP LOCKED (candidates)
        API->>DB: UPDATE jobs SET status=CLAIMED, claimed_by_worker_id=W
        API-->>W: [job...]
    end
 
    W->>API: POST /workers/{id}/jobs/{job}/start
    API->>DB: status=RUNNING, attempt_count+=1, INSERT JobExecution
    W->>W: run handler(payload)
    W->>API: POST /workers/{id}/jobs/{job}/result {status, result|error}
    API->>DB: success -> COMPLETED / failure -> RETRY_SCHEDULED or DEAD_LETTER
    API-->>U: WebSocket broadcast: job_finished
