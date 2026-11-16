import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix='cohort-tests-')
os.environ.setdefault('DATABASE_URL', f'sqlite+pysqlite:///{_TMP}/test.db')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARCHIVE_DIR', f'{_TMP}/archive')

import pytest
from fastapi.testclient import TestClient
from src.config.database import Base, Session, engine
from src.main import app
from src.models.entities import User
from src.utils.security import hash_password


class FakeRedis:
    """No broker in unit tests; accepted pushes are recorded, never consumed."""

    def __init__(self):
        self.pushed = []

    @classmethod
    def from_url(cls, *_args, **_kwargs):
        return cls()

    def rpush(self, queue, value):
        self.pushed.append((queue, value))
        return len(self.pushed)

    def lpush(self, queue, value):
        self.pushed.append((queue, value))
        return len(self.pushed)

    def blpop(self, *_args, **_kwargs):
        return None

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    import redis
    import src.services.archive as archive_service
    archive_service.os.environ['ARCHIVE_DIR'] = str(tmp_path / 'archive')
    archive_service.os.environ['ARTIFACTS_DIR'] = str(tmp_path / 'artifacts')
    monkeypatch.setattr(redis.Redis, 'from_url', FakeRedis.from_url)
    # Archive jobs run inline in tests instead of waiting for the worker.
    monkeypatch.setattr(archive_service, 'enqueue', lambda job_id: False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as s:
        for role in ('admin', 'analyst', 'viewer'):
            s.add(User(email=f'{role}@demo.local', role=role, password=hash_password('Demo123!')))
        s.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(client, role='analyst'):
    r = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}
