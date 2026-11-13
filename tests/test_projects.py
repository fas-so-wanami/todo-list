import sqlite3

import pytest

from app import db as database
from app import projects, tasks


def test_init_db_keeps_existing_tasks_unassigned_when_adding_projects(tmp_path):
    db_path = tmp_path / "legacy-todo.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO users (id, name) VALUES (1, '自分');
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            parent_id INTEGER,
            title TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 2,
            urgency INTEGER NOT NULL DEFAULT 2,
            status TEXT NOT NULL DEFAULT 'todo',
            progress INTEGER NOT NULL DEFAULT 0,
            due_date TEXT,
            start_date TEXT,
            memo TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            completed_at TEXT,
            recurrence_rule TEXT,
            waiting_since TEXT,
            waiting_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT INTO tasks (id, user_id, title, created_at)
        VALUES (42, 1, '既存タスク', '2026-10-29T14:30:00');
        """
    )
    connection.commit()
    connection.close()

    database.init_db(db_path)

    connection = database.connect(db_path)
    try:
        task = connection.execute("SELECT id, title, project_id FROM tasks WHERE id = 42").fetchone()
        assert tuple(task) == (42, "既存タスク", None)
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    finally:
        connection.close()


def test_parent_and_children_move_between_projects_together(db):
    first_project = projects.create_project(db, 1, "  先の案件  ")
    second_project = projects.create_project(db, 1, "次の案件")
    parent = tasks.create_task(db, 1, "親", project_id=first_project)
    child = tasks.create_task(db, 1, "子", parent_id=parent)

    assert db.execute("SELECT name FROM projects WHERE id = ?", (first_project,)).fetchone()[0] == "先の案件"
    assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (child,)).fetchone()[0] == first_project
    assert tasks.change_project(db, 1, parent, second_project)

    project_ids = {
        row["project_id"] for row in db.execute(
            "SELECT project_id FROM tasks WHERE user_id = ? AND id IN (?, ?)", (1, parent, child),
        )
    }
    assert project_ids == {second_project}


def test_linked_child_inherits_project_and_keeps_it_when_unlinked(db):
    project_id = projects.create_project(db, 1, "案件")
    parent = tasks.create_task(db, 1, "親", project_id=project_id)
    child = tasks.create_task(db, 1, "既存タスク")

    assert tasks.change_parent(db, 1, child, parent)
    assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (child,)).fetchone()[0] == project_id
    assert tasks.change_parent(db, 1, child, None)
    assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (child,)).fetchone()[0] == project_id


def test_closed_projects_remain_available_for_existing_tasks_only(db):
    project_id = projects.create_project(db, 1, "終了案件")
    task_id = tasks.create_task(db, 1, "既存タスク", project_id=project_id)
    projects.set_project_closed(db, 1, project_id, True)
    other_closed_project = projects.create_project(db, 1, "別の終了案件")
    projects.set_project_closed(db, 1, other_closed_project, True)

    with pytest.raises(tasks.TaskRelationshipError, match="終了"):
        tasks.create_task(db, 1, "新しいタスク", parent_id=task_id)
    with pytest.raises(tasks.TaskRelationshipError, match="終了"):
        tasks.change_project(db, 1, task_id, other_closed_project)

    assert projects.list_projects(db, 1, include_closed=False) == []
    assert projects.list_projects(db, 1)[0]["status"] == "closed"


def test_project_routes_connect_project_forms_and_task_rows(client, db_path):
    page = client.get("/")
    assert 'class="project-management"' in page.text
    assert 'id="quick-add-project-options"' in page.text

    created = client.post("/projects", data={"name": "移行準備"})
    assert "移行準備" in created.text
    assert "hx-swap-oob=\"innerHTML\"" in created.text
    assert '<details class="project-management" open>' in created.text

    connection = database.connect(db_path)
    try:
        project_id = connection.execute("SELECT id FROM projects WHERE name = ?", ("移行準備",)).fetchone()[0]
    finally:
        connection.close()

    response = client.post("/tasks", data={
        "title": "受入テスト", "project_id": str(project_id), "destination": "today",
    })
    assert "案件: 移行準備" in response.text

    connection = database.connect(db_path)
    try:
        parent_id = connection.execute("SELECT id FROM tasks WHERE title = ?", ("受入テスト",)).fetchone()[0]
    finally:
        connection.close()

    child_response = client.post(f"/tasks/{parent_id}/subtasks", data={"title": "項目を確認"})
    assert "案件: 移行準備" in child_response.text
    renamed = client.post(f"/projects/{project_id}/rename", data={"name": "受入案件"})
    assert "案件名を変更しました" in renamed.text
    assert "案件: 受入案件" in renamed.text

    closed = client.post(f"/projects/{project_id}/status/closed")
    assert "関連タスクと記録は残っています" in closed.text
    assert "案件: 受入案件" in closed.text
    assert "value=\"移行準備\"" not in closed.text


def test_closed_project_recurring_family_keeps_project_assignment(db):
    project_id = projects.create_project(db, 1, "終了案件")
    parent = tasks.create_task(db, 1, "繰り返す親", project_id=project_id, recurrence_rule="weekly")
    child = tasks.create_task(db, 1, "繰り返す子", parent_id=parent)
    projects.set_project_closed(db, 1, project_id, True)
    tasks.set_status(db, child, "done")

    tasks.set_status(db, parent, "done")

    next_parent = db.execute(
        "SELECT next_task_id FROM task_recurrences WHERE task_id = ?", (parent,),
    ).fetchone()[0]
    next_child = db.execute("SELECT id, project_id FROM tasks WHERE parent_id = ?", (next_parent,)).fetchone()
    assert next_child["project_id"] == project_id