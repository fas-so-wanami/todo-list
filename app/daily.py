"""今日やるタスクの選び方・タスク画面の組み立て・作業時間・アーカイブ。

タスク画面は「今日やる」と「あとでやる」の2段に分ける。
- 今日やる: 今日の予定に入れたもの / 期限が今日以前 / 進行中 / 今日完了したもの
- あとでやる: それ以外の未完了。焦らず目の前に集中できるよう、画面では半透明・折りたたみで控えめに出す
昨日以前に完了したものはアーカイブへ移す。ただし子タスクは親が完了するまで親の下に残す。
"""
import sqlite3
from datetime import date

from app import clock, tasks


# ---- 今日の予定 ----

def ensure_today_plan(db: sqlite3.Connection, user_id: int) -> None:
    """その日最初の表示時に、直前の予定日の未完了タスクを今日の予定へ持ち越す(1日1回だけ)。
    持ち越した後に「あとでやる」を押したものが、再表示で戻ってこないよう記録を残す。"""
    today_iso = clock.today().isoformat()
    already_rolled_over = db.execute(
        "SELECT 1 FROM plan_rollovers WHERE user_id = ? AND plan_date = ?", (user_id, today_iso)
    ).fetchone()
    if already_rolled_over:
        return
    # 持ち越し元は「最後に使った日」(持ち越しの記録がある最後の日)。
    # 「最後に予定が残っていた日」にすると、その日に全部「明日以降に」移した場合に、さらに前の日の予定が復活してしまう。
    last_used_date = db.execute(
        "SELECT MAX(plan_date) AS plan_date FROM plan_rollovers WHERE user_id = ? AND plan_date < ?",
        (user_id, today_iso),
    ).fetchone()["plan_date"]
    if last_used_date:
        last_plan_date = last_used_date
    else:  # 持ち越しの記録が無い古いデータでは、最後に予定があった日から持ち越す
        last_plan_date = db.execute(
            """SELECT MAX(dp.plan_date) AS plan_date FROM daily_plans dp
               JOIN tasks t ON t.id = dp.task_id
               WHERE t.user_id = ? AND dp.plan_date < ?""",
            (user_id, today_iso),
        ).fetchone()["plan_date"]
    if last_plan_date:
        db.execute(
            """INSERT OR IGNORE INTO daily_plans (plan_date, task_id, sort_order)
               SELECT ?, dp.task_id, dp.sort_order FROM daily_plans dp
               JOIN tasks t ON t.id = dp.task_id
               WHERE dp.plan_date = ? AND t.user_id = ? AND t.status != 'done'""",
            (today_iso, last_plan_date, user_id),
        )
    db.execute("INSERT INTO plan_rollovers (user_id, plan_date) VALUES (?, ?)", (user_id, today_iso))


def list_candidates(db: sqlite3.Connection, user_id: int) -> list[dict]:
    """今日やる未完了タスク: 今日の予定 / 期限が今日以前 / 進行中。ただし今日「あとでやる」に移したものは除く。"""
    ensure_today_plan(db, user_id)
    today_iso = clock.today().isoformat()
    rows = db.execute(
        """SELECT t.* FROM tasks t
           WHERE t.user_id = :user_id AND t.status != 'done' AND (
                 t.id IN (SELECT task_id FROM daily_plans WHERE plan_date = :today)
              OR (t.due_date IS NOT NULL AND t.due_date <= :today)
              OR t.status = 'doing')
             AND t.id NOT IN (SELECT task_id FROM plan_deferrals WHERE plan_date = :today)""",
        {"user_id": user_id, "today": today_iso},
    ).fetchall()
    candidates = tasks.enrich(db, rows)
    candidates.sort(key=lambda task: tasks.sort_key(task, today_iso))
    return candidates


def list_done_today(db: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' AND substr(completed_at, 1, 10) = ? "
        "ORDER BY completed_at",
        (user_id, clock.today().isoformat()),
    ).fetchall()
    return tasks.enrich(db, rows)


def list_unfinished_tasks(db: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status != 'done'", (user_id,)
    ).fetchall()
    unfinished = tasks.enrich(db, rows)
    unfinished.sort(key=lambda task: tasks.sort_key(task, clock.today().isoformat()))
    return unfinished


def merge_today_tasks(candidates: list[dict], done_today: list[dict]) -> list[dict]:
    """今日の候補と今日完了したものを1つの一覧にし、サブタスクは親の直下に並べる。
    完了しても並び順が変わらないよう、進捗は並び替えに使わない。"""
    today_iso = clock.today().isoformat()
    return tasks.arrange_as_tree(
        candidates + done_today,
        root_sort_key=lambda task: tasks.sort_key(task, today_iso),
        child_sort_key=lambda child: child["id"],
    )


def calculate_progress(done_count: int, remaining_count: int) -> dict:
    total = done_count + remaining_count
    percent = round(done_count * 100 / total) if total else 0
    return {"done": done_count, "total": total, "percent": percent}


def add_to_plan(db: sqlite3.Connection, task_id: int, plan_date: date) -> None:
    db.execute(
        "INSERT OR IGNORE INTO daily_plans (plan_date, task_id, sort_order) VALUES (?, ?, 0)",
        (plan_date.isoformat(), task_id),
    )


def remove_from_plan(db: sqlite3.Connection, task_id: int, plan_date: date) -> None:
    db.execute(
        "DELETE FROM daily_plans WHERE plan_date = ? AND task_id = ?", (plan_date.isoformat(), task_id)
    )


def _with_unfinished_subtasks(db: sqlite3.Connection, task_id: int) -> list[int]:
    """段の移動は親子まとめて行う(子だけ別の段に残ると、親の下から子が消えたように見えるため)。"""
    return [task_id, *tasks.list_unfinished_subtask_ids(db, task_id)]


def move_to_later(db: sqlite3.Connection, task_id: int) -> None:
    """「明日以降にやる」へ移す。期限切れ・進行中で自動的に今日やるに入るものも、今日だけは下の段に置く(翌日は戻る)。"""
    today = clock.today()
    for target_id in _with_unfinished_subtasks(db, task_id):
        remove_from_plan(db, target_id, today)
        db.execute(
            "INSERT OR IGNORE INTO plan_deferrals (plan_date, task_id) VALUES (?, ?)", (today.isoformat(), target_id)
        )


def move_to_today(db: sqlite3.Connection, task_id: int) -> None:
    today = clock.today()
    for target_id in _with_unfinished_subtasks(db, task_id):
        db.execute("DELETE FROM plan_deferrals WHERE plan_date = ? AND task_id = ?", (today.isoformat(), target_id))
    add_to_plan(db, task_id, today)


def is_in_today(db: sqlite3.Connection, user_id: int, task_id: int) -> bool:
    """タスク画面で「今日やる」の段に出ているか。子が今日やるなら親も今日やるに出る。"""
    candidate_ids = {task["id"] for task in list_candidates(db, user_id)}
    return any(target_id in candidate_ids for target_id in _with_unfinished_subtasks(db, task_id))


def task_board_section(db: sqlite3.Connection, user_id: int, task_id: int) -> str:
    """結び付けで段が変わっても見失わないよう、実際の親子の表示先を返す。"""
    board = build_board(db, user_id, set())
    if any(task["id"] == task_id for task in board["today"]):
        return "今日やる"
    if any(task["id"] == task_id for task in board["others"]):
        return "明日以降にやる"
    return "アーカイブ"


def toggle_today_plan(db: sqlite3.Connection, user_id: int, task_id: int) -> None:
    """「今日やる」にあれば「明日以降にやる」へ、なければ「今日やる」へ移す(親子まとめて)。"""
    if is_in_today(db, user_id, task_id):
        move_to_later(db, task_id)
    else:
        move_to_today(db, task_id)


# ---- タスク画面 ----

def _lingering_done_subtasks(db: sqlite3.Connection, user_id: int, today_iso: str) -> list[dict]:
    """親が未完了、または今日完了した場合、過去に完了した子も親の下に残す。"""
    rows = db.execute(
        """SELECT c.* FROM tasks c JOIN tasks p ON p.id = c.parent_id
           WHERE c.user_id = ? AND c.status = 'done' AND substr(c.completed_at, 1, 10) < ?
             AND p.user_id = c.user_id
             AND (p.status != 'done' OR substr(p.completed_at, 1, 10) = ?)""",
        (user_id, today_iso, today_iso),
    ).fetchall()
    return tasks.enrich(db, rows)


def build_board(
    db: sqlite3.Connection, user_id: int, report_task_ids: set[int], tag_name: str | None = None
) -> dict:
    """タスク画面の「今日やる」「明日以降にやる」の2段を組み立てる。待ちのタスクは各段の後ろに回す。
    親子は必ず同じ段に置く。未完了の子が1つでも今日やるなら、親とその子をまとめて今日やるに出す。"""
    today_iso = clock.today().isoformat()
    candidates = list_candidates(db, user_id)
    done_today = list_done_today(db, user_id)
    candidate_ids = {task["id"] for task in candidates}
    done_today_ids = {task["id"] for task in done_today}

    tasks_by_id: dict[int, dict] = {}
    for task in [*candidates, *done_today, *list_unfinished_tasks(db, user_id),
                 *_lingering_done_subtasks(db, user_id, today_iso)]:
        tasks_by_id.setdefault(task["id"], task)

    def family_root_id(task: dict) -> int:
        return task["parent_id"] if task["parent_id"] in tasks_by_id else task["id"]

    today_root_ids = {
        family_root_id(task) for task in tasks_by_id.values()
        if task["id"] in candidate_ids or (task["id"] in done_today_ids and family_root_id(task) == task["id"])
    }
    today_tasks = [task for task in tasks_by_id.values() if family_root_id(task) in today_root_ids]
    other_tasks = [task for task in tasks_by_id.values() if family_root_id(task) not in today_root_ids]

    if tag_name:
        today_tasks = [task for task in today_tasks if tag_name in task["tags"]]
        other_tasks = [task for task in other_tasks if tag_name in task["tags"]]

    for task in today_tasks + other_tasks:
        task["is_in_report"] = task["id"] in report_task_ids
        task["completed_date"] = (task["completed_at"] or "")[:10] or None

    def today_sort_key(task: dict) -> tuple:
        return (task["is_waiting"], *tasks.sort_key(task, today_iso))

    def other_sort_key(task: dict) -> tuple:
        """明日以降にやるは期限が近い順。期限なしは最後に、重要度+緊急性の順で並べる。"""
        return (task["is_waiting"], task["due_date"] or "9999-12-31",
                -(task["importance"] + task["urgency"]), task["id"])

    # 当日完了した全タスクをフラットに並べる(親子関係なく、完了時刻順)。
    # today_tasks の中だけでなく list_done_today の全件を使うことで、
    # 「明日以降にやる」の段にいた子タスクが完了した場合も漏れなく拾う。
    done_today_flat = sorted(
        done_today,
        key=lambda task: task["completed_at"],
    )
    for task in done_today_flat:
        task["is_in_report"] = task["id"] in report_task_ids
        task["completed_date"] = (task["completed_at"] or "")[:10] or None

    return {
        "today": tasks.arrange_as_tree(today_tasks, today_sort_key, lambda child: child["id"]),
        "others": tasks.arrange_as_tree(other_tasks, other_sort_key, lambda child: child["id"]),
        "done_today_count": len(done_today_flat),
        "done_today": done_today_flat,
    }


# ---- アーカイブ ----

def list_archive(db: sqlite3.Connection, user_id: int, tag_name: str | None = None) -> list[dict]:
    """昨日以前に完了したタスクを完了日ごとにまとめる。子タスクは親の完了日のところに親と並べる。"""
    today_iso = clock.today().isoformat()
    rows = db.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' AND substr(completed_at, 1, 10) < ?",
        (user_id, today_iso),
    ).fetchall()
    archived = tasks.enrich(db, rows)
    if tag_name:
        archived = [task for task in archived if tag_name in task["tags"]]
    archived_by_id = {task["id"]: task for task in archived}
    lingering_ids = {task["id"] for task in _lingering_done_subtasks(db, user_id, today_iso)}

    groups: dict[str, list[dict]] = {}
    for task in archived:
        if task["id"] in lingering_ids:
            continue
        task["completed_date"] = task["completed_at"][:10]
        parent = archived_by_id.get(task["parent_id"])
        group_date = parent["completed_at"][:10] if parent else task["completed_date"]
        groups.setdefault(group_date, []).append(task)

    return [
        {
            "date": date.fromisoformat(group_date),
            "tasks": tasks.arrange_as_tree(
                group_tasks,
                root_sort_key=lambda task: task["completed_at"],
                child_sort_key=lambda child: child["completed_at"],
            ),
        }
        for group_date, group_tasks in sorted(groups.items(), reverse=True)
    ]


# ---- 作業時間 ----

def get_minutes(db: sqlite3.Connection, task_id: int, work_date: date) -> int | None:
    row = db.execute(
        "SELECT minutes FROM time_logs WHERE task_id = ? AND work_date = ?",
        (task_id, work_date.isoformat()),
    ).fetchone()
    return row["minutes"] if row else None


def set_minutes(
    db: sqlite3.Connection, task_id: int, work_date: date, minutes: int, source: str = "manual"
) -> None:
    """その日の作業時間の合計を設定する。0以下なら記録を消す。"""
    if minutes <= 0:
        db.execute(
            "DELETE FROM time_logs WHERE task_id = ? AND work_date = ?",
            (task_id, work_date.isoformat()),
        )
        return
    db.execute(
        """INSERT INTO time_logs (task_id, work_date, minutes, source) VALUES (?, ?, ?, ?)
           ON CONFLICT (task_id, work_date) DO UPDATE SET minutes = excluded.minutes, source = excluded.source""",
        (task_id, work_date.isoformat(), minutes, source),
    )


def add_minutes(
    db: sqlite3.Connection, task_id: int, work_date: date, minutes: int, source: str = "ai"
) -> int | None:
    """作業時間を加算し、加算前の値(記録なしなら None)を返す。"""
    previous_minutes = get_minutes(db, task_id, work_date)
    set_minutes(db, task_id, work_date, (previous_minutes or 0) + minutes, source)
    return previous_minutes


def list_reflection_tasks(db: sqlite3.Connection, user_id: int) -> list[dict]:
    """今日の作業の集計対象: 今日の候補・今日完了したもの・今日の作業時間が入っているもの。
    (一時停止中の Copilot 連携の「今日のまとめ」で使用)"""
    today = clock.today()
    reflection_tasks = {task["id"]: task for task in list_candidates(db, user_id)}
    for task in list_done_today(db, user_id):
        reflection_tasks.setdefault(task["id"], task)
    logged_rows = db.execute(
        "SELECT t.* FROM tasks t JOIN time_logs l ON l.task_id = t.id "
        "WHERE t.user_id = ? AND l.work_date = ?",
        (user_id, today.isoformat()),
    ).fetchall()
    for task in tasks.enrich(db, logged_rows):
        reflection_tasks.setdefault(task["id"], task)
    result = list(reflection_tasks.values())
    for task in result:
        task["today_minutes"] = get_minutes(db, task["id"], today)
    result.sort(key=lambda task: (task["status"] == "done", *tasks.sort_key(task, today.isoformat())))
    return result
