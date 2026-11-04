"""子の件数・状態から親の状態が決まることと、既存データの引き継ぎを確認する。"""
from datetime import date
from pathlib import Path
import sqlite3

import pytest

from app import daily, db as database, reports, tasks


def make_family(db: sqlite3.Connection) -> tuple[int, int, int]:
    """2件の子を持つ親を作り、部分完了と全件完了を確認できるようにする。"""
    parent = tasks.create_task(db, 1, "資料作成")
    first = tasks.create_task(db, 1, "下書き", parent_id=parent)
    second = tasks.create_task(db, 1, "確認", parent_id=parent)
    return parent, first, second


def task_state(db: sqlite3.Connection, task_id: int) -> str:
    """保存された状態を直接確認する。"""
    task = tasks.get_owned_task(db, 1, task_id)
    assert task is not None
    return task["status"]


def test_parent_follows_all_child_transitions(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    assert task_state(db, parent) == "todo"
    tasks.set_status(db, first, "doing")
    assert task_state(db, parent) == "doing"
    tasks.set_status(db, first, "done")
    assert task_state(db, parent) == "doing"
    parent_view = tasks.enrich(db, [tasks.get_task(db, parent)])[0]
    assert (parent_view["subtask_done"], parent_view["subtask_total"]) == (1, 2)
    tasks.set_status(db, second, "done")
    assert task_state(db, parent) == "doing"
    assert tasks.get_task(db, parent)["completed_at"] is None
    tasks.set_status(db, parent, "done")
    assert task_state(db, parent) == "done"
    assert tasks.get_task(db, parent)["completed_at"] == "2026-10-29T14:30:00"
    tasks.set_status(db, first, "doing")
    assert task_state(db, parent) == "doing"
    assert tasks.get_task(db, parent)["completed_at"] is None
    tasks.set_status(db, first, "todo")
    tasks.set_status(db, second, "todo")
    assert task_state(db, parent) == "todo"
    assert tasks.get_task(db, parent)["started_at"] is None


@pytest.mark.parametrize("status", tasks.STATUSES)
def test_parent_cannot_change_children_by_hand(db: sqlite3.Connection, status: str) -> None:
    parent, first, second = make_family(db)
    with pytest.raises((tasks.ParentTaskStatusError, tasks.UnfinishedSubtasksError)):
        tasks.set_status(db, parent, status)
    assert [task_state(db, child) for child in (first, second)] == ["todo", "todo"]


def test_add_child_reopens_completed_parent(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    tasks.set_status(db, first, "done")
    tasks.set_status(db, second, "done")
    tasks.set_status(db, parent, "done")
    child = tasks.create_task(db, 1, "追加確認", parent_id=parent)
    assert task_state(db, parent) == "doing"
    assert task_state(db, child) == "todo"


def test_first_child_makes_parent_state_automatic(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "単独だったタスク")
    tasks.set_status(db, parent, "done")
    tasks.create_task(db, 1, "最初の子", parent_id=parent)
    assert task_state(db, parent) == "todo"


def test_delete_last_unfinished_child_leaves_parent_ready(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    tasks.set_status(db, first, "done")
    tasks.delete_task(db, second)
    assert task_state(db, parent) == "doing"
    view = tasks.enrich(db, [tasks.get_task(db, parent)])[0]
    assert (view["subtask_done"], view["subtask_total"]) == (1, 1)


def test_delete_completed_child_can_reset_parent_to_todo(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    tasks.set_status(db, first, "done")
    tasks.delete_task(db, first)
    assert task_state(db, parent) == "todo"
    assert task_state(db, second) == "todo"


@pytest.mark.parametrize("status", tasks.STATUSES)
def test_zero_children_keeps_parent_state(db: sqlite3.Connection, status: str) -> None:
    parent = tasks.create_task(db, 1, "親")
    child = tasks.create_task(db, 1, "最後の子", parent_id=parent)
    tasks.set_status(db, child, status)
    if status == "done":
        tasks.set_status(db, parent, "done")
    tasks.delete_task(db, child)
    assert task_state(db, parent) == status
    view = tasks.enrich(db, [tasks.get_task(db, parent)])[0]
    assert (view["subtask_done"], view["subtask_total"]) == (0, 0)
    tasks.set_status(db, parent, "doing")
    assert task_state(db, parent) == "doing"


def test_completed_children_stay_with_parent_until_next_day(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, first, second = make_family(db)
    tasks.set_status(db, first, "done")
    db.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", ("2026-10-28T15:00:00", first))
    tasks.set_status(db, second, "done")
    tasks.set_status(db, parent, "done")
    board = daily.build_board(db, 1, set())
    assert [task["id"] for task in board["today"]] == [parent, first, second]
    assert daily.list_archive(db, 1) == []
    monkeypatch.setattr(daily.clock, "today", lambda: date(2026, 10, 30))
    archive = daily.list_archive(db, 1)
    assert [task["id"] for task in archive[0]["tasks"]] == [parent, first, second]
    tasks.set_status(db, first, "doing")
    assert task_state(db, parent) == "doing"
    assert daily.list_archive(db, 1) == []
    assert [task["id"] for task in daily.build_board(db, 1, set())["today"]] == [parent, first, second]


def test_family_stays_together_and_deferral_still_works(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    tasks.set_status(db, first, "doing")
    assert [task["id"] for task in daily.build_board(db, 1, set())["today"]] == [parent, first, second]
    daily.move_to_later(db, parent)
    board = daily.build_board(db, 1, set())
    assert board["today"] == []
    assert [task["id"] for task in board["others"]] == [parent, first, second]


@pytest.mark.parametrize("progress,status", [(0, "todo"), (25, "doing"), (50, "doing"), (75, "doing"), (100, "done")])
def test_existing_progress_is_preserved_as_state(
    db: sqlite3.Connection, db_path: Path, progress: int, status: str,
) -> None:
    task_id = tasks.create_task(db, 1, "引き継ぎ")
    db.execute(
        "UPDATE tasks SET progress = ?, status = ?, completed_at = ? WHERE id = ?",
        (progress, status, "2026-10-28T10:00:00" if status == "done" else None, task_id),
    )
    daily.set_minutes(db, task_id, date(2026, 10, 28), 90)
    db.commit()
    database.init_db(db_path)
    assert task_state(db, task_id) == status
    assert tasks.get_task(db, task_id)["progress"] == progress
    assert daily.get_minutes(db, task_id, date(2026, 10, 28)) == 90


def test_startup_recalculates_parents_without_creating_recurrences(db: sqlite3.Connection, db_path: Path) -> None:
    parent, first, second = make_family(db)
    db.execute(
        "UPDATE tasks SET status = 'done', progress = 100, completed_at = ? WHERE id IN (?, ?)",
        ("2026-10-28T10:00:00", first, second),
    )
    tasks.update_task(db, parent, recurrence_rule="weekly")
    db.commit()
    database.init_db(db_path)
    assert task_state(db, parent) == "doing"
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
    completed_at = tasks.get_task(db, parent)["completed_at"]
    database.init_db(db_path)
    assert tasks.get_task(db, parent)["completed_at"] == completed_at
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3


def test_sync_does_not_change_another_users_parent(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    parent = tasks.create_task(db, 2, "別の人の親")
    tasks.synchronize_parent_status(db, 1, parent)
    assert tasks.get_task(db, parent)["status"] == "todo"


def test_counts_ignore_children_belonging_to_another_user(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    parent, first, second = make_family(db)
    db.execute(
        "INSERT INTO tasks (user_id, parent_id, title, created_at) VALUES (?, ?, ?, ?)",
        (2, parent, "別の利用者の不整合データ", "2026-10-28T15:00:00"),
    )
    tasks.set_status(db, first, "done")
    tasks.set_status(db, second, "done")
    parent_row = tasks.get_task(db, parent)
    assert parent_row is not None
    view = tasks.enrich(db, [parent_row])[0]
    assert (view["subtask_done"], view["subtask_total"]) == (2, 2)
    assert view["status"] == "doing"


def test_count_uses_all_children_even_when_filter_hides_some(db: sqlite3.Connection) -> None:
    parent = tasks.create_task(db, 1, "親 #案件", tag_names=["案件"])
    children = [tasks.create_task(db, 1, f"子{index}", parent_id=parent) for index in range(5)]
    for child in children[:2]:
        tasks.set_status(db, child, "done")
    board = daily.build_board(db, 1, set(), "案件")
    visible = board["today"] + board["others"]
    assert len(visible) == 1
    assert visible[0]["id"] == parent
    assert (visible[0]["subtask_done"], visible[0]["subtask_total"]) == (2, 5)


def test_repeated_completion_does_not_duplicate_next_occurrence(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    tasks.update_task(db, parent, recurrence_rule="weekly", due_date="2026-10-30")
    tasks.set_status(db, first, "done")
    tasks.set_status(db, second, "done")
    assert db.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0] == 0
    tasks.set_status(db, parent, "done")
    next_task = db.execute("SELECT * FROM tasks WHERE id NOT IN (?, ?, ?)", (parent, first, second)).fetchone()
    assert next_task is not None
    assert next_task["due_date"] == "2026-11-06"
    tasks.set_status(db, second, "doing")
    tasks.set_status(db, second, "done")
    tasks.set_status(db, parent, "done")
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6


def test_repeated_completion_after_next_is_deleted_does_not_recreate_it(db: sqlite3.Connection) -> None:
    task_id = tasks.create_task(db, 1, "繰り返し", recurrence_rule="daily")
    next_id = tasks.set_status(db, task_id, "done")
    assert next_id is not None
    tasks.delete_task(db, next_id)
    tasks.set_status(db, task_id, "doing")
    assert tasks.set_status(db, task_id, "done") is None
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_done_today_flat_includes_subtasks_from_other_section(db: sqlite3.Connection) -> None:
    """「明日以降にやる」の段にいた子タスクが完了した場合も、done_today_flat に含まれることを確認する。
    要件②: 親子関係なく当日完了した全タスクをフラットに一覧表示する。"""
    # 「明日以降にやる」の段にある子タスクを作る
    parent = tasks.create_task(db, 1, "親タスク")
    child = tasks.create_task(db, 1, "子タスク", parent_id=parent)
    # 子タスクだけ完了する(親は未完了のまま)
    tasks.set_status(db, child, "done")
    board = daily.build_board(db, 1, set())
    # done_today_flat に子タスクが含まれることを確認
    done_ids = {task["id"] for task in board["done_today"]}
    assert child in done_ids
    assert board["done_today_count"] == 1


def test_done_today_flat_sorted_by_completed_at(db: sqlite3.Connection) -> None:
    """完了時刻順に並ぶことを確認する。"""
    first = tasks.create_task(db, 1, "先に完了")
    second = tasks.create_task(db, 1, "後に完了")
    tasks.set_status(db, first, "done")
    # 完了時刻を手動で差を付ける
    db.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", ("2026-10-29T10:00:00", first))
    tasks.set_status(db, second, "done")
    db.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", ("2026-10-29T12:00:00", second))
    board = daily.build_board(db, 1, set())
    done_ids = [task["id"] for task in board["done_today"]]
    assert done_ids == [first, second]


def test_report_minutes_and_flags_are_not_lost_when_states_change(db: sqlite3.Connection) -> None:
    parent, first, second = make_family(db)
    reports.toggle_item(db, 1, first, date(2026, 10, 29))
    reports.save_draft(
        db, 1, date(2026, 10, 29), {first: 45}, work_style="office", leave_types=[], comment="",
    )
    tasks.set_status(db, first, "done")
    tasks.set_status(db, second, "done")
    view = reports.build_report_view(db, 1, date(2026, 10, 29))
    assert view["total_minutes"] == 45
    assert view["task_items"][0]["id"] == first
