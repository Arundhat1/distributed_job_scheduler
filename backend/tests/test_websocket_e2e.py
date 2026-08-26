"""
End-to-end test of the actual /api/v1/dashboard/ws route (as opposed to
test_websocket.py, which tests WebSocketManager in isolation without any
HTTP/ASGI layer). This one needs a real DB connection because
_authenticate_ws looks the user up by id, so it runs against the same
scratch Postgres as tests/conftest.py's other fixtures.

Requires the app.main / app.database / app.routers.dashboard module
namespaces to all be pointed at the test database before the app object
is constructed — see the ws_app fixture below for why each of the three
patch targets is needed (each module bound its own local name to the
object at *its own* import time, so patching app.database alone is not
sufficient once app.main and app.routers.dashboard have already imported
from it).
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.security import create_access_token
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ws_app(monkeypatch, db_session):
    test_engine = create_async_engine(TEST_DATABASE_URL)
    test_session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    import app.database as dbmod

    monkeypatch.setattr(dbmod, "engine", test_engine)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", test_session_maker)

    from app.main import app as fastapi_app  # noqa: E402  (import after monkeypatch is intentional)
    import app.main as main_mod
    import app.routers.dashboard as dashboard_mod

    # Each module bound its own name to app.database's objects at its own
    # import time, via `from app.database import AsyncSessionLocal, engine`.
    # Patching app.database's attributes alone does not retroactively change
    # those already-bound local names — patch each site that actually uses
    # them at call time.
    monkeypatch.setattr(main_mod, "engine", test_engine)
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", test_session_maker)
    monkeypatch.setattr(dashboard_mod, "AsyncSessionLocal", test_session_maker)

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[dbmod.get_db] = override_get_db

    yield fastapi_app

    fastapi_app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest_asyncio.fixture
async def registered_user(db_session):
    """A real user row + token, via the same path the API itself uses."""
    from app.models import Organization, OrgMembership, OrgRole, User
    from app.security import hash_password

    user = User(email="ws-test@example.com", hashed_password=hash_password("irrelevant"), full_name="WS Test")
    db_session.add(user)
    await db_session.flush()

    org = Organization(name="WS Test Org")
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrgMembership(user_id=user.id, organization_id=org.id, role=OrgRole.OWNER))
    await db_session.commit()
    await db_session.refresh(user)

    return user, create_access_token(subject=str(user.id))


async def test_websocket_rejects_connection_with_no_token(ws_app):
    from fastapi.testclient import TestClient

    with TestClient(ws_app) as client:
        with pytest.raises(Exception):
            # starlette's TestClient raises when the server closes during handshake;
            # the meaningful assertion is that no message is ever received.
            with client.websocket_connect("/api/v1/dashboard/ws") as ws:
                ws.receive_text()


async def test_websocket_rejects_connection_with_garbage_token(ws_app):
    from fastapi.testclient import TestClient

    with TestClient(ws_app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/v1/dashboard/ws?token=not-a-real-token") as ws:
                ws.receive_text()


async def test_websocket_accepts_connection_with_valid_token(ws_app, registered_user):
    from fastapi.testclient import TestClient

    _, token = registered_user
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/api/v1/dashboard/ws?token={token}") as ws:
            # Connection must be accepted (no exception raised on entry).
            # Send a keep-alive ping; the server doesn't echo, so we just
            # confirm the socket stays open long enough to send without error.
            ws.send_text("ping")


async def test_websocket_replays_backlog_since_given_id(ws_app, registered_user):
    from fastapi.testclient import TestClient

    from app.websocket_manager import ws_manager

    _, token = registered_user
    first = await ws_manager.broadcast("job_started", {"job_id": 1})
    await ws_manager.broadcast("job_finished", {"job_id": 1})

    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/api/v1/dashboard/ws?token={token}&since={first['id']}") as ws:
            replayed = ws.receive_json()
            assert replayed["type"] == "job_finished"
            assert replayed["id"] > first["id"]