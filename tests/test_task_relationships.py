"""既存タスクの結び付けと、繰り返しの親子セットを一時DBで確認する。"""
from datetime import date
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import clock, daily, db as database, reports, tasks


def owned_row(db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    """本人の保存内容が存在することも含めて確認する。"""
    row = tasks.get_owned_task(db, 1, task_id)
    assert row is not None
    return row


def test_link_and_unlink_preserve_records(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "既存の作業", tag_names=["案件"], memo="残すメモ")
    tasks.set_status(db, child, "doing")
    daily.set_minutes(db, child, date(2026, 10, 29), 60)
    reports.toggle_item(db, 1, child, date(2026, 10, 29))
    assert tasks.change_parent(db, 1, child, parent)
    assert owned_row(db, child)["parent_id"] == parent
    assert owned_row(db, parent)["status"] == "doing"
    assert tasks.change_parent(db, 1, child, None)
    assert owned_row(db, child)["parent_id"] is None
    assert owned_row(db, child)["status"] == "doing"
    assert owned_row(db, child)["memo"] == "残すメモ"
    assert owned_row(db, parent)["status"] == "doing"
    assert daily.get_minutes(db, child, date(2026, 10, 29)) == 60
    assert reports.report_task_ids(db, 1, date(2026, 10, 29)) == {child}
    assert tasks.enrich(db, [owned_row(db, child)])[0]["tags"] == ["案件"]


def test_link_completed_child_leaves_parent_ready(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "完了した作業")
    tasks.set_status(db, child, "done")
    tasks.change_parent(db, 1, child, parent)
    assert owned_row(db, parent)["status"] == "doing"


def test_unlink_last_unfinished_child_leaves_remaining_parent_ready(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    done_child = tasks.create_task(db, 1, "完了", parent_id=parent)
    remaining = tasks.create_task(db, 1, "未着手", parent_id=parent)
    tasks.set_status(db, done_child, "done")
    tasks.change_parent(db, 1, remaining, None)
    assert owned_row(db, parent)["status"] == "doing"
    assert owned_row(db, remaining)["status"] == "todo"


def test_invalid_hierarchies_are_rejected(db: sqlite3.Connection) -> None:
    root = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "子", parent_id=root)
    other = tasks.create_task(db, 1, "別の親")
    for source, target in [(root, root), (root, other), (other, child), (child, other), (root, child)]:
        with pytest.raises(tasks.TaskRelationshipError):
            tasks.change_parent(db, 1, source, target)
    assert owned_row(db, root)["parent_id"] is None
    assert owned_row(db, child)["parent_id"] == root
    with pytest.raises(tasks.TaskRelationshipError):
        tasks.create_task(db, 1, "孫", parent_id=child)


def test_completed_missing_and_other_users_parents_are_rejected(db: sqlite3.Connection) -> None:
    child = tasks.create_task(db, 1, "対象")
    completed = tasks.create_task(db, 1, "完了した親")
    tasks.set_status(db, completed, "done")
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    other = tasks.create_task(db, 2, "非公開")
    for parent in (completed, other, 999999, 2 ** 80):
        with pytest.raises(tasks.TaskRelationshipError):
            tasks.change_parent(db, 1, child, parent)
    with pytest.raises(tasks.TaskRelationshipError):
        tasks.change_parent(db, 1, other, None)
    assert owned_row(db, child)["parent_id"] is None


def test_parent_candidates_are_owned_unfinished_roots(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    tasks.create_task(db, 1, "子", parent_id=parent)
    standalone = tasks.create_task(db, 1, "単独")
    done = tasks.create_task(db, 1, "完了")
    tasks.set_status(db, done, "done")
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    tasks.create_task(db, 2, "別の人の親")
    assert {row["id"] for row in tasks.list_parent_candidates(db, 1)} == {parent, standalone}


def test_recurring_leaf_requires_confirmation_before_linking(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "繰り返し", recurrence_rule="weekly")
    with pytest.raises(tasks.TaskRelationshipError, match="確認"):
        tasks.change_parent(db, 1, child, parent)
    assert owned_row(db, child)["recurrence_rule"] == "weekly"
    tasks.change_parent(db, 1, child, parent, confirm_clear_recurrence=True)
    assert owned_row(db, child)["recurrence_rule"] is None
    tasks.change_parent(db, 1, child, None)
    assert owned_row(db, child)["recurrence_rule"] is None


def test_legacy_child_recurrence_is_not_silently_removed(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "旧設定の子", parent_id=parent)
    tasks.update_task(db, child, recurrence_rule="daily")
    with pytest.raises(tasks.TaskRelationshipError, match="確認"):
        tasks.set_recurrence_rule(db, 1, parent, "weekly")
    with pytest.raises(tasks.TaskRelationshipError, match="繰り返し"):
        tasks.set_status(db, child, "done")
    assert owned_row(db, child)["recurrence_rule"] == "daily"
    assert owned_row(db, child)["status"] == "todo"
    tasks.set_recurrence_rule(db, 1, parent, "weekly", confirm_clear_recurrence=True)
    assert owned_row(db, child)["recurrence_rule"] is None
    assert owned_row(db, parent)["recurrence_rule"] == "weekly"
    with pytest.raises(tasks.TaskRelationshipError):
        tasks.set_recurrence_rule(db, 1, child, "daily")


@pytest.mark.parametrize(
    "rule,parent_due,child_due,next_parent_due,next_child_due",
    [
        ("weekly", "2026-11-06", "2026-11-05", "2026-11-13", "2026-11-12"),
        ("monthly", "2026-10-31", "2026-10-30", "2026-11-30", "2026-11-29"),
        ("weekly", "2026-10-01", "2026-09-30", "2026-11-05", "2026-11-04"),
        ("weekly", None, "2026-10-28", "2026-11-05", "2026-11-04"),
        ("daily", "2026-10-29", None, "2026-10-30", None),
        ("weekdays", "2026-10-30", "2026-10-29", "2026-11-02", "2026-11-01"),
    ],
)
def test_recurring_family_preserves_relative_deadlines(
    db: sqlite3.Connection, rule: str, parent_due: str | None, child_due: str | None,
    next_parent_due: str, next_child_due: str | None,
) -> None:
    parent = tasks.create_task(db, 1, "毎回の親", recurrence_rule=rule, due_date=parent_due, tag_names=["親の案件"])
    child = tasks.create_task(
        db, 1, "毎回の子", parent_id=parent, due_date=child_due, memo="引き継ぐメモ",
        importance=3, urgency=1, tag_names=["子の案件"],
    )
    tasks.toggle_waiting(db, child)
    daily.set_minutes(db, child, date(2026, 10, 29), 90)
    tasks.set_status(db, child, "done")
    assert db.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    tasks.set_status(db, parent, "done")
    next_id = db.execute("SELECT next_task_id FROM task_recurrences WHERE task_id = ?", (parent,)).fetchone()[0]
    next_parent = owned_row(db, next_id)
    next_children = db.execute("SELECT * FROM tasks WHERE user_id = ? AND parent_id = ?", (1, next_id)).fetchall()
    assert next_parent["due_date"] == next_parent_due
    assert next_parent["recurrence_rule"] == rule
    assert next_parent["status"] == "todo"
    assert len(next_children) == 1
    next_child = next_children[0]
    assert next_child["due_date"] == next_child_due
    assert next_child["recurrence_rule"] is None
    assert next_child["status"] == "todo"
    assert next_child["started_at"] is None
    assert next_child["completed_at"] is None
    assert next_child["waiting_since"] is None
    assert next_child["memo"] == "引き継ぐメモ"
    assert (next_child["importance"], next_child["urgency"]) == (3, 1)
    assert tasks.enrich(db, [next_child])[0]["tags"] == ["子の案件"]
    assert daily.get_minutes(db, next_child["id"], date(2026, 10, 29)) is None
    assert daily.get_minutes(db, child, date(2026, 10, 29)) == 90
    tasks.set_status(db, child, "doing")
    tasks.set_status(db, child, "done")
    tasks.set_status(db, parent, "done")
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ?", (1,)).fetchone()[0] == 4


def seed_linkable_tasks(db_path: Path, recurrence: str | None = None) -> tuple[int, int]:
    """一覧から編集して結び付けるための、別々のタスクを作る。"""
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "結び付け先")
        child = tasks.create_task(connection, 1, "結び付ける作業", recurrence_rule=recurrence)
        daily.add_to_plan(connection, parent, date(2026, 10, 29))
        connection.commit()
        return parent, child
    finally:
        connection.close()


def test_edit_form_links_and_unlinks_existing_task(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    assert 'name="parent_id"' in client.get("/").text
    response = client.post(f"/tasks/{child}/edit", data={"title": "結び付ける作業", "parent_id": str(parent)})
    assert "親タスクに結び付けました" in response.text
    assert "明日以降にやる」から「今日やる" in response.text
    assert "0/1件完了" in response.text
    assert "親から外して独立させる" in response.text
    response = client.post(f"/tasks/{child}/edit", data={"title": "結び付ける作業", "parent_id": ""})
    assert "親から外して独立させました" in response.text
    assert "0/0件完了" not in response.text
    connection = database.connect(db_path)
    try:
        assert owned_row(connection, child)["parent_id"] is None
    finally:
        connection.close()


@pytest.mark.parametrize(
    "parent_in_today,destination,expected_section",
    [(True, "later", "today"), (False, "today", "others")],
)
def test_quick_add_can_create_child_in_parent_section(
    client: TestClient, db_path: Path, parent_in_today: bool, destination: str, expected_section: str,
) -> None:
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "作成先の親")
        if parent_in_today:
            daily.add_to_plan(connection, parent, clock.today())
        connection.commit()
    finally:
        connection.close()

    form_response = client.get("/")
    assert f'<option value="{parent}">作成先の親（No.{parent}）</option>' in form_response.text
    response = client.post(
        "/tasks", data={"title": "新しく作る子", "parent_id": str(parent), "destination": destination},
    )
    assert "新しく作る子" in response.text

    connection = database.connect(db_path)
    try:
        child = connection.execute(
            "SELECT id FROM tasks WHERE user_id = ? AND title = ?", (1, "新しく作る子")
        ).fetchone()
        assert child is not None
        assert owned_row(connection, child["id"])["parent_id"] == parent
        board = daily.build_board(connection, 1, set())
        assert any(task["id"] == child["id"] for task in board[expected_section])
    finally:
        connection.close()


@pytest.mark.parametrize("parent_value", ["abc", "-1", "１", "9" * 100, "999999"])
def test_invalid_parent_does_not_save_other_edits(
    client: TestClient, db_path: Path, parent_value: str,
) -> None:
    parent, child = seed_linkable_tasks(db_path)
    response = client.post(
        f"/tasks/{child}/edit", data={"title": "入力は残すが保存しない", "parent_id": parent_value, "memo": "書きかけ"},
    )
    assert 'value="入力は残すが保存しない"' in response.text
    assert "書きかけ" in response.text
    assert 'class="task-detail" open' in response.text
    connection = database.connect(db_path)
    try:
        row = owned_row(connection, child)
        assert row["title"] == "結び付ける作業"
        assert row["parent_id"] is None
        assert row["memo"] == ""
    finally:
        connection.close()


def test_confirmation_checkbox_is_required_and_then_works(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path, recurrence="weekly")
    form = {"title": "変更するタイトル", "parent_id": str(parent), "recurrence_rule": "weekly"}
    response = client.post(f"/tasks/{child}/edit", data=form)
    assert "外すことを確認" in response.text
    connection = database.connect(db_path)
    try:
        assert owned_row(connection, child)["parent_id"] is None
        assert owned_row(connection, child)["recurrence_rule"] == "weekly"
        assert owned_row(connection, child)["title"] == "結び付ける作業"
        response = client.post(f"/tasks/{child}/edit", data={**form, "confirm_clear_recurrence": "true"})
        assert "親タスクに結び付けました" in response.text
        assert owned_row(connection, child)["parent_id"] == parent
        assert owned_row(connection, child)["recurrence_rule"] is None
    finally:
        connection.close()


def test_final_link_copies_saved_child_details_to_next_set(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    connection = database.connect(db_path)
    try:
        tasks.update_task(connection, parent, recurrence_rule="weekly", due_date="2026-11-06")
        tasks.set_status(connection, child, "done")
        connection.commit()
        response = client.post(f"/tasks/{child}/edit", data={
            "title": "保存した子のタイトル", "parent_id": str(parent),
            "due_date": "2026-11-05", "memo": "保存したメモ", "tag_text": "引き継ぐ案件",
        })
        assert response.status_code == 200
        assert "HX-Trigger-After-Settle" not in response.headers
        assert "親も完了にする" in response.text
        client.post(f"/tasks/{parent}/status/done")
        next_parent_id = connection.execute(
            "SELECT next_task_id FROM task_recurrences WHERE task_id = ?", (parent,),
        ).fetchone()[0]
        next_child = connection.execute("SELECT * FROM tasks WHERE parent_id = ?", (next_parent_id,)).fetchone()
        assert next_child is not None
        assert next_child["title"] == "保存した子のタイトル"
        assert next_child["memo"] == "保存したメモ"
        assert next_child["due_date"] == "2026-11-12"
        assert tasks.enrich(connection, [next_child])[0]["tags"] == ["引き継ぐ案件"]
    finally:
        connection.close()


def test_edit_without_parent_field_keeps_existing_relationship(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    client.post(f"/tasks/{child}/edit", data={"title": "作業", "parent_id": str(parent)})
    client.post(f"/tasks/{child}/edit", data={"title": "題名だけ変更"})
    connection = database.connect(db_path)
    try:
        assert owned_row(connection, child)["parent_id"] == parent
    finally:
        connection.close()


def test_empty_recurrence_field_clears_the_setting(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path, recurrence="weekly")
    response = client.post(f"/tasks/{child}/edit", data={"title": "単独のまま", "recurrence_rule": ""})
    assert response.status_code == 200
    connection = database.connect(db_path)
    try:
        assert owned_row(connection, child)["recurrence_rule"] is None
        assert owned_row(connection, child)["parent_id"] is None
    finally:
        connection.close()


def test_parent_candidates_are_available_across_tag_filter(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    connection = database.connect(db_path)
    try:
        tasks.set_tags(connection, child, ["表示中"])
        connection.commit()
        response = client.get("/?tag=表示中")
        assert f'<option value="{parent}"' in response.text
        assert "結び付け先（No." in response.text
    finally:
        connection.close()


def test_parent_edit_clears_legacy_child_rules_only_after_confirmation(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    connection = database.connect(db_path)
    try:
        tasks.change_parent(connection, 1, child, parent)
        tasks.update_task(connection, child, recurrence_rule="daily")
        connection.commit()
        assert "子1件の個別の繰り返し設定を外し" in client.get("/").text
        form = {"title": "結び付け先", "recurrence_rule": "weekly"}
        response = client.post(f"/tasks/{parent}/edit", data=form)
        assert "外すことを確認" in response.text
        assert owned_row(connection, child)["recurrence_rule"] == "daily"
        assert owned_row(connection, parent)["recurrence_rule"] is None
        client.post(f"/tasks/{parent}/edit", data={**form, "confirm_clear_recurrence": "true"})
        assert owned_row(connection, child)["recurrence_rule"] is None
        assert owned_row(connection, parent)["recurrence_rule"] == "weekly"
    finally:
        connection.close()


def test_rejected_link_rolls_back_parent_state_and_edits(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    connection = database.connect(db_path)
    try:
        tasks.update_task(connection, parent, recurrence_rule="weekly")
        tasks.set_status(connection, child, "done")
        connection.commit()
        response = client.post(f"/tasks/{child}/edit", data={
            "title": "保存してはいけない変更", "parent_id": str(parent), "recurrence_rule": "daily",
        })
        assert "確認" in response.text
        assert owned_row(connection, child)["parent_id"] is None
        assert owned_row(connection, child)["title"] == "結び付ける作業"
        assert owned_row(connection, parent)["status"] == "todo"
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    finally:
        connection.close()


def test_parent_completion_refuses_unconfirmed_legacy_rules(client: TestClient, db_path: Path) -> None:
    parent, child = seed_linkable_tasks(db_path)
    connection = database.connect(db_path)
    try:
        tasks.change_parent(connection, 1, child, parent)
        tasks.set_status(connection, child, "done")
        remaining = tasks.create_task(connection, 1, "残りの子", parent_id=parent)
        tasks.update_task(connection, parent, recurrence_rule="weekly")
        tasks.update_task(connection, child, recurrence_rule="daily")
        reports.toggle_item(connection, 1, remaining, date(2026, 10, 29))
        connection.commit()
        response = client.post(f"/tasks/{remaining}/delete")
        assert "HX-Trigger-After-Settle" not in response.headers
        assert "親も完了にする" in response.text
        assert tasks.get_task(connection, remaining) is None
        assert reports.report_task_ids(connection, 1, date(2026, 10, 29)) == set()
        response = client.post(f"/tasks/{parent}/status/done")
        assert "子の個別の繰り返し設定が残っています" in response.text
        assert owned_row(connection, parent)["status"] == "doing"
        assert owned_row(connection, child)["recurrence_rule"] == "daily"
        assert connection.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    finally:
        connection.close()
