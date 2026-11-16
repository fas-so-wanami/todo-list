"""上司向けに、メンバーの支援が必要なタスクだけを組み立てる。"""
import sqlite3

from app import auth, tasks as task_logic

ALERT_TYPES = ("long_running", "overdue", "blocker")
ALERT_LABELS = {
    "long_running": "長時間化",
    "overdue": "期限超過",
    "blocker": "相談中",
}


def build_dashboard(
    db: sqlite3.Connection,
    settings: auth.AuthSettings,
    selected_types: set[str],
) -> list[dict]:
    """異常タスクがある人だけを残し、選択条件に合う画面情報を返す。"""
    member_emails = auth.allowed_member_emails(settings)
    users_by_email = {}
    if member_emails:
        placeholders = ",".join("?" for _ in member_emails)
        users_by_email = {
            auth.normalize_email(user["email"]): user
            for user in db.execute(
                f"SELECT id, name, email FROM users WHERE email IN ({placeholders})",
                member_emails,
            )
        }

    members = []
    for email in member_emails:
        user = users_by_email.get(email)
        if user is None:
            continue

        task_rows = db.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY title COLLATE NOCASE, id",
            (user["id"],),
        ).fetchall()
        if not task_rows:
            continue

        exception_tasks = []
        for task in task_logic.enrich(db, task_rows):
            alert_types = []
            if task["status"] != "done" and task["is_long_running"]:
                alert_types.append("long_running")
            if task["is_overdue"]:
                alert_types.append("overdue")
            if task["has_open_blocker"]:
                alert_types.append("blocker")
            if not alert_types:
                continue
            exception_tasks.append({
                "id": task["id"],
                "title": task["title"],
                "due_date": task["due_date"],
                "total_minutes": task["total_minutes"],
                "blocker_note": task["blocker_note"],
                "alert_types": alert_types,
            })

        if not exception_tasks:
            continue

        visible_tasks = [
            task for task in exception_tasks
            if set(task["alert_types"]).intersection(selected_types)
        ]
        member = {
            "name": user["name"],
            "tasks": visible_tasks,
            "empty_message": "",
        }
        if not visible_tasks:
            if not selected_types:
                member["empty_message"] = "種類を選択してください"
            else:
                member["empty_message"] = "選択中の種類には該当なし"
        members.append(member)

    members.sort(key=lambda member: (
        not bool(member["tasks"]),
        member["name"].casefold(),
    ))
    return members
