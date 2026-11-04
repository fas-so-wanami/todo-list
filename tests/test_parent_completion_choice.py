"""親の完了は行内ボタンを押した場合だけ行われることを確かめる。"""
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db as database, tasks


def seed_pending_family(db_path: Path) -> tuple[int, int, int]:
    """最後の子だけが残る、繰り返し設定のある親子を作る。"""
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "確認してから完了する親", recurrence_rule="weekly")
        first = tasks.create_task(connection, 1, "完了した子", parent_id=parent)
        last = tasks.create_task(connection, 1, "最後の子", parent_id=parent)
        tasks.set_status(connection, first, "done")
        connection.commit()
        return parent, first, last
    finally:
        connection.close()


@pytest.mark.parametrize("view", ["", "mini"])
def test_last_child_shows_button_without_prompt_and_button_completes_parent(
    client: TestClient, db_path: Path, view: str,
) -> None:
    parent, first, last = seed_pending_family(db_path)
    suffix = f"?view={view}" if view else ""
    response = client.post(f"/tasks/{last}/status/done{suffix}")
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "2/2件完了" in response.text
    assert "親も完了にする" in response.text
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
        response = client.post(f"/tasks/{parent}/status/done{suffix}")
        assert tasks.get_task(connection, parent)["status"] == "done"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
        assert "HX-Trigger-After-Settle" not in response.headers
        if view:
            assert 'class="mini-list"' in response.text
    finally:
        connection.close()


def test_completion_button_remains_available_after_reload(client: TestClient, db_path: Path) -> None:
    parent, first, last = seed_pending_family(db_path)
    client.post(f"/tasks/{last}/status/done")
    for url in ("/", "/mini", "/content", "/content?view=mini"):
        response = client.get(url)
        assert "HX-Trigger-After-Settle" not in response.headers
        assert "親も完了にする" in response.text
    response = client.post(f"/tasks/{last}/status/done")
    assert "HX-Trigger-After-Settle" not in response.headers
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    finally:
        connection.close()


def test_parent_cannot_be_completed_if_child_is_reopened(client: TestClient, db_path: Path) -> None:
    parent, first, last = seed_pending_family(db_path)
    client.post(f"/tasks/{last}/status/done")
    client.post(f"/tasks/{first}/status/doing")
    response = client.post(f"/tasks/{parent}/status/done")
    assert "残っているため完了にできません" in response.text
    assert "HX-Trigger-After-Settle" not in response.headers
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    finally:
        connection.close()


def test_reopened_parent_keeps_completion_button_without_prompt(client: TestClient, db_path: Path) -> None:
    parent, first, last = seed_pending_family(db_path)
    client.post(f"/tasks/{last}/status/done")
    client.post(f"/tasks/{parent}/status/done")
    response = client.post(f"/tasks/{parent}/status/doing")
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "親も完了にする" in response.text
    database.init_db(db_path)
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert tasks.get_task(connection, first)["status"] == "done"
        assert tasks.get_task(connection, last)["status"] == "done"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 1
    finally:
        connection.close()


def test_archived_parent_can_be_reopened_without_changing_children(client: TestClient, db_path: Path) -> None:
    parent, first, last = seed_pending_family(db_path)
    client.post(f"/tasks/{last}/status/done")
    client.post(f"/tasks/{parent}/status/done")
    connection = database.connect(db_path)
    try:
        connection.execute(
            "UPDATE tasks SET completed_at = ? WHERE user_id = ? AND id IN (?, ?, ?)",
            ("2026-10-28T15:00:00", 1, parent, first, last),
        )
        connection.commit()
        assert f"/tasks/{parent}/status/doing?view=archive" in client.get("/archive").text
        response = client.post(f"/tasks/{parent}/status/doing?view=archive")
        assert response.headers["HX-Redirect"] == "/"
        assert "HX-Trigger-After-Settle" not in response.headers
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert tasks.get_task(connection, first)["status"] == "done"
        assert tasks.get_task(connection, last)["status"] == "done"
        assert "2/2件完了" in client.get("/").text
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["link", "unlink", "delete"])
def test_relationship_changes_show_button_without_prompt_when_parent_becomes_ready(
    client: TestClient, db_path: Path, operation: str,
) -> None:
    parent, first, last = seed_pending_family(db_path)
    if operation == "link":
        connection = database.connect(db_path)
        try:
            tasks.change_parent(connection, 1, first, None)
            tasks.change_parent(connection, 1, last, None)
            connection.commit()
        finally:
            connection.close()
        response = client.post(f"/tasks/{first}/edit", data={"title": "完了した子", "parent_id": str(parent)})
    elif operation == "unlink":
        response = client.post(f"/tasks/{last}/edit", data={"title": "最後の子", "parent_id": ""})
    else:
        response = client.post(f"/tasks/{last}/delete")
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "親も完了にする" in response.text
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    finally:
        connection.close()


def test_deleting_last_child_does_not_show_button_for_zero_children(client: TestClient, db_path: Path) -> None:
    parent, first, last = seed_pending_family(db_path)
    client.post(f"/tasks/{first}/delete")
    response = client.post(f"/tasks/{last}/delete")
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "0/0件完了" not in response.text


def test_startup_keeps_existing_completed_parent_and_dates(db: sqlite3.Connection, db_path: Path) -> None:
    parent = tasks.create_task(db, 1, "以前から完了した親")
    child = tasks.create_task(db, 1, "以前から完了した子", parent_id=parent)
    db.execute(
        "UPDATE tasks SET status = 'done', progress = 100, completed_at = ? WHERE user_id = ? AND id IN (?, ?)",
        ("2026-10-28T15:00:00", 1, parent, child),
    )
    db.commit()
    database.init_db(db_path)
    assert tasks.get_task(db, parent)["status"] == "done"
    assert tasks.get_task(db, parent)["completed_at"] == "2026-10-28T15:00:00"


def test_candidates_never_include_another_users_parent(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    parent = tasks.create_task(db, 2, "別の利用者の親")
    child = tasks.create_task(db, 2, "別の利用者の子", parent_id=parent)
    tasks.set_status(db, child, "done")
    assert tasks.parents_ready_for_completion(db, 1) == {}
    assert tasks.parents_ready_for_completion(db, 2) == {parent: "別の利用者の親"}
