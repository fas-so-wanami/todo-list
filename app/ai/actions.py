"""Copilot(MCP経由)から届いた操作をDBへ反映し、Undoできるよう変更前の状態を記録する。"""
import json
import sqlite3
from datetime import date

from app import blockers, clock, daily, tasks

STATE_FIELDS = ("status", "progress", "started_at", "completed_at")


def _find_task(
    db: sqlite3.Connection, user_id: int, action: dict, created_tasks_by_title: dict[str, int]
) -> sqlite3.Row | None:
    task_id = action.get("task_id")
    if task_id is not None:
        row = tasks.get_task(db, task_id)
        return row if row and row["user_id"] == user_id else None

    title = action.get("task_title", "")
    if title in created_tasks_by_title:
        return tasks.get_task(db, created_tasks_by_title[title])
    rows = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? ORDER BY status = 'done', id DESC", (user_id,)
    ).fetchall()
    exact_matches = [row for row in rows if row["title"] == title]
    partial_matches = [row for row in rows if title and title in row["title"]]
    candidates = exact_matches or partial_matches
    return candidates[0] if candidates else None


def _snapshot_state(task: sqlite3.Row) -> dict:
    return {"kind": "restore_task", "task_id": task["id"], "fields": {key: task[key] for key in STATE_FIELDS}}


def _change_status(db: sqlite3.Connection, task: sqlite3.Row, status: str, undo_steps: list[dict]) -> None:
    undo_steps.append(_snapshot_state(task))
    next_task_id = tasks.set_status(db, task["id"], status)
    if next_task_id:
        undo_steps.append({"kind": "delete_task", "task_id": next_task_id})


def _change_progress(db: sqlite3.Connection, task: sqlite3.Row, progress: int, undo_steps: list[dict]) -> None:
    undo_steps.append(_snapshot_state(task))
    next_task_id = tasks.set_progress(db, task["id"], progress)
    if next_task_id:
        undo_steps.append({"kind": "delete_task", "task_id": next_task_id})


def describe_progress(progress: int) -> str:
    return f"{progress}%({tasks.STATUS_LABELS[tasks.status_for_progress(progress)]})"


def _apply_action(
    db: sqlite3.Connection,
    user_id: int,
    action: dict,
    created_tasks_by_title: dict[str, int],
    undo_steps: list[dict],
) -> tuple[bool, str]:
    action_type = action["type"]

    if action_type == "create":
        title, tag_names = tasks.split_title_and_tags(action["title"])
        task_id = tasks.create_task(db, user_id, title, tag_names=tag_names)
        daily.add_to_plan(db, task_id, clock.today())
        undo_steps.append({"kind": "delete_task", "task_id": task_id})
        created_tasks_by_title[action["title"]] = task_id
        created_tasks_by_title[title] = task_id
        progress = action.get("progress", 0)
        if progress:
            tasks.set_progress(db, task_id, progress)
        return True, f"新しいタスク「{title}」を作成しました。進捗: {describe_progress(progress)}"

    task = _find_task(db, user_id, action, created_tasks_by_title)
    if task is None:
        reference = action.get("task_title") or f"ID {action.get('task_id')}"
        return False, f"対象のタスクが見つからず反映できませんでした: {reference}"
    title = task["title"]

    is_completing = action_type == "done" or (action_type == "progress" and action.get("progress") == 100)
    unfinished_count = len(tasks.list_unfinished_subtask_ids(db, task["id"])) if is_completing else 0
    if unfinished_count:
        return False, f"「{title}」はサブタスクが {unfinished_count} 件残っているため完了にできません"

    if action_type in ("start", "done"):
        new_status = "doing" if action_type == "start" else "done"
        if task["status"] == new_status:
            return False, f"「{title}」はすでに{tasks.STATUS_LABELS[new_status]}です"
        _change_status(db, task, new_status, undo_steps)
        return True, f"「{title}」を{tasks.STATUS_LABELS[new_status]}にしました"

    if action_type == "progress":
        progress = action["progress"]
        if task["progress"] == progress:
            return False, f"「{title}」はすでに{describe_progress(progress)}です"
        _change_progress(db, task, progress, undo_steps)
        return True, f"「{title}」の進捗を{describe_progress(progress)}にしました"

    if action_type == "add_time":
        work_date = clock.today()
        previous_minutes = daily.add_minutes(db, task["id"], work_date, action["minutes"], source="ai")
        undo_steps.append({
            "kind": "restore_minutes", "task_id": task["id"],
            "work_date": work_date.isoformat(), "minutes": previous_minutes,
        })
        return True, f"「{title}」に今日の作業時間 {action['minutes']}分 を追加しました"

    if action_type == "blocker":
        blocker_id = blockers.raise_blocker(db, task["id"], source="ai", note=action.get("note", ""))
        if blocker_id is None:
            return False, f"「{title}」はすでに困りごとフラグが付いています"
        undo_steps.append({"kind": "delete_blocker", "blocker_id": blocker_id})
        return True, f"「{title}」に困りごとフラグを付けました"

    # memo
    undo_steps.append({"kind": "restore_memo", "task_id": task["id"], "memo": task["memo"]})
    timestamp = clock.now_iso()[5:16].replace("T", " ")
    appended_memo = f"{task['memo']}\n[{timestamp}] {action['text']}".strip()
    tasks.update_task(db, task["id"], memo=appended_memo)
    return True, f"「{title}」のメモに追記しました"


def apply_actions(db: sqlite3.Connection, user_id: int, raw_text: str, actions: list[dict]) -> dict:
    """アクションを順に反映し、報告1件分の履歴(適用結果とUndo情報)を保存して返す。
    1件も反映できなかった場合は履歴を残さない(report_id は None)。"""
    undo_steps: list[dict] = []
    created_tasks_by_title: dict[str, int] = {}
    results = []
    for action in actions:
        succeeded, message = _apply_action(db, user_id, action, created_tasks_by_title, undo_steps)
        results.append({"action": action, "ok": succeeded, "message": message})
    if not any(result["ok"] for result in results):
        return {"report_id": None, "results": results}

    cursor = db.execute(
        "INSERT INTO ai_reports (user_id, raw_text, parsed_actions_json, undo_json, applied_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, raw_text, json.dumps(results, ensure_ascii=False),
         json.dumps(undo_steps, ensure_ascii=False), clock.now_iso()),
    )
    return {"report_id": cursor.lastrowid, "results": results}


def _undo_step(db: sqlite3.Connection, step: dict) -> None:
    kind = step["kind"]
    if kind == "restore_task":
        if tasks.get_task(db, step["task_id"]):
            fields = dict(step["fields"])
            if "status" in fields and "progress" not in fields:  # 進捗率の導入前に記録された報告
                fields["progress"] = {"todo": 0, "doing": tasks.DEFAULT_DOING_PROGRESS, "done": 100}[fields["status"]]
            tasks.restore_fields(db, step["task_id"], fields)
    elif kind == "delete_task":
        tasks.delete_task(db, step["task_id"])
    elif kind == "restore_minutes":
        if tasks.get_task(db, step["task_id"]):
            daily.set_minutes(
                db, step["task_id"], date.fromisoformat(step["work_date"]), step["minutes"] or 0
            )
    elif kind == "delete_blocker":
        db.execute("DELETE FROM blockers WHERE id = ?", (step["blocker_id"],))
    elif kind == "restore_memo":
        if tasks.get_task(db, step["task_id"]):
            tasks.update_task(db, step["task_id"], memo=step["memo"])


def undo_report(db: sqlite3.Connection, user_id: int, report_id: int) -> bool:
    """報告の反映を取り消す。取り消せたら True、存在しない/取り消し済みなら False。"""
    report = db.execute(
        "SELECT * FROM ai_reports WHERE id = ? AND user_id = ? AND undone_at IS NULL",
        (report_id, user_id),
    ).fetchone()
    if report is None:
        return False
    for step in reversed(json.loads(report["undo_json"])):
        _undo_step(db, step)
    db.execute("UPDATE ai_reports SET undone_at = ? WHERE id = ?", (clock.now_iso(), report_id))
    return True


def list_recent_reports(db: sqlite3.Connection, user_id: int, limit: int = 5) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM ai_reports WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    return [
        {
            "id": row["id"],
            "raw_text": row["raw_text"],
            "results": json.loads(row["parsed_actions_json"]),
            "is_undone": row["undone_at"] is not None,
        }
        for row in rows
    ]
