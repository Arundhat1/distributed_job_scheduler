import asyncio
import json

from fastapi import WebSocket


class WebSocketManager:
    """
    In-memory pub/sub for a single API instance. Every router that
    mutates job/worker state calls broadcast() so any connected
    dashboard tab reflects it immediately without polling. For a
    multi-instance deployment this would be backed by Redis pub/sub
    instead of a process-local set (see docs/DESIGN_DECISIONS.md).
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message, default=str)
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


ws_manager = WebSocketManager()