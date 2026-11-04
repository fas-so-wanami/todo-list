"""日報: タスク一覧で選んだタスクに作業時間を付け、勤務形態・休暇・備考と合わせて提出する。

- 1人1日1件。入力は下書きとして自動保存し、ブラウザバックしても消えない。
- 提出後も修正できる。修正すると「修正あり(未提出)」になり、再提出で確定する。
- タスクごとの作業時間は time_logs にも書き込み、長時間化(累計5時間以上)の判定に使う。
- 「作業時間」と「勤務形態・休暇など」は、それぞれ別のON/OFFで入力欄を出すか選べる(どちらも任意)。
  OFFのときはその項目を選ばなくても提出できる。ON/OFFは日報ごとに保存し、次に開いたときも覚えている。
  まだ一度も開いていない日は両方ONから始まる。
"""
import re
import sqlite3
from datetime import date

from app import clock, daily, tasks

BASE_WORK_MINUTES = 7 * 60 + 45  # 基本勤務時間(休憩を除く実働)
MAX_MINUTES_PER_TASK = 24 * 60
WORK_STYLE_LABELS = {"office": "出社", "telework": "テレワーク"}
EARLY_LEAVE = "早退"
EXCLUSIVE_LEAVE_TYPES = ("時間休", "午前休", "午後休")  # 同じ日に2つ以上付くことはない
LEAVE_TYPES = (EARLY_LEAVE, *EXCLUSIVE_LEAVE_TYPES)
STATUS_LABELS = {"draft": "下書き", "submitted": "提出済み", "modified": "修正あり(未提出)"}

_HHMM_PATTERN = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")
_HOURS_PATTERN = re.compile(r"^\s*(\d{1,2})\s*$")


def parse_hhmm(text: str | None) -> int | None:
    """「1:30」「1：30」「2」(時間のみ)を分に変換する。空欄は 0、読めない値は None。"""
    if text is None or not text.strip():
        return 0
    matched = _HHMM_PATTERN.match(text)
    if matched:
        hours, minutes = int(matched.group(1)), int(matched.group(2))
        if minutes >= 60:
            return None
        return min(hours * 60 + minutes, MAX_MINUTES_PER_TASK)
    matched = _HOURS_PATTERN.match(text)
    if matched:
        return min(int(matched.group(1)) * 60, MAX_MINUTES_PER_TASK)
    return None


def format_hhmm(minutes: int | None, signed: bool = False) -> str:
    """分を「7:45」の形にする。signed=True なら「+0:30」「−1:00」のように符号を付ける。"""
    minutes = minutes or 0
    sign = ""
    if signed:
        sign = "+" if minutes > 0 else ("−" if minutes < 0 else "±")
    hours, rest_minutes = divmod(abs(minutes), 60)
    return f"{sign}{hours}:{rest_minutes:02d}"


def parse_leave_types(text: str) -> list[str]:
    return [name for name in (text or "").split(",") if name in LEAVE_TYPES]


def normalize_leave_types(names: list[str]) -> list[str]:
    """休暇などを決まった順に並べる。時間休・午前休・午後休は排他なので、複数あれば最後に指定されたものだけ残す。"""
    exclusive_names = [name for name in names if name in EXCLUSIVE_LEAVE_TYPES]
    kept = {name for name in names if name == EARLY_LEAVE}
    if exclusive_names:
        kept.add(exclusive_names[-1])
    return [name for name in LEAVE_TYPES if name in kept]


def toggle_time_tracking(db: sqlite3.Connection, user_id: int, report_date: date) -> bool:
    """「作業時間」の入力欄を出すかを切り替え、切り替え後のON/OFFを返す。"""
    report = get_or_create_report(db, user_id, report_date)
    enabled = not report["time_tracking_enabled"]
    db.execute(
        "UPDATE daily_reports SET time_tracking_enabled = ? WHERE id = ? AND user_id = ?",
        (int(enabled), report["id"], user_id),
    )
    _touch(db, report["id"])
    return enabled


def toggle_details(db: sqlite3.Connection, user_id: int, report_date: date) -> bool:
    """「勤務形態・休暇など」の入力欄を出すかを切り替え、切り替え後のON/OFFを返す。"""
    report = get_or_create_report(db, user_id, report_date)
    enabled = not report["details_enabled"]
    db.execute(
        "UPDATE daily_reports SET details_enabled = ? WHERE id = ? AND user_id = ?",
        (int(enabled), report["id"], user_id),
    )
    _touch(db, report["id"])
    return enabled


def find_report(db: sqlite3.Connection, user_id: int, report_date: date) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM daily_reports WHERE user_id = ? AND report_date = ?",
        (user_id, report_date.isoformat()),
    ).fetchone()


def get_or_create_report(db: sqlite3.Connection, user_id: int, report_date: date) -> sqlite3.Row:
    db.execute(
        "INSERT OR IGNORE INTO daily_reports (user_id, report_date, updated_at) VALUES (?, ?, ?)",
        (user_id, report_date.isoformat(), clock.now_iso()),
    )
    return find_report(db, user_id, report_date)


def report_task_ids(db: sqlite3.Connection, user_id: int, report_date: date) -> set[int]:
    return {row["task_id"] for row in db.execute(
        "SELECT i.task_id FROM daily_report_items i JOIN daily_reports r ON r.id = i.report_id "
        "WHERE r.user_id = ? AND r.report_date = ?",
        (user_id, report_date.isoformat()),
    )}


def _touch(db: sqlite3.Connection, report_id: int) -> None:
    """内容が変わったことを記録する。提出済みなら「修正あり(未提出)」にする。"""
    db.execute(
        "UPDATE daily_reports SET updated_at = ?, "
        "has_unsubmitted_changes = CASE WHEN submitted_at IS NULL THEN 0 ELSE 1 END WHERE id = ?",
        (clock.now_iso(), report_id),
    )


def toggle_item(db: sqlite3.Connection, user_id: int, task_id: int, report_date: date) -> bool:
    """タスクを日報に入れる/外す。入れたら True。既に作業時間の記録があればそれを初期値にする。"""
    task = tasks.get_task(db, task_id)
    if task is None or task["user_id"] != user_id:
        return False
    report = get_or_create_report(db, user_id, report_date)
    if task_id in report_task_ids(db, user_id, report_date):
        remove_item(db, user_id, task_id, report_date)
        return False
    logged_minutes = daily.get_minutes(db, task_id, report_date) or 0
    db.execute(
        "INSERT INTO daily_report_items (report_id, task_id, minutes) VALUES (?, ?, ?)",
        (report["id"], task_id, logged_minutes),
    )
    _touch(db, report["id"])
    return True


def remove_item(db: sqlite3.Connection, user_id: int, task_id: int, report_date: date) -> None:
    report = find_report(db, user_id, report_date)
    if report is None:
        return
    deleted = db.execute(
        "DELETE FROM daily_report_items WHERE report_id = ? AND task_id = ?", (report["id"], task_id)
    ).rowcount
    if deleted:
        daily.set_minutes(db, task_id, report_date, 0)
        _touch(db, report["id"])


def save_draft(
    db: sqlite3.Connection,
    user_id: int,
    report_date: date,
    minutes_by_task_id: dict[int, int],
    work_style: str | None,
    leave_types: list[str] | None,
    comment: str,
) -> None:
    """入力内容を保存する。内容が変わったときだけ「変更あり」として記録する(提出後の「修正あり」判定に使う)。

    作業時間は「作業時間」ON/OFFがOFFのときは入力欄が無いため、minutes_by_task_id が空のまま届く(既存の記録は消さない)。
    work_style・leave_types は「勤務形態・休暇など」ON/OFFがOFFのときは None が届く。その場合は今の値をそのまま保ち、意図せず消さない。
    """
    report = get_or_create_report(db, user_id, report_date)
    changed = False

    new_fields = {
        "work_style": work_style if work_style in WORK_STYLE_LABELS else report["work_style"],
        "leave_types": ",".join(normalize_leave_types(leave_types)) if leave_types is not None else report["leave_types"],
        "comment": comment.strip()[:2000],
    }
    if any(report[column] != value for column, value in new_fields.items()):
        db.execute(
            "UPDATE daily_reports SET work_style = ?, leave_types = ?, comment = ? WHERE id = ?",
            (new_fields["work_style"], new_fields["leave_types"], new_fields["comment"], report["id"]),
        )
        changed = True

    current_minutes = {row["task_id"]: row["minutes"] for row in db.execute(
        "SELECT task_id, minutes FROM daily_report_items WHERE report_id = ?", (report["id"],))}
    for task_id, minutes in minutes_by_task_id.items():
        if task_id not in current_minutes or current_minutes[task_id] == minutes:
            continue
        db.execute(
            "UPDATE daily_report_items SET minutes = ? WHERE report_id = ? AND task_id = ?",
            (minutes, report["id"], task_id),
        )
        daily.set_minutes(db, task_id, report_date, minutes)
        changed = True

    if changed:
        _touch(db, report["id"])


def submit(db: sqlite3.Connection, user_id: int, report_date: date) -> None:
    report = get_or_create_report(db, user_id, report_date)
    now = clock.now_iso()
    db.execute(
        "UPDATE daily_reports SET submitted_at = ?, updated_at = ?, has_unsubmitted_changes = 0 WHERE id = ?",
        (now, now, report["id"]),
    )


def mark_reports_changed_for_task(db: sqlite3.Connection, task_id: int) -> None:
    """タスクを削除する前に呼ぶ。そのタスクが入っている日報を「変更あり」にする(提出済みなら「修正あり」になる)。"""
    report_ids = [row["report_id"] for row in db.execute(
        "SELECT report_id FROM daily_report_items WHERE task_id = ?", (task_id,))]
    for report_id in report_ids:
        _touch(db, report_id)


def report_status(report: sqlite3.Row | None) -> str:
    if report is None or report["submitted_at"] is None:
        return "draft"
    return "modified" if report["has_unsubmitted_changes"] else "submitted"


def build_report_view(db: sqlite3.Connection, user_id: int, report_date: date) -> dict:
    """日報画面の表示内容(タスクごとの時間・合計・基本勤務時間との差など)。"""
    report = find_report(db, user_id, report_date)
    item_rows = []
    if report is not None:
        item_rows = db.execute(
            "SELECT t.*, i.minutes AS report_minutes FROM daily_report_items i "
            "JOIN tasks t ON t.id = i.task_id WHERE i.report_id = ?",
                        (report["id"],),
        ).fetchall()
    items = tasks.enrich(db, item_rows)
    today_iso = clock.today().isoformat()
    items.sort(key=lambda task: (task["status"] != "done", *tasks.sort_key(task, today_iso)))

    total_minutes = sum(item["report_minutes"] for item in items)
    status = report_status(report)
    return {
        "report_date": report_date,
        "task_items": items,
        "work_style": report["work_style"] if report else "office",
        "leave_types": parse_leave_types(report["leave_types"]) if report else [],
        "comment": report["comment"] if report else "",
        # 初めて開く日(レコード未作成)はON(1)から始める
        "time_tracking_enabled": bool(report["time_tracking_enabled"]) if report else True,
        "details_enabled": bool(report["details_enabled"]) if report else True,
        "submitted_at": report["submitted_at"] if report else None,
        "updated_at": report["updated_at"] if report else None,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "total_minutes": total_minutes,
        "base_minutes": BASE_WORK_MINUTES,
        "overtime_minutes": total_minutes - BASE_WORK_MINUTES,
    }


def list_recent_reports(db: sqlite3.Connection, user_id: int, limit: int = 14) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM daily_reports WHERE user_id = ? ORDER BY report_date DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    return [
        {"report_date": date.fromisoformat(row["report_date"]), "status": report_status(row),
         "status_label": STATUS_LABELS[report_status(row)]}
        for row in rows
    ]
