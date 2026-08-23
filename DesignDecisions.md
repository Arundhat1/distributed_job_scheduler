# Design Decisions & Trade-offs

## 1. Postgres `SKIP LOCKED` instead of a message broker (SQS/RabbitMQ/Kafka)

**Decision:** atomic claiming is implemented as
`SELECT ... FOR UPDATE SKIP LOCKED` against the `jobs` table, not a
separate queueing system.

**Why:** the assignment's data model (queues, retries, DLQ, execution
history, scheduling) is inherently relational and needs to be queried and
joined constantly by the dashboard. Introducing a broker means keeping two
systems consistent — the broker's queue state and the DB's job records —
which is exactly the kind of dual-write problem that causes subtle bugs
(a job acked in the broker but not marked complete in the DB, or vice
versa). `SKIP LOCKED` gives genuine at-most-once claiming semantics
(two workers physically cannot lock the same row) with one system of
record and one transaction.

**Trade-off accepted:** polling instead of push-based delivery, and claim
throughput is bounded by Postgres, not a purpose-built broker. At the
scale this assignment targets (a handful of workers, sub-second poll
interval) that ceiling is far away. **Documented upgrade path:** if claim
volume ever became the bottleneck, the claim endpoint's contract
(`POST /claim` → array of jobs) doesn't have to change — the
implementation behind it could move to `LISTEN/NOTIFY` for push-based
wakeups, or the whole claim path could be swapped for a broker while
Postgres remains the durable system of record for everything else.

## 2. Workers as HTTP clients of the API, not direct DB clients

**Decision:** `scripts/run_worker.py` never imports SQLAlchemy or touches
Postgres — it only calls `register`, `claim`, `start`, `heartbeat`,
`result` over HTTP.

**Why:** this keeps the atomicity guarantee, retry-policy resolution, and
authorization in exactly one place. It also means a worker can be written
in any language (the protocol is just JSON over HTTP) and deployed
anywhere with network access to the API and an auth token — no VPC-level
database access required, which matters for security posture in a real
deployment.

**Trade-off accepted:** one extra network hop per lifecycle transition
versus a worker that queries Postgres directly. Given jobs are expected to
run for at least tens of milliseconds (usually far more), a few
single-digit-millisecond HTTP calls are not the bottleneck.

## 3. Two ways to run the scheduler (embedded vs. standalone)

**Decision:** the scheduler tick (`app/services/scheduler_service.py`) is
one function, invoked both by an `asyncio` task inside the API process
*and* by `scripts/run_scheduler.py` as an independent process.

**Why:** `docker compose up` with nothing else configured should still
promote delayed/cron jobs correctly (embedded mode). A real deployment
wants scheduling to scale, restart, and be observed independently of API
request-handling (standalone mode). Rather than choosing one and
documenting a migration, both are supported from day one because every
tick operation is a `WHERE`-guarded UPDATE and therefore safe to run
concurrently/redundantly.

**Trade-off accepted:** running both simultaneously (as the default
`docker-compose.yml` does, for demo completeness) means the promotion and
cron-evaluation queries run twice as often as strictly necessary. This is
wasted work, not a correctness risk — acceptable for this project's scale,
called out here so it's an informed choice rather than an oversight. In
production, disable the embedded loop and run exactly the standalone
process (or the standalone process behind a leader-election lock if you
want >1 instance for failover without duplicate work).

## 4. In-memory WebSocket manager (single API instance)

**Decision:** `app/websocket_manager.py` keeps connected sockets in a
process-local `set`.

**Why:** simplest thing that works, and correct for the single-API-instance
deployment this assignment ships (`docker-compose.yml` runs one `api`
container). Building a distributed pub/sub layer for a system that isn't
horizontally scaling the API yet would be premature.

**Trade-off accepted:** this does **not** fan out correctly across
multiple API instances — a dashboard connected to instance A won't see an
event broadcast by instance B. **Documented upgrade path:** swap
`WebSocketManager.broadcast` to publish to a Redis channel and have every
instance subscribe and forward to its own local connections; the router
call sites (`await ws_manager.broadcast(...)`) don't need to change.

## 5. Retry strategies and where policy lives

**Decision:** `RetryPolicy` is its own table, referenced by `Queue` (as a
default) and optionally by `Job` (override). Three strategies — fixed,
linear, exponential — implemented as pure functions in
`app/services/retry.py`, unit-tested independent of the database.

**Why:** separating "what delay to use" (pure calculation) from "how a
failed execution updates job state" (`record_result` in
`job_service.py`) means the backoff math can be tested exhaustively with
zero DB setup, while the state-machine logic can be tested against a real
Postgres instance for the parts that actually need it (claiming).

**Trade-off accepted:** exponential backoff has no jitter in this
implementation. In a system with many jobs failing in sync (e.g. a
downstream outage), un-jittered exponential backoff can cause a
thundering-herd retry spike. Deliberately left out to keep the retry
calculation simple and auditable for this assignment; adding
`delay * random(0.5, 1.5)` in `compute_delay_seconds` is a small, isolated
change if needed.

## 6. Dead Letter Queue stores a payload snapshot (denormalization)

**Decision:** `DeadLetterEntry.payload_snapshot` duplicates the job's
payload rather than joining back to `Job.payload`.

**Why:** covered in `docs/ER_DIAGRAM.md` — the DLQ is meant to be a durable
audit/replay record independent of the source job's retention. This is
the one deliberate normalization break in the schema.

**Trade-off accepted:** if a job's payload were mutated after creation
(it isn't, in this design — jobs are immutable once submitted), the
snapshot and the live job could diverge. Since jobs are write-once, this
risk doesn't materialize in practice.

## 7. RBAC scope: organization-level roles, not per-queue ACLs

**Decision:** `OrgMembership.role` (owner/admin/member/viewer) gates
access at the organization level; any member of an org can see/act on
every project and queue inside it. Only `viewer` is restricted (read-only).

**Why:** the assignment's bonus list includes RBAC but the core
requirement is project/queue management under authenticated users — a
full per-resource ACL system (queue-level permissions, custom roles) is
significant scope beyond what "role-based access control" as a bonus
implies, and would meaningfully complicate the schema (a permissions
table, role-to-permission mapping) for a feature not central to the
scheduler itself.

**Trade-off accepted / explicitly out of scope:** no per-queue or
per-project role overrides. **Upgrade path:** `authz.py` already
centralizes every access check into two functions
(`get_project_or_403`, `get_queue_or_403`); adding a `ProjectMembership`
override table would mean extending those two functions, not touching
every route handler.

## 8. What's explicitly out of scope, and why

- **Workflow/DAG dependencies between jobs** — the schema and claim query
  assume jobs are independent units of work. Adding
  "job B waits for job A" would require a dependency table and a
  materially different claim predicate (`WHERE NOT EXISTS unmet
  dependencies`); left out to keep the claim query — the hottest path in
  the system — simple and provably correct rather than partially correct
  across two features.
- **Queue sharding across multiple databases** — not needed at this
  scale; the composite claim index (`docs/ER_DIAGRAM.md`) is the right
  first lever for throughput before reaching for sharding.
- **AI-generated failure summaries** — would sit cleanly on top of
  existing data (`JobExecution.error_message` / `error_stacktrace`) as an
  additive endpoint calling out to an LLM; skipped to keep the reliability
  and correctness work (the 55/100 of the rubric on architecture, DB
  design, and concurrency) as the primary focus of the time spent.
- **Full event-driven execution (e.g. webhook-triggered jobs, pub/sub
  ingestion)** — the REST submission API already covers "create a job";
  an event-bus trigger is a thin producer in front of the same
  `POST /jobs` contract and doesn't change the scheduler's design.
