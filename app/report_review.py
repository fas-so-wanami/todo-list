"""上司が確認する、提出済み日報の一覧を組み立てる。"""
import sqlite3
from datetime import date

from app import auth, reports

REVIEW_STATUS_LABELS = {
    "unsubmitted": "未提出",
    "submitted": "提出済み",
    "modified": "修正あり・再提出待ち",
}


def build_review_view(
    db: sqlite3.Connection,
    settings: auth.AuthSettings,
    report_date: date,
) -> list[dict]:
    """許可メンバーの状態と提出版だけを返し、編集中の本文を読まない。"""
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
        report_row = None
        snapshot = None
        if user is not None:
            report_row = db.execute(
                "SELECT submitted_at, has_unsubmitted_changes FROM daily_reports "
                "WHERE user_id = ? AND report_date = ?",
                (user["id"], report_date.isoformat()),
            ).fetchone()
            snapshot = reports.get_submission_snapshot(db, user["id"], report_date)

        current_status = reports.report_status(report_row)
        status = "unsubmitted" if current_status == "draft" else current_status
        if snapshot is not None and current_status == "unsubmitted":
            status = "submitted"
        submitted_at = (
            snapshot["submitted_at"] if snapshot is not None
            else report_row["submitted_at"] if report_row is not None
            else None
        )
        time_tracking_enabled = (
            snapshot is not None and snapshot["time_tracking_enabled"] == 1
        )
        details_enabled = snapshot is not None and snapshot["details_enabled"] == 1
        task_items = snapshot["task_items"] if snapshot is not None else []
        total_minutes = sum(item["minutes"] for item in task_items) if time_tracking_enabled else None

        members.append({
            "name": user["name"] if user is not None else email,
            "sort_email": email,
            "status": status,
            "status_label": REVIEW_STATUS_LABELS[status],
            "submitted_at": submitted_at,
            "has_submission_content": snapshot is not None,
            "time_tracking_enabled": time_tracking_enabled,
            "details_enabled": details_enabled,
            "task_items": task_items,
            "total_minutes": total_minutes,
            "base_minutes": reports.BASE_WORK_MINUTES if time_tracking_enabled else None,
            "overtime_minutes": (
                total_minutes - reports.BASE_WORK_MINUTES
                if total_minutes is not None else None
            ),
            "work_style_label": (
                reports.WORK_STYLE_LABELS.get(snapshot["work_style"], "")
                if snapshot is not None and details_enabled else ""
            ),
            "leave_types": (
                reports.parse_leave_types(snapshot["leave_types"])
                if snapshot is not None and details_enabled else []
            ),
            "comment": snapshot["comment"] if snapshot is not None else "",
        })

    members.sort(key=lambda member: (
        member["status"] == "submitted",
        member["name"].casefold(),
        member["sort_email"],
    ))
    for member in members:
        del member["sort_email"]
    return members
