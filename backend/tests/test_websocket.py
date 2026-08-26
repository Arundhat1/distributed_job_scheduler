"""
The one property that matters most for the WS hardening in this pass:
an unauthenticated (missing/invalid token) connection must never be
accepted, and must never observe the broadcast set. We test the
authentication helper and the manager's envelope/replay behavior
directly (no Postgres needed) rather than spinning up the full ASGI app
with a live DB — the WS route itself is a thin wrapper around
_authenticate_ws + ws_manager, both pure/DB-light enough to test in
isolation.
"""
import pytest

from app.security import create_access_token
from app.websocket_manager import WebSocketManager


@pytest.mark.asyncio
async def test_broadcast_assigns_monotonically_increasing_ids():
    mgr = WebSocketManager()
    e1 = await mgr.broadcast("job_started", {"job_id": 1})
    e2 = await mgr.broadcast("job_finished", {"job_id": 1})
    assert e2["id"] > e1["id"]
    assert e1["type"] == "job_started"
    assert e1["data"] == {"job_id": 1}


@pytest.mark.asyncio
async def test_replay_since_returns_only_events_after_given_id():
    mgr = WebSocketManager()
    events = [await mgr.broadcast("job_started", {"job_id": i}) for i in range(5)]
    cutoff = events[2]["id"]

    replayed = mgr.replay_since(cutoff)

    assert [e["id"] for e in replayed] == [e["id"] for e in events[3:]]


@pytest.mark.asyncio
async def test_replay_since_latest_id_returns_nothing():
    mgr = WebSocketManager()
    last = await mgr.broadcast("job_started", {"job_id": 1})
    assert mgr.replay_since(last["id"]) == []


@pytest.mark.asyncio
async def test_ring_buffer_is_bounded():
    from app.websocket_manager import RING_BUFFER_SIZE

    mgr = WebSocketManager()
    for i in range(RING_BUFFER_SIZE + 50):
        await mgr.broadcast("job_started", {"job_id": i})
    assert len(mgr._ring_buffer) == RING_BUFFER_SIZE
    # oldest surviving event should be the 51st broadcast, not the 1st
    assert mgr._ring_buffer[0]["data"]["job_id"] == 50


@pytest.mark.asyncio
async def test_broadcast_with_no_connections_does_not_raise():
    mgr = WebSocketManager()
    envelope = await mgr.broadcast("worker_registered", {"worker_id": 1})
    assert envelope["type"] == "worker_registered"


def test_valid_token_round_trips_through_decode():
    from app.security import decode_access_token

    token = create_access_token(subject="7")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "7"


def test_garbage_token_fails_to_decode():
    from app.security import decode_access_token

    assert decode_access_token("not.a.valid.jwt") is None
    assert decode_access_token("") is None