import sqlite3
import json

import pytest
from fastapi.testclient import TestClient

from app import auth, db as database, launcher
from app.main import create_app


def test_existing_users_get_email_column_without_changing_task_owner(tmp_path):
    db_path = tmp_path / "legacy-users.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            INSERT INTO users (id, name) VALUES (1, '自分');
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                parent_id INTEGER, title TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 2,
                urgency INTEGER NOT NULL DEFAULT 2, status TEXT NOT NULL DEFAULT 'todo',
                progress INTEGER NOT NULL DEFAULT 0, due_date TEXT, start_date TEXT,
                memo TEXT NOT NULL DEFAULT '', started_at TEXT, completed_at TEXT,
                recurrence_rule TEXT, waiting_since TEXT, waiting_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            INSERT INTO tasks (id, user_id, title, created_at)
            VALUES (7, 1, '今までのタスク', '2026-10-29T14:30:00');
            """
        )

    database.init_db(db_path)

    connection = database.connect(db_path)
    try:
        assert connection.execute("SELECT email FROM users WHERE id = 1").fetchone()[0] is None
        assert connection.execute("SELECT user_id FROM tasks WHERE id = 7").fetchone()[0] == 1
        database.bind_default_user_email(connection, " Owner@Example.com ")
        assert connection.execute("SELECT email FROM users WHERE id = 1").fetchone()[0] == "owner@example.com"
        assert connection.execute("SELECT user_id FROM tasks WHERE id = 7").fetchone()[0] == 1
    finally:
        connection.close()


def test_existing_data_owner_email_cannot_be_silently_changed(db):
    database.bind_default_user_email(db, "owner@example.com")

    with pytest.raises(ValueError, match="別の所有者"):
        database.bind_default_user_email(db, "someone-else@example.com")

    assert db.execute("SELECT email FROM users WHERE id = 1").fetchone()[0] == "owner@example.com"


def test_entra_mode_requires_allowed_users_and_explicit_data_owner(monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.delenv("TODO_ALLOWED_USER_EMAILS", raising=False)
    monkeypatch.delenv("TODO_DATA_OWNER_EMAIL", raising=False)

    with pytest.raises(auth.AuthConfigurationError, match="TODO_ALLOWED_USER_EMAILS"):
        auth.load_settings()


def test_entra_mode_binds_existing_owner_and_registers_only_allowlisted_users(db, monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.setenv("TODO_ALLOWED_USER_EMAILS", "owner@example.com,member@example.com")
    monkeypatch.setenv("TODO_DATA_OWNER_EMAIL", "OWNER@EXAMPLE.COM")
    monkeypatch.setenv("TODO_MANAGER_EMAILS", "owner@example.com")
    settings = auth.load_settings()

    auth.initialize_users(db, "temporary.db", settings)
    owner = auth.resolve_enterprise_user(db, "owner@example.com", settings)
    member = auth.resolve_enterprise_user(db, " MEMBER@example.com ", settings)
    rejected = auth.resolve_enterprise_user(db, "outsider@example.com", settings)

    assert owner["id"] == database.DEFAULT_USER_ID
    assert owner["email"] == "owner@example.com"
    assert member["email"] == "member@example.com"
    assert rejected is None
    assert settings.manager_emails == {"owner@example.com"}


def test_development_users_are_created_only_for_nonproduction_database(tmp_path, db):
    settings = auth.AuthSettings(
        mode="dev", allowed_emails=frozenset(), manager_emails=frozenset(),
        data_owner_email=None, cookie_secret=b"test-secret",
    )

    with pytest.raises(auth.AuthConfigurationError, match="TODO_DATA_OWNER_EMAIL"):
        auth.initialize_users(db, database.DEFAULT_DB_PATH, settings)

    auth.initialize_users(db, tmp_path / "temporary.db", settings)
    users = auth.get_development_users(db, settings)

    assert [(user["name"], user["email"]) for user in users] == [
        ("開発メンバーA", "member-a@example.test"),
        ("開発メンバーB", "member-b@example.test"),
        ("開発上司", "manager@example.test"),
    ]


def test_development_mode_can_bind_configured_owner_on_default_database(db):
    settings = auth.AuthSettings(
        mode="dev", allowed_emails=frozenset(), manager_emails=frozenset(),
        data_owner_email="so.wanami@furukawaelectric.com", cookie_secret=b"test-secret",
    )

    auth.initialize_users(db, database.DEFAULT_DB_PATH, settings)

    owner = db.execute("SELECT id, name, email FROM users WHERE id = 1").fetchone()
    assert tuple(owner) == (1, "ユーザーA", "so.wanami@furukawaelectric.com")
    assert [user["email"] for user in auth.get_development_users(db, settings)] == [
        "so.wanami@furukawaelectric.com", "member-b@example.test", "manager@example.test",
    ]


def test_development_user_cookie_rejects_tampering():
    signed_cookie = auth.sign_development_user(2, b"test-secret")

    assert auth.verify_development_user(signed_cookie, b"test-secret") == 2
    assert auth.verify_development_user("2.invalid", b"test-secret") is None
    assert auth.verify_development_user("not-an-id", b"test-secret") is None


def test_development_login_switches_between_isolated_task_and_report_screens(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("TODO_AUTH_MODE", "dev")
    db_path = tmp_path / "development-users.db"
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        unauthenticated = client.get("/", follow_redirects=False)
        login_page = client.get("/login")
        version_response = client.get("/api/data-version")
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/login"
        assert version_response.status_code == 200
        assert isinstance(version_response.json()["version"], int)
        assert "開発ユーザー選択" in login_page.text
        assert "開発メンバーA" in login_page.text
        assert "開発メンバーB" in login_page.text
        assert "開発上司" in login_page.text
        assert "password" not in login_page.text.casefold()

        connection = database.connect(db_path)
        try:
            member_a_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("member-a@example.test",),
            ).fetchone()[0]
            member_b_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("member-b@example.test",),
            ).fetchone()[0]
        finally:
            connection.close()

        client.post("/dev-login", data={"user_id": str(member_a_id)}, follow_redirects=False)
        member_a_page = client.get("/")
        assert "開発: 開発メンバーA" in member_a_page.text
        assert "ユーザー切替" in member_a_page.text
        client.post("/tasks", data={"title": "Aだけのタスク"})
        connection = database.connect(db_path)
        try:
            task_id = connection.execute(
                "SELECT id FROM tasks WHERE user_id = ? AND title = ?", (member_a_id, "Aだけのタスク"),
            ).fetchone()[0]
        finally:
            connection.close()
        client.post(f"/tasks/{task_id}/report")
        assert "Aだけのタスク" in client.get("/report").text

        client.post("/dev-login", data={"user_id": str(member_b_id)}, follow_redirects=False)
        member_b_page = client.get("/")
        assert "開発: 開発メンバーB" in member_b_page.text
        assert "Aだけのタスク" not in member_b_page.text
        assert "Aだけのタスク" not in client.get("/report").text
        client.post("/tasks", data={"title": "Bだけのタスク"})
        assert "Bだけのタスク" in client.get("/").text
        client.post(f"/tasks/{task_id}/status/done")
        connection = database.connect(db_path)
        try:
            assert connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "todo"
        finally:
            connection.close()

        client.post("/dev-login", data={"user_id": "999999"}, follow_redirects=False)
        assert client.get("/", follow_redirects=False).status_code == 303


def test_entra_login_requires_header_and_rejects_non_allowlisted_email(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.setenv("TODO_ALLOWED_USER_EMAILS", "owner@example.com,member@example.com")
    monkeypatch.setenv("TODO_MANAGER_EMAILS", "owner@example.com")
    monkeypatch.setenv("TODO_DATA_OWNER_EMAIL", "owner@example.com")
    db_path = tmp_path / "entra-users.db"

    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 401
        assert client.get("/", headers={
            "X-MS-CLIENT-PRINCIPAL-NAME": "outsider@example.com",
        }).status_code == 403
        owner_page = client.get("/", headers={
            "X-MS-CLIENT-PRINCIPAL-NAME": "owner@example.com",
        })
        member_page = client.get("/", headers={
            "X-MS-CLIENT-PRINCIPAL-NAME": "MEMBER@example.com",
        })
        assert owner_page.status_code == 200
        assert member_page.status_code == 200
        owner_connection = database.connect(db_path)
        try:
            assert owner_connection.execute(
                "SELECT id FROM users WHERE email = ?", ("owner@example.com",),
            ).fetchone()[0] == database.DEFAULT_USER_ID
            assert owner_connection.execute(
                "SELECT COUNT(*) FROM users WHERE email = ?", ("member@example.com",),
            ).fetchone()[0] == 1
        finally:
            owner_connection.close()


def test_launcher_reads_local_auth_settings_without_overriding_environment(tmp_path, monkeypatch):
    settings_path = tmp_path / "local_auth.json"
    settings_path.write_text(json.dumps({
        "TODO_AUTH_MODE": "dev",
        "TODO_DATA_OWNER_EMAIL": "owner@example.com",
    }), encoding="utf-8")
    monkeypatch.setattr(launcher, "LOCAL_AUTH_SETTINGS_PATH", settings_path)
    monkeypatch.delenv("TODO_AUTH_MODE", raising=False)
    monkeypatch.delenv("TODO_DATA_OWNER_EMAIL", raising=False)

    launcher.load_local_auth_settings()

    assert __import__("os").environ["TODO_AUTH_MODE"] == "dev"
    assert __import__("os").environ["TODO_DATA_OWNER_EMAIL"] == "owner@example.com"

    monkeypatch.setenv("TODO_AUTH_MODE", "local")
    launcher.load_local_auth_settings()
    assert __import__("os").environ["TODO_AUTH_MODE"] == "local"


def test_launcher_rejects_invalid_local_auth_settings(tmp_path, monkeypatch):
    settings_path = tmp_path / "local_auth.json"
    settings_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(launcher, "LOCAL_AUTH_SETTINGS_PATH", settings_path)

    with pytest.raises(ValueError, match="項目一覧"):
        launcher.load_local_auth_settings()