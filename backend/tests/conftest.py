import base64
import hashlib
import json
import os
import time
import uuid

import pytest

# Configure before the app is imported: settings and the engine are cached on first use.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://resilience:resilience@localhost:5432/resilience_test",
)
os.environ["PUBLIC_API_BASE"] = "https://api.example.test"
os.environ["CORS_ORIGINS"] = "https://app.example.test"
os.environ["APP_ENV"] = "test"
os.environ["PLATFORM_PUBKEY"] = ""  # env beats a developer's .env file
os.environ["NIP05_DEV_BASE_URL"] = ""
ADMIN_SECRET = "ad" * 32

from app.nostr.events import pubkey_of  # noqa: E402

os.environ["ADMIN_PUBKEYS"] = pubkey_of(ADMIN_SECRET)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.nostr.events import sign_event  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
SECRET = "7f" * 32  # a test-only key
OTHER_SECRET = "3a" * 32
# DELETE rather than TRUNCATE: TRUNCATE takes an exclusive lock and rewrites files, which made
# every test wait about two seconds.
CLEAN_DIRECTORY = (
    "DELETE FROM disbursement_transitions; DELETE FROM disbursements; "
    "DELETE FROM counsellor_attestations; DELETE FROM counsellor_profiles; "
    "DELETE FROM roster_events; DELETE FROM organizations"
)


def alembic_config() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "migrations"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def database():
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture
def client():
    # Signed test events can repeat inside one second, so each test starts with a clean
    # replay table.
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM seen_auth_events"))
        conn.execute(text(CLEAN_DIRECTORY))
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


def auth_header(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    secret: str = SECRET,
    created_at: int | None = None,
    kind: int = 27235,
    payload: str | None = None,
) -> dict:
    # NIP-98 reads are reusable, but writes are single-use. Give each test request a unique
    # harmless tag so identical write fixtures do not replay one another within the same second.
    tags = [["u", url], ["method", method], ["nonce", uuid.uuid4().hex]]
    if payload is not None:
        tags.append(["payload", payload])
    elif body is not None:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(secret, kind, tags, "", created_at=created_at or int(time.time()))
    token = base64.b64encode(json.dumps(event).encode()).decode()
    return {"Authorization": f"Nostr {token}"}
