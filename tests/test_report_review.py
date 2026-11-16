from contextlib import closing
import sqlite3
from datetime import timedelta

from fastapi.testclient import TestClient

from app import db as database, reports
from app.main import create_app


def _insert_task(connection, user_id, title, *, parent_id=None):
    """提出版テスト用のタスクを作り、作業時間を関連付けられるIDを返す。"""
    cursor = connection.execute(
        "INSERT INTO tasks (user_id, parent_id, title, status, progress, created_at) "
        "VALUES (?, ?, ?, 'todo', 0, ?)",
        (user_id, parent_id, title, "2026-10-29T09:00:00"),
    )
    return cursor.lastrowid


def _member_section(page_text, member_name):
    """表示順に依存せず、指定メンバーの日報欄だけをテストで確認する。"""
    sections = page_text.split('<details class="report-review-member">')[1:]
    marker = f'<span class="report-review-name">{member_name}</span>'
    return next(section.split("</details>", 1)[0] for section in sections if marker in section)


def test_report_review_shows_submitted_snapshots_only_for_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_AUTH_MODE", "entra")
    monkeypatch.setenv(
        "TODO_ALLOWED_USER_EMAILS",
        "owner@example.com,other@example.com,pending@example.com,never@example.com,manager@example.com",
    )
    monkeypatch.setenv("TODO_MANAGER_EMAILS", "manager@example.com")
    monkeypatch.setenv("TODO_DATA_OWNER_EMAIL", "owner@example.com")
    db_path = tmp_path / "report-review.db"
    report_day = reports.clock.today()
    report_date = report_day.isoformat()
    owner_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@example.com"}
    other_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "other@example.com"}
    pending_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "pending@example.com"}
    manager_headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "manager@example.com"}

    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        client.get("/", headers=owner_headers)
        client.get("/", headers=other_headers)
        client.get("/", headers=pending_headers)

        with closing(database.connect(db_path)) as connection:
            owner_id = database.DEFAULT_USER_ID
            other_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("other@example.com",),
            ).fetchone()["id"]
            pending_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("pending@example.com",),
            ).fetchone()["id"]

            parent_id = _insert_task(connection, owner_id, "提出時の親")
            owner_task_id = _insert_task(connection, owner_id, "提出時の作業", parent_id=parent_id)
            reports.toggle_item(connection, owner_id, owner_task_id, report_day)
            reports.save_draft(
                connection, owner_id, report_day, {owner_task_id: 75},
                work_style="telework", leave_types=["早退"], comment="提出版の備考",
            )
            connection.execute(
                "UPDATE daily_reports SET details_enabled = 0 WHERE user_id = ? AND report_date = ?",
                (owner_id, report_date),
            )
            reports.submit(connection, owner_id, report_day)
            reports.save_draft(
                connection, owner_id, report_day, {owner_task_id: 130},
                work_style=None, leave_types=None, comment="編集中の秘密の備考",
            )
            connection.execute(
                "UPDATE tasks SET title = ? WHERE id = ?",
                ("編集中の作業名", owner_task_id),
            )

            other_task_id = _insert_task(connection, other_id, "作業時間OFFの作業")
            reports.toggle_item(connection, other_id, other_task_id, report_day)
            reports.save_draft(
                connection, other_id, report_day, {other_task_id: 180},
                work_style="telework", leave_types=["午前休"], comment="時間OFFでも提出した備考",
            )
            connection.execute(
                "UPDATE daily_reports SET time_tracking_enabled = 0 WHERE user_id = ? AND report_date = ?",
                (other_id, report_date),
            )
            reports.submit(connection, other_id, report_day)

            reports.save_draft(
                connection, pending_id, report_day, {},
                work_style=None, leave_types=None, comment="未提出下書きの秘密",
            )
            connection.commit()

        manager_page = client.get(
            f"/report-review?date={report_date}",
            headers=manager_headers,
        )
        assert manager_page.status_code == 200
        assert 'href="/report-review"' in manager_page.text
        previous_page = client.get(
            f"/report-review?date={(report_day - timedelta(days=1)).isoformat()}",
            headers=manager_headers,
        )
        assert f'href="/report-review?date={report_date}"' in previous_page.text
        assert manager_page.text.index("未提出") < manager_page.text.index("修正あり・再提出待ち")
        assert manager_page.text.index("修正あり・再提出待ち") < manager_page.text.index("提出済み")
        assert "never@example.com" in manager_page.text
        assert "manager@example.com" not in manager_page.text
        assert "提出時の親" in manager_page.text
        assert "提出時の作業" in manager_page.text
        assert "提出版の備考" in manager_page.text
        assert "編集中の秘密の備考" not in manager_page.text
        assert "編集中の作業名" not in manager_page.text
        assert "未提出下書きの秘密" not in manager_page.text
        assert "時間OFFでも提出した備考" in manager_page.text
        assert "作業時間OFFの作業" in manager_page.text

        owner_section = _member_section(manager_page.text, "自分")
        assert "テレワーク" not in owner_section
        assert "早退" not in owner_section
        assert "1:15" in owner_section
        assert "−6:30" in owner_section

        other_section = _member_section(manager_page.text, "other")
        assert "テレワーク" in other_section
        assert "午前休" in other_section
        assert "3:00" not in other_section
        assert "作業時間合計" not in other_section
        assert "基本勤務時間" not in other_section

        denied = client.get(
            "/report-review",
            headers=owner_headers,
        )
        assert denied.status_code == 403
        owner_page = client.get("/", headers=owner_headers)
        assert 'href="/report-review"' not in owner_page.text


def test_legacy_submission_snapshot_has_unknown_optional_sections(tmp_path):
    db_path = tmp_path / "legacy-report-snapshot.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE daily_report_snapshots ("
            "report_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, report_date TEXT NOT NULL, "
            "work_style TEXT NOT NULL, leave_types TEXT NOT NULL, comment TEXT NOT NULL, "
            "submitted_at TEXT NOT NULL, UNIQUE (user_id, report_date))"
        )
        connection.execute(
            "INSERT INTO daily_report_snapshots "
            "(report_id, user_id, report_date, work_style, leave_types, comment, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (7, 1, "2026-10-28", "office", "", "保存済み", "2026-10-28T17:00:00"),
        )

    database.init_db(db_path)

    with closing(database.connect(db_path)) as connection:
        snapshot = connection.execute(
            "SELECT time_tracking_enabled, details_enabled "
            "FROM daily_report_snapshots WHERE report_id = ?",
            (7,),
        ).fetchone()
        assert tuple(snapshot) == (None, None)
