"""実データと切り離したDBと固定日時で、変更後の動作を確かめる。"""
from datetime import date
from pathlib import Path
from collections.abc import Iterator
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import clock, db as database
from app.main import create_app


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """操作が同じ秒でも正しく動くか確認できるよう、日時を固定する。"""
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 29))
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-29T14:30:00")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """テストごとに異なるDBファイルを使い、利用者のDBに接続しない。"""
    return tmp_path / "test-todo.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """業務処理を確認するための一時DB接続を返す。"""
    database.init_db(db_path)
    connection = database.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    """Hostの制限も本番と同じ条件で確認する。"""
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as test_client:
        yield test_client
