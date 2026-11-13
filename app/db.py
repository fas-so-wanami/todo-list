"""SQLite接続とスキーマ定義。"""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "todo.db"
DEFAULT_USER_ID = 1  # MVPは固定1ユーザー。user_idは将来の複数ユーザー対応用。
DEFAULT_USER_NAME = "自分"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user_status ON projects(user_id, status);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id),
    title TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 2,
    urgency INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress IN (0, 25, 50, 75, 100)),
    due_date TEXT,
    start_date TEXT,
    memo TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    recurrence_rule TEXT,
    waiting_since TEXT,  -- 「待ち」(他人の返事待ちなど)にした日時。待ちでなければ NULL
    waiting_note TEXT NOT NULL DEFAULT '',  -- 誰の・何の返事を待っているか(本人用のメモ。上司には出さない)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_recurrences (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    next_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS task_tags (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);

CREATE TABLE IF NOT EXISTS time_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'ai')),
    UNIQUE (task_id, work_date)
);

CREATE TABLE IF NOT EXISTS blockers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    raised_at TEXT NOT NULL,
    resolved_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto', 'ai')),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_plans (
    plan_date TEXT NOT NULL,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_date, task_id)
);

CREATE TABLE IF NOT EXISTS daily_reflections (
    user_id INTEGER NOT NULL REFERENCES users(id),
    reflection_date TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (user_id, reflection_date)
);

-- 前日の予定を今日へ持ち越した記録(1日1回だけ持ち越すため)
CREATE TABLE IF NOT EXISTS plan_rollovers (
    user_id INTEGER NOT NULL REFERENCES users(id),
    plan_date TEXT NOT NULL,
    PRIMARY KEY (user_id, plan_date)
);

-- 「あとでやる」に移した記録。期限切れ・進行中で自動的に「今日やる」に入るタスクを、その日だけ下の段に置くため
CREATE TABLE IF NOT EXISTS plan_deferrals (
    plan_date TEXT NOT NULL,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (plan_date, task_id)
);

-- 日報。1人1日1件。下書き(draft)のまま自動保存し、提出(submitted)後も修正できる。
CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    report_date TEXT NOT NULL,
    work_style TEXT NOT NULL DEFAULT 'office' CHECK (work_style IN ('office', 'telework')),
    leave_types TEXT NOT NULL DEFAULT '',  -- 早退・時間休など。カンマ区切り
    comment TEXT NOT NULL DEFAULT '',
    -- 作業時間、勤務形態・休暇の入力欄を出すか(任意入力のON/OFF)。初めて開く日はON。
    time_tracking_enabled INTEGER NOT NULL DEFAULT 1 CHECK (time_tracking_enabled IN (0, 1)),
    details_enabled INTEGER NOT NULL DEFAULT 1 CHECK (details_enabled IN (0, 1)),
    submitted_at TEXT,  -- 最後に提出した日時。未提出なら NULL
    has_unsubmitted_changes INTEGER NOT NULL DEFAULT 0,  -- 提出後に修正して、まだ再提出していないなら 1
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, report_date)
);

CREATE TABLE IF NOT EXISTS daily_report_items (
    report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (report_id, task_id)
);

CREATE TABLE IF NOT EXISTS daily_report_exclusions (
    user_id INTEGER NOT NULL REFERENCES users(id),
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    report_date TEXT NOT NULL,
    PRIMARY KEY (user_id, task_id, report_date)
);

CREATE TABLE IF NOT EXISTS daily_report_snapshots (
    report_id INTEGER PRIMARY KEY REFERENCES daily_reports(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    report_date TEXT NOT NULL,
    work_style TEXT NOT NULL,
    leave_types TEXT NOT NULL,
    comment TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    UNIQUE (user_id, report_date)
);

CREATE TABLE IF NOT EXISTS daily_report_snapshot_items (
    report_id INTEGER NOT NULL REFERENCES daily_report_snapshots(report_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    source_task_id INTEGER NOT NULL,
    task_title TEXT NOT NULL,
    parent_title TEXT NOT NULL DEFAULT '',
    minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (report_id, source_task_id)
);

CREATE TABLE IF NOT EXISTS ai_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    raw_text TEXT NOT NULL,
    parsed_actions_json TEXT NOT NULL,
    undo_json TEXT NOT NULL DEFAULT '[]',
    applied_at TEXT,
    undone_at TEXT
);
"""


def resolve_db_path() -> Path:
    configured_path = os.environ.get("TODO_DB_PATH")
    return Path(configured_path) if configured_path else DEFAULT_DB_PATH


def connect(db_path: Path | str) -> sqlite3.Connection:
    # FastAPIは同期ルートをスレッドプールで実行するため、スレッド間利用を許可する。
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _add_progress_column_if_missing(connection: sqlite3.Connection) -> None:
    """進捗率の列が無い既存DBに列を追加し、従来の進捗(status)から初期値を埋める。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "progress" in column_names:
        return
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN progress INTEGER NOT NULL DEFAULT 0 "
        "CHECK (progress IN (0, 25, 50, 75, 100))"
    )
    connection.execute(
        "UPDATE tasks SET progress = CASE status WHEN 'done' THEN 100 WHEN 'doing' THEN 25 ELSE 0 END"
    )


def _add_user_email_column_if_missing(connection: sqlite3.Connection) -> None:
    """既存ユーザー表に、ログイン中のメールを結び付ける列を追加する。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    if "email" not in column_names:
        connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL"
    )


def bind_default_user_email(connection: sqlite3.Connection, email: str) -> None:
    """既存の固定ユーザーに指定メールを結び付け、所有者を誤って移さない。"""
    normalized_email = email.strip().casefold()
    if not normalized_email:
        raise ValueError("既存データの所有者メールを設定してください。")
    owner = connection.execute(
        "SELECT email FROM users WHERE id = ?", (DEFAULT_USER_ID,),
    ).fetchone()
    if owner is None:
        raise ValueError("既存データの所有者ユーザーが見つかりません。")
    if owner["email"] and owner["email"].casefold() != normalized_email:
        raise ValueError("既存データには別の所有者メールが設定されています。変更を中止しました。")
    email_owner = connection.execute(
        "SELECT id FROM users WHERE email = ? AND id != ?", (normalized_email, DEFAULT_USER_ID),
    ).fetchone()
    if email_owner is not None:
        raise ValueError("このメールアドレスは別のユーザーに割り当て済みです。")
    connection.execute(
        "UPDATE users SET email = ? WHERE id = ?", (normalized_email, DEFAULT_USER_ID),
    )


def _add_waiting_column_if_missing(connection: sqlite3.Connection) -> None:
    """「待ち」の列が無い既存DBに列を追加する。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "waiting_since" not in column_names:
        connection.execute("ALTER TABLE tasks ADD COLUMN waiting_since TEXT")
    if "waiting_note" not in column_names:
        connection.execute("ALTER TABLE tasks ADD COLUMN waiting_note TEXT NOT NULL DEFAULT ''")


def _add_start_date_column_if_missing(connection: sqlite3.Connection) -> None:
    """開始日の列が無い既存DBに、値を埋めずに列だけ追加する。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "start_date" not in column_names:
        connection.execute("ALTER TABLE tasks ADD COLUMN start_date TEXT")


def _add_project_id_column_if_missing(connection: sqlite3.Connection) -> None:
    """既存タスクを未分類のまま保ち、案件との関連列だけを追加する。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "project_id" not in column_names:
        connection.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id)")


def _add_report_toggle_columns_if_missing(connection: sqlite3.Connection) -> None:
    """日報の「作業時間」「勤務形態・休暇」の入力ON/OFFの列が無い既存DBに列を追加する(初期値はON)。"""
    column_names = {row["name"] for row in connection.execute("PRAGMA table_info(daily_reports)")}
    if "time_tracking_enabled" not in column_names:
        connection.execute(
            "ALTER TABLE daily_reports ADD COLUMN time_tracking_enabled INTEGER NOT NULL DEFAULT 1 "
            "CHECK (time_tracking_enabled IN (0, 1))"
        )
    if "details_enabled" not in column_names:
        connection.execute(
            "ALTER TABLE daily_reports ADD COLUMN details_enabled INTEGER NOT NULL DEFAULT 1 "
            "CHECK (details_enabled IN (0, 1))"
        )


def init_db(db_path: Path | str) -> None:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = connect(db_path)
    try:
        connection.executescript(SCHEMA)
        _add_user_email_column_if_missing(connection)
        _add_progress_column_if_missing(connection)
        _add_waiting_column_if_missing(connection)
        _add_start_date_column_if_missing(connection)
        _add_project_id_column_if_missing(connection)
        _add_report_toggle_columns_if_missing(connection)
        connection.execute(
            "INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)",
            (DEFAULT_USER_ID, DEFAULT_USER_NAME),
        )
        from app import tasks
        from app import reports

        for user in connection.execute("SELECT id FROM users").fetchall():
            tasks.initialize_task_states(connection, user["id"])
        reports.backfill_unchanged_submissions(connection)
        connection.commit()
    finally:
        connection.close()
