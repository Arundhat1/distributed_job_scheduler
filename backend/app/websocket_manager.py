"""
In-memory pub/sub for a single API instance's dashboard WebSocket feed.

Consistency model (be honest about what this is, not what it sounds like):
  - At-most-once, best-effort delivery. If a client is disconnected when an
    event fires, it is not queued for that client specifically — it only
    survives in the shared ring buffer below, which the client can replay
    from on reconnect via ?since=<event_id>.
  - The ring buffer is bounded (RING_BUFFER_SIZE) and process-local: it is
    lost on restart and is not shared across API instances. This is a
    deliberate trade-off, not an oversight — see docs/DESIGN_DECISIONS.md
    for why Redis Streams were considered and deferred.
  - The REST endpoint /api/v1/dashboard/summary remains the source of
    truth. The WebSocket feed is a low-latency convenience layer on top of
    it, not a replacement — the frontend still polls summary periodically
    as a backstop (see frontend/src/pages/Dashboard.jsx).

Every event is wrapped in an envelope so clients can detect gaps and order
events deterministically even if multiple events arrive in the same tick:

    {"id": 42, "type": "job_finished", "ts": "2026-08-26T10:00:00Z", "data": {...}}

`id` is a per-process monotonically increasing counter — NOT a database
identity, and NOT globally unique across API instances. It only needs to
be locally ordered for the replay-since-N mechanism to work.
"""
import asyncio
import itertools
import json
from collections import deque
from datetime import datetime, timezone

from fastapi import WebSocket

RING_BUFFER_SIZE = 200


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._id_counter = itertools.count(1)
        self._ring_buffer: deque[dict] = deque(maxlen=RING_BUFFER_SIZE)

    async def connect(self, ws: WebSocket) -> None:
        """Caller is responsible for authenticating before invoking this."""
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    def replay_since(self, since_id: int) -> list[dict]:
        """Best-effort backlog for a reconnecting client. May be incomplete
        (buffer wraps, or process restarted since the client's last id) —
        callers must treat this as a convenience, not a guarantee."""
        return [e for e in self._ring_buffer if e["id"] > since_id]

    async def broadcast(self, event_type: str, data: dict) -> dict:
        envelope = {
            "id": next(self._id_counter),
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        self._ring_buffer.append(envelope)

        payload = json.dumps(envelope, default=str)
        dead = []
        async with self._lock:
            connections = list(self._connections)
        for ws in connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)
        return envelope


ws_manager = WebSocketManager()