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

*Superseded in part by decision #9 below*, which hardens this same
component with authentication and a replay buffer — the single-instance
trade-off described here is unchanged by that work and remains the
relevant limitation to know about before scaling the API horizontally.

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

## 9. WebSocket authentication and event replay (hardening pass)

### Decision
Require the same JWT every REST endpoint uses on the WebSocket handshake
(`?token=...` query param, since browser `WebSocket` clients cannot set an
`Authorization` header), validated *before* `accept()` is called. Wrap
every broadcast in a structured envelope (`{id, type, ts, data}`) with a
per-process monotonic id, backed by a bounded 200-event ring buffer that
a reconnecting client can replay from via `?since=<id>`.

### Context
The original implementation accepted any WebSocket connection to
`/api/v1/dashboard/ws` with no authentication at all, while every REST
endpoint in the system required a valid bearer token. That's not a minor
inconsistency — it meant job payloads, worker hostnames, and error
messages were observable by anyone with network access to the API,
authenticated or not. Separately, events were flat, unordered dicts: a
client that disconnected for any reason (phone sleep, wifi blip, tab
backgrounded) had no way to know what it missed and no way to ask for it.

### Alternatives considered
- **Do nothing / accept the gap as "demo-quality."** Rejected — this is
  an actual information-disclosure bug, not a missing nice-to-have, and
  fixing it doesn't require new infrastructure.
- **Session cookie instead of a token in the query string.** Rejected for
  this stack: the API is a stateless bearer-token API by design (see the
  auth section elsewhere in this document); introducing a parallel
  cookie-based auth path for exactly one endpoint would be more surface
  area, not less, for equivalent security.
- **Redis Streams (or a message broker) for durable, replayable event
  history.** This is the *architecturally correct* answer at a larger
  scale, or the moment the API runs more than one instance — a
  process-local ring buffer cannot serve a client whose previous
  connection landed on a different instance, or survive a restart.
  Rejected for *now* because this project runs a single API instance and
  Redis would buy durability the current topology can't take advantage
  of yet. This is the clearest example in this project of "don't
  introduce infrastructure a fashionable label" — the in-memory buffer is
  the right size for the actual deployment, and the upgrade path is
  named explicitly below rather than pretended away.

### Decision rationale
Token-on-query-param is the standard, well-understood pattern for
authenticating a browser WebSocket handshake (it's what the constraint of
"no custom headers on the handshake request" leaves you with), and it
reuses the exact same JWT validation code path as every REST route — no
new auth logic, no new attack surface, no duplicated rules to keep in
sync. The envelope + ring buffer is the smallest change that turns
"events might just get missed" into "events might get missed only in
specific, named, documented circumstances" (buffer wraparound, process
restart) — which is a materially different and more honest guarantee.

### Consequences
**Benefits:** the info-disclosure gap is closed; reconnecting clients
recover from short blips (Wi-Fi drop, tab backgrounded) without a gap in
the dashboard's live feed; every event has a stable identity useful for
debugging ("which event was event #4821").
**Costs:** one extra round trip conceptually (token must be minted before
the socket opens, which it always was for REST anyway); the ring buffer
is 200 events of extra memory per process (negligible); token expiry
mid-connection is not currently handled by force-closing the socket — an
expired token only blocks *new* connections, not ones already open. That
last point is a known, accepted gap, not an oversight — see "Future
evolution."

### Failure model
- **Client provides no token / an expired or tampered token:** connection
  is never accepted; closed with code 1008 (policy violation) before
  joining the broadcast set. No partial/momentary exposure.
- **Client disconnects and reconnects within the ring buffer's window
  (last 200 events):** full recovery via `?since=<id>`.
- **Client reconnects after the buffer has wrapped, or after an API
  restart:** silently loses events older than the buffer/restart. The
  client's periodic REST poll of `/dashboard/summary` (already present in
  `Dashboard.jsx` as a 10s backstop) is what actually bounds staleness in
  this case — the WS feed is explicitly a latency optimization on top of
  that poll, not a replacement for it.
- **API process crashes:** the entire ring buffer and connection set are
  lost; every connected client falls back to reconnect-with-backoff logic
  already in `useLiveEvents.js`, and picks up wherever the REST poll
  backstop leaves off.

### Consistency model
At-most-once, best-effort delivery, single-process. Not exactly-once —
nothing here deduplicates if a client somehow processes the same envelope
twice — and not durable across restarts. This is stated plainly in the
`websocket_manager.py` module docstring specifically so a future reader
doesn't infer stronger guarantees than exist.

### Concurrency model
`WebSocketManager` protects its connection set with an `asyncio.Lock`
around mutation (`connect`/`disconnect`/pruning dead sockets during
`broadcast`), so concurrent broadcasts from different request handlers
can't corrupt the connection set. The ring buffer (`collections.deque`
with `maxlen`) is only ever mutated from within `broadcast`, which is
always awaited sequentially per call site — no separate locking needed
there.

### Scalability implications
Fine as-is for one API instance and a dashboard-sized number of
concurrent viewers (tens, not thousands). Does **not** fan out across
multiple API instances — a client connected to instance A never sees an
event broadcast by instance B. This was already true before this pass
and remains an explicit, named limitation (see architecture doc). At
larger scale, the fix is Redis pub/sub or Streams behind the same
`broadcast()`/`replay_since()` call sites — the router call sites and the
frontend hook do not need to change.

### Security implications
Closes the unauthenticated-observer gap described above. Remaining gap:
a token that expires *after* a socket is already open is not currently
force-disconnected — the connection stays live until the client closes
it or the process restarts. For a system where job payloads could carry
sensitive data, the next hardening step would be a periodic
token-freshness check on the server side that closes stale-token
connections proactively.

### Operational implications
No new services, no new environment variables, no Docker Compose changes
— this is a code-only change within the existing `api` container.
Debugging a "client isn't getting updates" report now has a starting
point: check the event `id` the client last saw against server logs (the
WS route logs `connected`/`disconnected` with `user_id`), and check
whether the gap exceeds the 200-event buffer window.

### Future evolution
1. Force-close sockets on token expiry rather than only checking at
   connect time.
2. Redis pub/sub (multi-instance fan-out) or Redis Streams (durable
   replay across restarts) once the API runs more than one instance —
   the point at which the in-memory approach's stated limitations
   actually start to bite.
3. Per-organization/per-project event scoping (today every authenticated
   user sees every event system-wide) if this is ever used by more than
   one tenant's worth of users concurrently.

---

## 10. Rate limiting: fixing a real bug + a fairness-aware key function

### Decision
Add `SlowAPIMiddleware` so `Limiter(default_limits=["300/minute"])`
actually applies to every route, not only routes with an explicit
`@limiter.limit(...)` decorator. Layer tighter, endpoint-specific limits
on the highest-risk routes (`/auth/login`, `/auth/register` at
10/minute; `/workers/claim` at 120/minute; job submission at 60/minute).
Key rate-limit buckets by authenticated subject (`user:<id>`) when a
valid bearer token is present, falling back to remote IP only for
pre-auth endpoints.

### Context
The Limiter object was constructed with `default_limits=["300/minute"]`
and wired into the app's exception handler, which *looks* complete —
`app.state.limiter = limiter` plus the exception handler is the part of
slowapi's setup most examples show. It is not sufficient on its own:
slowapi only auto-applies `default_limits` to undecorated routes through
`SlowAPIMiddleware`. Without that middleware, `default_limits` is
inert — a genuinely misleading state to ship, since the configuration
reads as "everything is protected" while only explicitly decorated
routes were. This was caught during a self-review pass, not by an
external report, and is worth naming as exactly the kind of gap that
"the code runs, therefore it works" testing misses — the app started
fine and every route returned 200s; the failure only shows up if you
specifically try to exceed a limit on an undecorated route.

### Alternatives considered
- **Rely on default_limits alone, skip per-route tuning.** Rejected: a
  flat 300/min is too loose for `/auth/login` (meaningful brute-force
  budget) and arguably too tight for `/workers/claim` under many
  concurrent workers polling every second — one flat number can't serve
  both a public write endpoint and an expected-high-frequency internal
  polling endpoint well.
- **IP-only keying (the slowapi default, `get_remote_address`).**
  Rejected as the sole key: this project's own `docker-compose.yml` runs
  two worker containers; if they happen to share an egress IP (same
  Docker network, same host, same NAT in front of a real deployment),
  IP-keying would let one worker's poll traffic exhaust the budget for
  another worker's legitimate `/workers/claim` calls. That's a fairness
  bug, not just an abuse-prevention gap.
- **Redis-backed distributed limiter storage.** The correct answer once
  the API runs more than one instance (in-memory counters are
  per-process, so N instances silently multiply the effective limit).
  Deferred for the same reason as the WebSocket ring buffer — one API
  instance today, no benefit yet from paying for Redis.

### Decision rationale
Keying by authenticated subject when available is a small, local change
(one function) that meaningfully improves fairness without adding
infrastructure — it directly targets a fairness failure mode this
project's own deployment topology (multiple workers) would otherwise hit.
Tighter limits on `/auth/*` target the specific attack these numbers are
meant to blunt (credential stuffing, signup spam) rather than applying
one number uniformly regardless of endpoint risk.

### Consequences
**Benefits:** the previously-inert default now genuinely protects every
route; auth endpoints get materially tighter protection than general
traffic; multiple workers/users sharing infrastructure don't throttle
each other.
**Costs:** slightly more code (a custom key function, decoding a JWT on
every rate-limit check) versus the zero-effort default `get_remote_address`.
The JWT decode is cheap (local HMAC verification, no DB round trip) so
this isn't a meaningful performance cost.

### Failure model
- **Legitimate burst traffic from one authenticated user** (e.g. a
  dashboard tab plus a worker process sharing one user's token) shares
  one budget and can be throttled together. Acceptable today; if it
  becomes a real problem, workers should authenticate with their own
  service-account-style tokens rather than a human user's token, which
  the schema already supports (nothing ties a token's subject to "must be
  a human").
- **Rate limiter's in-memory storage resets on process restart** — an
  attacker who times a request around a deploy gets a fresh budget. Low
  severity for this project's scale; the Redis-backed alternative (noted
  above) would close this too, at the cost of an external dependency.

### Consistency model
Approximate, not exact: slowapi's fixed-window counting can allow a burst
of up to ~2x the configured limit at window boundaries (e.g., a client
could get `limit` requests in the last second of one window and `limit`
more in the first second of the next). This is the standard trade-off of
fixed-window rate limiting and is acceptable for abuse-prevention/fairness
purposes here; a sliding-window or token-bucket algorithm would tighten
this at the cost of more state per key, and isn't justified at this
project's traffic scale.

### Concurrency model
Each request's limit check-and-increment is handled by slowapi's storage
backend, which for the in-memory default is safe under FastAPI's
single-process async concurrency (no separate locking needed on our side).
This is another place the in-memory-vs-Redis trade-off resurfaces: a
Redis backend would additionally make the check atomic across processes,
which in-memory cannot.

### Scalability implications
As worker count or dashboard-user count grows within a single API
instance, the subject-keyed limiter scales fine — it's a hash map lookup.
As API *instance* count grows, the limiter's real behavior silently
diverges from its configured behavior (see Context / Alternatives above)
until backed by Redis.

### Security implications
Directly mitigates credential stuffing on `/auth/login` and signup spam
on `/auth/register`. Does not mitigate a distributed attack across many
IPs each making requests under the per-IP threshold — no single-node rate
limiter can, regardless of backend; that class of attack needs a
WAF/CDN-level control in front of the API, out of scope for this project.

### Operational implications
No new services, no new environment variables. A `429` response is
immediately diagnosable from the response body (slowapi includes the
matched limit string, e.g. "10 per 1 minute", in the error text) and from
the existing request-logging middleware, which logs every request's
status code including 429s.

### Future evolution
1. Redis-backed storage (`storage_uri="redis://..."`) the moment the API
   scales beyond one instance — no call-site changes needed elsewhere.
2. Separate service-account token type for workers, so worker polling
   traffic and human-user traffic never share a rate-limit budget even
   when a human happens to be running a worker locally under their own
   token.
3. A sliding-window or token-bucket algorithm if the fixed-window
   boundary-burst behavior ever becomes a practical problem at this
   project's actual traffic levels (it hasn't yet, by design of the
   current scale).
