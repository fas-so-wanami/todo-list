import sqlite3

import pytest

from app import db as database
from app import projects, tasks, wbs


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
    assert "プロジェクトを管理" in page.text
    assert "新しいプロジェクト" in page.text
    assert "プロジェクト未設定" in page.text

    created = client.post("/projects", data={"name": "移行準備"})
    assert "移行準備" in created.text
    assert "プロジェクト「移行準備」を作成しました。" in created.text
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
    assert "プロジェクト: 移行準備" in response.text

    connection = database.connect(db_path)
    try:
        parent_id = connection.execute("SELECT id FROM tasks WHERE title = ?", ("受入テスト",)).fetchone()[0]
    finally:
        connection.close()

    child_response = client.post(f"/tasks/{parent_id}/subtasks", data={"title": "項目を確認"})
    assert "プロジェクト: 移行準備" in child_response.text
    renamed = client.post(f"/projects/{project_id}/rename", data={"name": "受入案件"})
    assert "プロジェクト名を変更しました" in renamed.text
    assert "プロジェクト: 受入案件" in renamed.text

    closed = client.post(f"/projects/{project_id}/status/closed")
    assert "関連タスクと記録は残っています" in closed.text
    assert "プロジェクト: 受入案件" in closed.text
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


def test_shared_project_is_assignable_but_only_creator_or_manager_can_manage(db):
    db.executemany(
        "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
        [(2, "メンバーB", "member-b@example.test"), (3, "上司", "manager@example.test")],
    )
    shared_id = projects.create_project(db, 1, "共通プロジェクト", is_shared=True)

    assert projects.create_project(db, 1, "個人プロジェクト")
    assert shared_id not in [project["id"] for project in projects.list_assignable_projects(db, 2)]
    with pytest.raises(projects.ProjectError, match="許可リスト"):
        projects.add_project_members(
            db, 1, shared_id, ["outsider@example.test"], allowed_emails={"member-b@example.test"},
        )
    projects.add_project_members(
        db, 1, shared_id, ["MEMBER-B@example.test"], allowed_emails={"member-b@example.test"},
    )
    assert shared_id not in [project["id"] for project in projects.list_assignable_projects(db, 2)]
    assert [invite["project_name"] for invite in projects.list_pending_invitations(db, "member-b@example.test")] == [
        "共通プロジェクト",
    ]
    projects.respond_to_invitation(db, "member-b@example.test", shared_id, accept=True)
    assert shared_id in [project["id"] for project in projects.list_assignable_projects(db, 2)]
    assert projects.get_assignable_project(db, 2, shared_id)["is_shared"] == 1
    member_project = next(
        project for project in projects.list_manageable_projects(db, 2) if project["id"] == shared_id
    )
    assert member_project["can_manage"] == 0

    tasks.create_task(db, 2, "メンバーBの作業", project_id=shared_id)
    with pytest.raises(projects.ProjectError, match="変更"):
        projects.rename_project(db, 2, shared_id, "勝手な変更")

    projects.rename_project(db, 3, shared_id, "管理者が変更", is_manager=True)
    assert projects.get_assignable_project(db, 1, shared_id)["name"] == "管理者が変更"


def test_invitation_revoke_and_member_removal_preserve_tasks_but_restrict_wbs(db):
    db.execute(
        "UPDATE users SET email = ? WHERE id = ?", ("owner@example.test", 1),
    )
    db.executemany(
        "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
        [(2, "メンバーB", "member-b@example.test"), (3, "上司", "manager@example.test")],
    )
    shared_id = projects.create_project(db, 1, "解除テスト", is_shared=True)
    projects.add_project_members(
        db, 1, shared_id, ["member-b@example.test"], allowed_emails={"member-b@example.test"},
    )
    projects.revoke_project_invitation(db, 1, shared_id, "member-b@example.test")
    assert projects.list_pending_invitations(db, "member-b@example.test") == []

    projects.add_project_members(
        db, 1, shared_id, ["member-b@example.test"], allowed_emails={"member-b@example.test"},
    )
    projects.respond_to_invitation(db, "member-b@example.test", shared_id, accept=True)
    task_id = tasks.create_task(db, 2, "残すタスク", project_id=shared_id)
    projects.remove_project_member(db, 1, shared_id, "member-b@example.test")

    assert db.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == shared_id
    assert shared_id not in [project["id"] for project in projects.list_wbs_projects(db, 2)]
    assert shared_id not in [project["id"] for project in projects.list_assignable_projects(db, 2)]
    with pytest.raises(tasks.TaskRelationshipError, match="見つかりません"):
        tasks.create_task(db, 2, "新しい割当", project_id=shared_id)
    assert shared_id in [project["id"] for project in projects.list_wbs_projects(db, 3, is_manager=True)]
    assert wbs.build_view(db, 3, project_id=shared_id, is_manager=True)["tasks"] == []

    projects.add_project_members(
        db, 1, shared_id, ["member-b@example.test"], allowed_emails={"member-b@example.test"},
    )
    projects.respond_to_invitation(db, "member-b@example.test", shared_id, accept=True)
    assert [task["id"] for task in wbs.build_view(
        db, 2, project_id=shared_id,
    )["tasks"]] == [task_id]


def test_shared_member_backfill_preserves_users_with_existing_project_tasks(db):
    db.execute(
        "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
        (2, "既存参加者", "legacy@example.test"),
    )
    project_id = projects.create_project(db, 1, "既存共有案件", is_shared=True)
    db.execute(
        "INSERT INTO tasks (user_id, project_id, title, created_at) VALUES (?, ?, ?, ?)",
        (2, project_id, "以前からある作業", "2026-10-29T14:30:00"),
    )
    database._backfill_shared_project_members(db)

    assert project_id in [project["id"] for project in projects.list_wbs_projects(db, 2)]


def test_shared_project_routes_are_distinguished_from_personal_projects(client, db_path):
    created = client.post("/projects", data={"name": "共有案件", "is_shared": "true"})
    assert "共有案件" in created.text
    assert "共有" in created.text
    connection = database.connect(db_path)
    try:
        row = connection.execute("SELECT is_shared FROM projects WHERE name = ?", ("共有案件",)).fetchone()
        assert row["is_shared"] == 1
    finally:
        connection.close()


def test_shared_invitation_is_received_and_accepted_in_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("TODO_AUTH_MODE", "dev")
    db_path = tmp_path / "project-invitation.db"
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        connection = database.connect(db_path)
        try:
            owner_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("member-a@example.test",),
            ).fetchone()["id"]
            invitee_id = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("member-b@example.test",),
            ).fetchone()["id"]
        finally:
            connection.close()

        client.post("/dev-login", data={"user_id": str(owner_id)})
        project_form = client.get("/")
        assert 'class="project-shared-choice"' in project_form.text
        assert 'data-shared-invite-area hidden' in project_form.text
        created = client.post("/projects", data={
            "name": "招待する案件", "is_shared": "true", "invite_emails": "member-b@example.test",
        })
        assert "次回ログイン時にアプリ内で案内" in created.text
        connection = database.connect(db_path)
        try:
            project_id = connection.execute(
                "SELECT id FROM projects WHERE name = ?", ("招待する案件",),
            ).fetchone()["id"]
        finally:
            connection.close()

        client.post("/dev-login", data={"user_id": str(invitee_id)})
        invite_page = client.get("/")
        assert "プロジェクトへの招待" in invite_page.text
        assert "招待する案件" in invite_page.text
        accepted = client.post(f"/project-invitations/{project_id}/accept")
        assert "参加を承認しました" in accepted.text
        assert f'value="{project_id}">共有: 招待する案件</option>' in accepted.text
        assert "招待する案件のWBS" in client.get(f"/wbs?project_id={project_id}").text

        management = client.get("/")
        assert "参加中・管理は作成者と上司のみ" in management.text
        connection = database.connect(db_path)
        try:
            assert connection.execute(
                "SELECT status FROM project_members WHERE project_id = ? AND email = ?",
                (project_id, "member-b@example.test"),
            ).fetchone()["status"] == "accepted"
        finally:
            connection.close()