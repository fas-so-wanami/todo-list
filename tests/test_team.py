from contextlib import closing

from fastapi.testclient import TestClient

from app import db as database
from app.main import create_app


def _insert_task(connection, user_id, title, *, status="todo", due_date=None, memo=""):
    progress = {"todo": 0, "doing": 25, "done": 100}[status]
    cursor = connection.execute(
        "INSERT INTO tasks (user_id, title, status, progress, due_date, memo, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, title, status, progress, due_date, memo, "2026-10-29T09:00:00"),
    )
    return cursor.lastrowid


def _insert_time_log(connection, task_id, minutes):
    connection.execute(
        "INSERT INTO time_logs (task_id, work_date, minutes) VALUES (?, ?, ?)",
        (task_id, "2026-10-29", minutes),
    )


def _insert_blocker(connection, task_id, note=""):
    connection.execute(
        "INSERT INTO blockers (task_id, raised_at, note) VALUES (?, ?, ?)",
        (task_id, "2026-10-29T09:00:00", note),
    )


def test_team_dashboard_shows_only_manager_summary_and_filters_types(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.setenv(
        "TODO_ALLOWED_USER_EMAILS",
        "owner@example.com,member@example.com,clear@example.com,other@example.com,never@example.com,manager@example.com",
    )
    monkeypatch.setenv("TODO_MANAGER_EMAILS", "manager@example.com")
    monkeypatch.setenv("TODO_DATA_OWNER_EMAIL", "owner@example.com")
    db_path = tmp_path / "team-dashboard.db"
    member_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "member@example.com"}
    clear_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "clear@example.com"}
    other_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "other@example.com"}
    manager_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "manager@example.com"}

    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        client.get("/", headers=member_headers)
        client.get("/", headers=clear_headers)
        client.get("/", headers=other_headers)

        empty_page = client.get("/team", headers=manager_headers)
        assert empty_page.status_code == 200
        assert "現在、表示対象の例外タスクはありません。" in empty_page.text
        assert "clear</h2>" not in empty_page.text
        assert "never@example.com" not in empty_page.text

        with closing(database.connect(db_path)) as connection:
            owner_id = database.DEFAULT_USER_ID
            clear_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("clear@example.com",),
            ).fetchone()["id"]
            other_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("other@example.com",),
            ).fetchone()["id"]

            combined_id = _insert_task(
                connection, owner_id, "長時間・期限超過・相談中",
                status="doing", due_date="2026-10-28", memo="画面には出さないメモ",
            )
            _insert_time_log(connection, combined_id, 300)
            _insert_blocker(connection, combined_id)

            completed_long_id = _insert_task(
                connection, owner_id, "完了済み長時間", status="done",
            )
            _insert_time_log(connection, completed_long_id, 300)

            _insert_task(connection, owner_id, "今日が期限", due_date="2026-10-29")
            _insert_task(connection, owner_id, "期限超過だけ", due_date="2026-10-28")

            completed_blocker_id = _insert_task(
                connection, owner_id, "完了後も未解決の相談", status="done",
            )
            _insert_blocker(connection, completed_blocker_id, "確認が必要です")

            blocker_only_id = _insert_task(connection, other_id, "相談だけ")
            _insert_blocker(connection, blocker_only_id, "返答待ちです")
            _insert_task(connection, clear_id, "異常のないタスク", due_date="2026-10-30")
            connection.commit()

        denied = client.get("/team", headers=member_headers)
        assert denied.status_code == 403
        member_page = client.get("/", headers=member_headers)
        assert 'href="/team"' not in member_page.text

        page = client.get("/team", headers=manager_headers)
        assert page.status_code == 200
        assert 'href="/team"' in page.text
        assert "メンバー状況" in page.text
        assert page.text.count("長時間・期限超過・相談中") == 1
        assert "長時間化" in page.text
        assert "期限超過" in page.text
        assert "相談中" in page.text
        assert "理由は未記入" in page.text
        assert "完了後も未解決の相談" in page.text
        assert "確認が必要です" in page.text
        assert "完了済み長時間" not in page.text
        assert "今日が期限" not in page.text
        assert "画面には出さないメモ" not in page.text
        assert "開発メンバーB</h2>" not in page.text
        assert "member@example.com" not in page.text
        assert "異常のないタスク" not in page.text
        assert "タスク未登録" not in page.text
        assert "clear</h2>" not in page.text
        assert "never@example.com" not in page.text
        assert "未ログイン" not in page.text
        assert "manager@example.com" not in page.text

        filtered = client.get(
            "/team?filters=1&type=overdue",
            headers=manager_headers,
        )
        assert filtered.status_code == 200
        assert "長時間・期限超過・相談中" in filtered.text
        assert "期限超過だけ" in filtered.text
        assert "相談だけ" not in filtered.text
        assert "選択中の種類には該当なし" in filtered.text
        assert "other</h2>" in filtered.text
        assert "clear</h2>" not in filtered.text

        multi_filtered = client.get(
            "/team?filters=1&type=long_running&type=blocker",
            headers=manager_headers,
        )
        assert multi_filtered.text.count("長時間・期限超過・相談中") == 1
        assert "完了後も未解決の相談" in multi_filtered.text
        assert "期限超過だけ" not in multi_filtered.text


def test_team_dashboard_rejects_missing_manager_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.setenv("TODO_ALLOWED_USER_EMAILS", "owner@example.com,manager@example.com")
    monkeypatch.setenv("TODO_MANAGER_EMAILS", "manager@example.com")
    monkeypatch.setenv("TODO_DATA_OWNER_EMAIL", "owner@example.com")

    with TestClient(create_app(tmp_path / "team-auth.db"), base_url="http://127.0.0.1") as client:
        assert client.get("/team").status_code == 401
