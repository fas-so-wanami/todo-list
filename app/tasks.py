"""タスクのCRUD・状態遷移・繰り返し・タグ。"""
import calendar
import re
import sqlite3
from datetime import date, timedelta

from app import clock

STATUSES = ("todo", "doing", "done")
STATUS_LABELS = {"todo": "未着手", "doing": "進行中", "done": "完了"}
PROGRESS_STEPS = (0, 25, 50, 75, 100)
DEFAULT_DOING_PROGRESS = 25  # 「開始した」だけの報告などで進行中にするときの進捗率
LEVEL_LABELS = {1: "低", 2: "中", 3: "高"}
RECURRENCE_LABELS = {"daily": "毎日", "weekdays": "平日", "weekly": "毎週", "monthly": "毎月"}
LONG_RUNNING_MINUTES = 5 * 60  # 同一タスクの累計がこれ以上なら自動ハイライト

EDITABLE_FIELDS = {"title", "importance", "urgency", "due_date", "start_date", "memo", "recurrence_rule"}
RESTORABLE_FIELDS = EDITABLE_FIELDS | {"status", "progress", "started_at", "completed_at", "waiting_since", "waiting_note"}
MAX_NOTE_LENGTH = 200  # 相談の理由・待ちの一言の上限
TAG_PATTERN = re.compile(r"#(\S+)")


def split_title_and_tags(text: str) -> tuple[str, list[str]]:
    """「資料作成 #案件A」のように入力された #タグ を取り出す。"""
    tag_names = [name[:MAX_TAG_LENGTH] for name in dict.fromkeys(TAG_PATTERN.findall(text))][:MAX_TAGS_PER_TASK]
    title = TAG_PATTERN.sub("", text).strip()
    return re.sub(r"\s+", " ", title), tag_names


def parse_tag_names(text: str) -> list[str]:
    names = [name.strip().lstrip("#")[:MAX_TAG_LENGTH] for name in re.split(r"[,、，\s]+", text or "")]
    return [name for name in dict.fromkeys(names) if name][:MAX_TAGS_PER_TASK]


def clamp_level(value: int | str | None, default: int = 2) -> int:
    try:
        return min(3, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_due_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


class TaskDateError(ValueError):
    """タスクの開始日と期限の入力を理由付きで断るためのエラー。"""


def normalize_start_date(value: str | None) -> str | None:
    """開始日を YYYY-MM-DD にそろえ、未入力は空のまま扱う。"""
    if not value:
        return None
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise TaskDateError("開始日は YYYY-MM-DD の日付を入力してください。") from error
    if value != normalized:
        raise TaskDateError("開始日は YYYY-MM-DD の日付を入力してください。")
    return normalized


def validate_start_date(start_date: str | None, due_date: str | None) -> str | None:
    """開始日を検証し、期限より後の日付を登録しない。"""
    normalized_start_date = normalize_start_date(start_date)
    normalized_due_date = normalize_due_date(due_date)
    if normalized_start_date and normalized_due_date and normalized_start_date > normalized_due_date:
        raise TaskDateError("開始日は期限以前の日付にしてください。")
    return normalized_start_date


def normalize_recurrence(value: str | None) -> str | None:
    return value if value in RECURRENCE_LABELS else None


MAX_SQLITE_INTEGER = 2 ** 63 - 1
MAX_TAG_LENGTH = 50
MAX_TAGS_PER_TASK = 20
MAX_MEMO_LENGTH = 2000


def is_valid_task_id(task_id: int) -> bool:
    """SQLite の整数に収まる正の番号か(細工した巨大な番号でエラーにしない)。"""
    return isinstance(task_id, int) and 0 < task_id <= MAX_SQLITE_INTEGER


def get_task(db: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    if not is_valid_task_id(task_id):
        return None
    return db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_owned_task(db: sqlite3.Connection, user_id: int, task_id: int) -> sqlite3.Row | None:
    """本人のタスクだけを返す。他の人のタスクや存在しない番号なら None(操作の入口で必ず使う)。"""
    task = get_task(db, task_id)
    return task if task is not None and task["user_id"] == user_id else None


def create_task(
    db: sqlite3.Connection,
    user_id: int,
    title: str,
    *,
    parent_id: int | None = None,
    importance: int = 2,
    urgency: int = 2,
    due_date: str | None = None,
    start_date: str | None = None,
    memo: str = "",
    tag_names: list[str] | None = None,
    recurrence_rule: str | None = None,
) -> int:
    if parent_id is not None:
        parent = get_owned_task(db, user_id, parent_id)
        if parent is None or parent["parent_id"] is not None:
            raise TaskRelationshipError("子を追加できる親タスクが見つかりません。親子は1段だけにできます。")
        if recurrence_rule:
            raise TaskRelationshipError("子タスクの繰り返しは設定できません。親側で設定してください。")
    cursor = db.execute(
        """INSERT INTO tasks (user_id, parent_id, title, importance, urgency, due_date, start_date,
                              memo, recurrence_rule, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, parent_id, title.strip(), clamp_level(importance), clamp_level(urgency),
            normalize_due_date(due_date), validate_start_date(start_date, due_date),
            memo, normalize_recurrence(recurrence_rule),
            clock.now_iso(),
        ),
    )
    task_id = cursor.lastrowid
    if task_id is None:
        raise RuntimeError("タスクの作成番号を取得できませんでした")
    if tag_names:
        set_tags(db, task_id, tag_names)
    if parent_id is not None:
        synchronize_parent_status(db, user_id, parent_id)
    return task_id


def update_task(db: sqlite3.Connection, task_id: int, **fields) -> None:
    """EDITABLE_FIELDS のみ更新する(列名をホワイトリストで固定しSQLインジェクションを防ぐ)。"""
    unknown_fields = set(fields) - EDITABLE_FIELDS
    if unknown_fields:
        raise ValueError(f"更新できない項目です: {sorted(unknown_fields)}")
    _write_fields(db, task_id, fields)


class TaskRelationshipError(ValueError):
    """親子関係や繰り返しの変更を、理由付きで断るためのエラー。"""


def list_parent_candidates(db: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """絞り込み中も全案件から親を選べるよう、本人の未完了の親だけを返す。"""
    return db.execute(
        "SELECT id, title FROM tasks WHERE user_id = ? AND parent_id IS NULL "
        "AND status != 'done' ORDER BY title, id", (user_id,),
    ).fetchall()


def change_parent(
    db: sqlite3.Connection, user_id: int, task_id: int, parent_id: int | None,
    *, confirm_clear_recurrence: bool = False,
) -> bool:
    """既存タスクを結び付け・解除し、両側の件数と状態を更新する。記録は移動・削除しない。"""
    task = get_owned_task(db, user_id, task_id)
    if task is None:
        raise TaskRelationshipError("操作できるタスクが見つかりません。")
    previous_parent_id = task["parent_id"]
    if previous_parent_id == parent_id:
        return False
    if parent_id is not None:
        if previous_parent_id is not None:
            raise TaskRelationshipError("別の親へ移す場合は、いったん親から外してください。")
        parent = get_owned_task(db, user_id, parent_id)
        if parent_id == task_id:
            raise TaskRelationshipError("自分自身を親にはできません。")
        if parent is None or parent["parent_id"] is not None or parent["status"] == "done":
            raise TaskRelationshipError("親には、本人の未完了の親タスクだけを選べます。")
        if db.execute(
            "SELECT 1 FROM tasks WHERE user_id = ? AND parent_id = ?", (user_id, task_id)
        ).fetchone():
            raise TaskRelationshipError("子を持つタスクを別の親の子にはできません。親子は1段だけです。")
        if task["recurrence_rule"] and not confirm_clear_recurrence:
            raise TaskRelationshipError("子にする前に、このタスクの繰り返しを外すことを確認してください。")
        _write_fields(db, task_id, {"parent_id": parent_id, "recurrence_rule": None})
    else:
        _write_fields(db, task_id, {"parent_id": None})
    if previous_parent_id is not None:
        synchronize_parent_status(db, user_id, previous_parent_id)
    if parent_id is not None:
        synchronize_parent_status(db, user_id, parent_id)
    return True


def set_recurrence_rule(
    db: sqlite3.Connection, user_id: int, task_id: int, rule: str | None,
    *, confirm_clear_recurrence: bool = False,
) -> None:
    """繰り返しを親側だけで管理する。既存の子の設定は、確認なしでは外さない。"""
    task = get_owned_task(db, user_id, task_id)
    if task is None:
        raise TaskRelationshipError("操作できるタスクが見つかりません。")
    if rule is not None and rule not in RECURRENCE_LABELS:
        raise TaskRelationshipError("指定された繰り返しは設定できません。")
    if task["parent_id"] is not None:
        if (rule or task["recurrence_rule"]) and not confirm_clear_recurrence:
            raise TaskRelationshipError("子の繰り返し設定を外すことを確認してください。繰り返しは親側で管理します。")
        rule = None
    recurring_children = db.execute(
        "SELECT id FROM tasks WHERE user_id = ? AND parent_id = ? AND recurrence_rule IS NOT NULL",
        (user_id, task_id),
    ).fetchall()
    if recurring_children and not confirm_clear_recurrence:
        raise TaskRelationshipError("子の個別の繰り返し設定が残っています。外すことを確認してから保存してください。")
    if recurring_children:
        db.execute(
            "UPDATE tasks SET recurrence_rule = NULL WHERE user_id = ? AND parent_id = ?",
            (user_id, task_id),
        )
    _write_fields(db, task_id, {"recurrence_rule": rule})


def restore_fields(db: sqlite3.Connection, task_id: int, fields: dict) -> None:
    """Undo用。状態や時刻を含めて元の値に戻す。"""
    unknown_fields = set(fields) - RESTORABLE_FIELDS
    if unknown_fields:
        raise ValueError(f"復元できない項目です: {sorted(unknown_fields)}")
    _write_fields(db, task_id, fields)


def _write_fields(db: sqlite3.Connection, task_id: int, fields: dict) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{column} = ?" for column in fields)
    db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id))


def delete_task(db: sqlite3.Connection, task_id: int) -> None:
    task = get_task(db, task_id)
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    if task is not None and task["parent_id"] is not None:
        synchronize_parent_status(db, task["user_id"], task["parent_id"])


def set_tags(db: sqlite3.Connection, task_id: int, tag_names: list[str]) -> None:
    task = get_task(db, task_id)
    db.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
    for tag_name in tag_names:
        db.execute(
            "INSERT OR IGNORE INTO tags (user_id, name) VALUES (?, ?)", (task["user_id"], tag_name)
        )
        tag_id = db.execute(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?", (task["user_id"], tag_name)
        ).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?, ?)", (task_id, tag_id))


def list_tag_names(db: sqlite3.Connection, user_id: int) -> list[str]:
    rows = db.execute("SELECT name FROM tags WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()
    return [row["name"] for row in rows]


def toggle_waiting(db: sqlite3.Connection, task_id: int) -> bool:
    """「待ち」(他人の返事待ちなど、自分では進められない状態)を切り替え、切り替え後に待ちなら True。
    困りごととは別扱いで、上司に知らせる目印にはしない。"""
    task = get_task(db, task_id)
    if task is None:
        return False
    waiting_since = None if task["waiting_since"] else clock.now_iso()
    # 待ちを解除したら、何を待っていたかの一言も消す(次に待ちにしたとき古い内容が残らないように)
    _write_fields(db, task_id, {"waiting_since": waiting_since, "waiting_note": ""})
    return waiting_since is not None


def set_waiting_note(db: sqlite3.Connection, task_id: int, note: str) -> None:
    """待っている相手・内容の一言を書く。待ちでないタスクには書かない。"""
    task = get_task(db, task_id)
    if task is None or not task["waiting_since"]:
        return
    _write_fields(db, task_id, {"waiting_note": note.strip()[:MAX_NOTE_LENGTH]})


def status_for_progress(progress: int) -> str:
    if progress <= 0:
        return "todo"
    if progress >= 100:
        return "done"
    return "doing"


def set_status(db: sqlite3.Connection, task_id: int, status: str) -> int | None:
    """状態を変更する。進捗列は旧機能との互換用に残し、画面では使わない。"""
    if status not in STATUSES:
        raise ValueError(f"不正な進捗です: {status}")
    task = get_task(db, task_id)
    if task is None:
        return None
    if status == "todo":
        progress = 0
    elif status == "done":
        progress = 100
    else:
        progress = DEFAULT_DOING_PROGRESS
    return set_progress(db, task_id, progress)


class ParentTaskStatusError(ValueError):
    """親の状態を手動変更して、子との食い違いを生まないようにする。"""


class UnfinishedSubtasksError(ValueError):
    """子タスクが残っている親タスクを完了にしようとしたときのエラー。"""

    def __init__(self, unfinished_count: int):
        super().__init__(f"サブタスクが {unfinished_count} 件残っているため完了にできません")
        self.unfinished_count = unfinished_count


def list_unfinished_subtask_ids(db: sqlite3.Connection, parent_id: int) -> list[int]:
    return [row["id"] for row in db.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND status != 'done' ORDER BY id", (parent_id,))]


def set_progress(db: sqlite3.Connection, task_id: int, progress: int) -> int | None:
    """旧進捗値を受け取る入口。子の変更は親へ反映し、親から子は変更しない。"""
    if progress not in PROGRESS_STEPS:
        raise ValueError(f"不正な進捗率です: {progress}")
    task = get_task(db, task_id)
    if task is None:
        return None

    status = status_for_progress(progress)
    if status == "done" and task["parent_id"] is not None:
        _check_child_recurrences(db, task["user_id"], task["parent_id"])
    children = db.execute(
        "SELECT status FROM tasks WHERE user_id = ? AND parent_id = ?",
        (task["user_id"], task_id),
    ).fetchall()
    if children:
        unfinished_count = sum(child["status"] != "done" for child in children)
        if status == "done" and unfinished_count:
            raise UnfinishedSubtasksError(unfinished_count)
        if status == "done":
            _check_child_recurrences(db, task["user_id"], task_id)
        elif status != "doing" or task["status"] != "done":
            raise ParentTaskStatusError("親の着手状態は子タスクから自動で決まります。子タスクを操作してください。")
    if task["progress"] == progress:
        return None
    status_changed = task["status"] != status
    now = clock.now_iso()
    if status == "todo":
        started_at, completed_at = None, None
    elif status == "doing":
        started_at, completed_at = task["started_at"] or now, None
    else:
        started_at, completed_at = task["started_at"], now
    _write_fields(db, task_id, {
        "status": status, "progress": progress, "started_at": started_at, "completed_at": completed_at,
    })

    if task["parent_id"] is not None:
        synchronize_parent_status(db, task["user_id"], task["parent_id"])
    if status_changed and status == "done" and task["recurrence_rule"]:
        return create_next_occurrence(db, task)
    return None


def synchronize_parent_status(db: sqlite3.Connection, user_id: int, parent_id: int) -> None:
    """親の着手状態を子とそろえる。全件完了しても、親の完了は利用者が選ぶ。"""
    parent = get_owned_task(db, user_id, parent_id)
    if parent is None:
        return None
    children = db.execute(
        "SELECT status FROM tasks WHERE user_id = ? AND parent_id = ?", (user_id, parent_id)
    ).fetchall()
    if not children:
        return None
    if all(child["status"] == "done" for child in children):
        if parent["status"] == "done":
            return None
        status, progress = "doing", DEFAULT_DOING_PROGRESS
    elif all(child["status"] == "todo" for child in children):
        status, progress = "todo", 0
    else:
        status, progress = "doing", DEFAULT_DOING_PROGRESS
    if parent["status"] == status:
        return None
    now = clock.now_iso()
    _write_fields(db, parent_id, {
        "status": status, "progress": progress,
        "started_at": None if status == "todo" else parent["started_at"] or now,
        "completed_at": now if status == "done" else None,
    })
    return None


def parents_ready_for_completion(db: sqlite3.Connection, user_id: int) -> dict[int, str]:
    """新しく全件完了になった親だけを質問できるよう、確認待ちの親を返す。"""
    rows = db.execute(
        """SELECT p.id, p.title FROM tasks p
           WHERE p.user_id = ? AND p.parent_id IS NULL AND p.status != 'done'
             AND EXISTS (SELECT 1 FROM tasks c WHERE c.user_id = p.user_id AND c.parent_id = p.id)
             AND NOT EXISTS (
                 SELECT 1 FROM tasks c WHERE c.user_id = p.user_id AND c.parent_id = p.id AND c.status != 'done'
             ) ORDER BY p.id""", (user_id,),
    ).fetchall()
    return {row["id"]: row["title"] for row in rows}


def initialize_task_states(db: sqlite3.Connection, user_id: int) -> None:
    """起動時に既存の進捗を状態へ引き継ぐ。過去の親の整理では次回分を作らない。"""
    rows = db.execute("SELECT id, progress, status FROM tasks WHERE user_id = ?", (user_id,)).fetchall()
    for task in rows:
        status = status_for_progress(task["progress"])
        if task["status"] != status:
            db.execute("UPDATE tasks SET status = ? WHERE user_id = ? AND id = ?", (status, user_id, task["id"]))
    parent_ids = db.execute(
        "SELECT DISTINCT parent_id FROM tasks WHERE user_id = ? AND parent_id IS NOT NULL", (user_id,)
    ).fetchall()
    for row in parent_ids:
        synchronize_parent_status(db, user_id, row["parent_id"])


def reopen_parent_if_done(db: sqlite3.Connection, parent_id: int | None) -> None:
    """旧機能からの呼び出しを残しつつ、子から親の状態を計算し直す。"""
    if parent_id is None:
        return
    parent = get_task(db, parent_id)
    if parent is not None:
        synchronize_parent_status(db, parent["user_id"], parent_id)


def next_due_date(rule: str, base_date: date) -> date:
    if rule == "daily":
        return base_date + timedelta(days=1)
    if rule == "weekdays":
        next_date = base_date + timedelta(days=1)
        while next_date.weekday() >= 5:
            next_date += timedelta(days=1)
        return next_date
    if rule == "weekly":
        return base_date + timedelta(days=7)
    if rule == "monthly":
        year = base_date.year + base_date.month // 12
        month = base_date.month % 12 + 1
        last_day_of_month = calendar.monthrange(year, month)[1]
        return date(year, month, min(base_date.day, last_day_of_month))
    raise ValueError(f"不正な繰り返しルールです: {rule}")


def create_next_occurrence(db: sqlite3.Connection, completed_task: sqlite3.Row) -> int | None:
    """次回の親子セットを未着手で作り、開始日と期限の日数差を保つ。"""
    occurrence = db.execute(
        "SELECT r.next_task_id FROM task_recurrences r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.user_id = ? AND r.task_id = ?",
        (completed_task["user_id"], completed_task["id"]),
    ).fetchone()
    if occurrence is not None:
        return occurrence["next_task_id"]
    _check_child_recurrences(db, completed_task["user_id"], completed_task["id"])
    children = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND parent_id = ? ORDER BY id",
        (completed_task["user_id"], completed_task["id"]),
    ).fetchall()
    base_date = clock.today()
    if completed_task["due_date"]:
        base_date = max(date.fromisoformat(completed_task["due_date"]), base_date)
    original_date = date.fromisoformat(completed_task["due_date"]) if completed_task["due_date"] else clock.today()
    if not completed_task["due_date"] and completed_task["start_date"]:
        scheduled_start_date = date.fromisoformat(completed_task["start_date"])
        if scheduled_start_date > original_date:
            # 期限のない親の開始日が未来なら、次回期限もその日以降を基準にする。
            original_date = scheduled_start_date
            base_date = scheduled_start_date
    next_date = next_due_date(completed_task["recurrence_rule"], base_date)
    date_shift = next_date - original_date
    next_task_id = None
    for source in [completed_task, *children]:
        is_parent = source["id"] == completed_task["id"]
        due_date = next_date.isoformat() if is_parent else (
            (date.fromisoformat(source["due_date"]) + date_shift).isoformat() if source["due_date"] else None
        )
        start_date = (
            (date.fromisoformat(source["start_date"]) + date_shift).isoformat()
            if source["start_date"] else None
        )
        tag_names = [row["name"] for row in db.execute(
            "SELECT g.name FROM task_tags tt JOIN tags g ON g.id = tt.tag_id "
            "WHERE g.user_id = ? AND tt.task_id = ?", (source["user_id"], source["id"]),
        )]
        created_id = create_task(
            db, source["user_id"], source["title"],
            parent_id=None if is_parent else next_task_id,
            importance=source["importance"], urgency=source["urgency"], due_date=due_date, start_date=start_date,
            memo=source["memo"], tag_names=tag_names,
            recurrence_rule=completed_task["recurrence_rule"] if is_parent else None,
        )
        if is_parent:
            next_task_id = created_id
    db.execute(
        "INSERT INTO task_recurrences (task_id, next_task_id) VALUES (?, ?)",
        (completed_task["id"], next_task_id),
    )
    return next_task_id


def _check_child_recurrences(db: sqlite3.Connection, user_id: int, parent_id: int) -> None:
    """旧設定があれば完了前に知らせ、子の次回分を二重に作らない。"""
    if db.execute(
        "SELECT 1 FROM tasks WHERE user_id = ? AND parent_id = ? AND recurrence_rule IS NOT NULL",
        (user_id, parent_id),
    ).fetchone():
        raise TaskRelationshipError("子の個別の繰り返し設定が残っています。親の「編集」で外すことを確認して保存してください。")


def sort_key(task: dict, today_iso: str) -> tuple:
    """期限切れ/当日を先頭に、重要度+緊急性が高い順、期限が近い順。"""
    is_due = task["due_date"] is not None and task["due_date"] <= today_iso
    return (0 if is_due else 1, -(task["importance"] + task["urgency"]), -task["importance"],
            task["due_date"] or "9999-12-31", task["id"])


def enrich(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    """一覧表示に必要な付随情報(タグ・困りごと・累計時間・サブタスク数など)を付与する。"""
    tasks = [dict(row) for row in rows]
    if not tasks:
        return tasks
    task_ids = [task["id"] for task in tasks]
    placeholders = ",".join("?" * len(task_ids))
    today_iso = clock.today().isoformat()

    tags_by_task: dict[int, list[str]] = {}
    for row in db.execute(
        f"SELECT tt.task_id, g.name FROM task_tags tt JOIN tags g ON g.id = tt.tag_id "
        f"WHERE tt.task_id IN ({placeholders}) ORDER BY g.name", task_ids):
        tags_by_task.setdefault(row["task_id"], []).append(row["name"])

    # 未解決の困りごと(相談)と、その理由。上司画面で一番見たい情報なので行にも出す
    blocker_notes = {row["task_id"]: row["note"] for row in db.execute(
        f"SELECT task_id, note FROM blockers WHERE resolved_at IS NULL "
        f"AND task_id IN ({placeholders}) ORDER BY id", task_ids)}
    blocked_ids = set(blocker_notes)
    minutes_by_task = {row["task_id"]: row["total"] for row in db.execute(
        f"SELECT task_id, SUM(minutes) AS total FROM time_logs "
        f"WHERE task_id IN ({placeholders}) GROUP BY task_id", task_ids)}
    subtask_counts = {row["parent_id"]: row for row in db.execute(
        f"SELECT c.parent_id, COUNT(*) AS total, SUM(c.status = 'done') AS done, "
        f"SUM(c.recurrence_rule IS NOT NULL) AS recurring "
        f"FROM tasks c JOIN tasks p ON p.id = c.parent_id AND p.user_id = c.user_id "
        f"WHERE c.parent_id IN ({placeholders}) GROUP BY c.parent_id", task_ids)}
    planned_ids = {row["task_id"] for row in db.execute(
        f"SELECT task_id FROM daily_plans WHERE plan_date = ? AND task_id IN ({placeholders})",
        [today_iso, *task_ids])}

    parent_ids = sorted({task["parent_id"] for task in tasks if task["parent_id"]})
    parent_titles = {}
    if parent_ids:
        parent_titles = {row["id"]: row["title"] for row in db.execute(
            f"SELECT id, title FROM tasks WHERE id IN ({','.join('?' * len(parent_ids))})", parent_ids)}

    for task in tasks:
        total_minutes = minutes_by_task.get(task["id"], 0) or 0
        subtask_count = subtask_counts.get(task["id"])
        task["tags"] = tags_by_task.get(task["id"], [])
        task["has_open_blocker"] = task["id"] in blocked_ids
        task["blocker_note"] = blocker_notes.get(task["id"], "")
        task["total_minutes"] = total_minutes
        task["is_long_running"] = total_minutes >= LONG_RUNNING_MINUTES
        task["subtask_total"] = subtask_count["total"] if subtask_count else 0
        task["subtask_done"] = (subtask_count["done"] or 0) if subtask_count else 0
        task["recurring_child_count"] = subtask_count["recurring"] if subtask_count else 0
        task["is_planned_today"] = task["id"] in planned_ids
        task["is_overdue"] = (
            task["status"] != "done" and task["due_date"] is not None and task["due_date"] < today_iso
        )
        task["is_due_today"] = task["status"] != "done" and task["due_date"] == today_iso
        task["parent_title"] = parent_titles.get(task["parent_id"])
        task["is_waiting"] = task["waiting_since"] is not None
        task["tag_text"] = " ".join(task["tags"])
    return tasks


def arrange_as_tree(task_list: list[dict], root_sort_key, child_sort_key) -> list[dict]:
    """サブタスクを親の直後に並べ替える。各要素に depth(0=親, 1=子)と is_last_child を付ける。
    親が一覧に含まれていない子タスクは、単独の行(depth=0)として扱う。"""
    present_ids = {task["id"] for task in task_list}
    tasks_by_parent: dict[int | None, list[dict]] = {}
    for task in task_list:
        parent_key = task["parent_id"] if task["parent_id"] in present_ids else None
        tasks_by_parent.setdefault(parent_key, []).append(task)

    ordered: list[dict] = []

    def append_with_children(task: dict, depth: int) -> None:
        task["depth"] = depth
        ordered.append(task)
        children = sorted(tasks_by_parent.get(task["id"], []), key=child_sort_key)
        for index, child in enumerate(children):
            child["is_last_child"] = index == len(children) - 1
            append_with_children(child, depth + 1)

    for root in sorted(tasks_by_parent.get(None, []), key=root_sort_key):
        append_with_children(root, 0)
    return ordered


def list_tasks_for_tree(
    db: sqlite3.Connection, user_id: int, tag_name: str | None = None, show_done: bool = False
) -> list[dict]:
    """親タスクの順に、サブタスクを直後へ並べた一覧を返す(各要素に depth を付与)。
    今日完了したタスクは「完了も表示」がオフでも残し、その場で取り消し線表示にする。"""
    today_iso = clock.today().isoformat()
    conditions, params = ["t.user_id = ?"], [user_id]
    if not show_done:
        conditions.append("(t.status != 'done' OR substr(t.completed_at, 1, 10) = ?)")
        params.append(today_iso)
    if tag_name:
        conditions.append(
            "t.id IN (SELECT tt.task_id FROM task_tags tt JOIN tags g ON g.id = tt.tag_id "
            "WHERE g.name = ? AND g.user_id = t.user_id)"
        )
        params.append(tag_name)
    rows = db.execute(f"SELECT t.* FROM tasks t WHERE {' AND '.join(conditions)}", params).fetchall()
    tasks = enrich(db, rows)

    def is_done_before_today(task: dict) -> bool:
        return task["status"] == "done" and (task["completed_at"] or "")[:10] != today_iso

    return arrange_as_tree(
        tasks,
        root_sort_key=lambda task: (is_done_before_today(task), *sort_key(task, today_iso)),
        child_sort_key=lambda child: (is_done_before_today(child), child["id"]),
    )
