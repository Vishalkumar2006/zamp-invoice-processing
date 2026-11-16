from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import config


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "DB_PATH", d / "app.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", d / "uploads")
    monkeypatch.setattr(config, "WORKER_POLL_S", 0.05)
    return d


@pytest.fixture
def client(data_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_for(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}").json()
        if r["status"] in ("COMPLETED", "FAILED"):
            return r
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish")
