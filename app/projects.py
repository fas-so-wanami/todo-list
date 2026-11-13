"""案件の作成・名前変更・終了状態を管理する。"""
import sqlite3

from app import clock

MAX_PROJECT_NAME_LENGTH = 100
MAX_SQLITE_INTEGER = 2 ** 63 - 1


class ProjectError(ValueError):
    """案件の操作を理由付きで断るためのエラー。"""


def get_owned_project(db: sqlite3.Connection, user_id: int, project_id: int) -> sqlite3.Row | None:
    """本人の案件だけを返す。"""
    if not isinstance(project_id, int) or not 0 < project_id <= MAX_SQLITE_INTEGER:
        return None
    return db.execute(
        "SELECT * FROM projects WHERE user_id = ? AND id = ?", (user_id, project_id),
    ).fetchone()


def list_projects(
    db: sqlite3.Connection, user_id: int, *, include_closed: bool = True,
) -> list[sqlite3.Row]:
    """案件を一覧し、終了案件を含めるか選べるようにする。"""
    condition = "" if include_closed else "AND status = 'active'"
    return db.execute(
        f"SELECT * FROM projects WHERE user_id = ? {condition} "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, name COLLATE NOCASE, id",
        (user_id,),
    ).fetchall()


def create_project(db: sqlite3.Connection, user_id: int, name: str) -> int:
    """入力を整えて案件を作り、一覧にすぐ表示できるようにする。"""
    normalized_name = name.strip()
    if not normalized_name:
        raise ProjectError("案件名を入力してください。")
    if len(normalized_name) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectError(f"案件名は{MAX_PROJECT_NAME_LENGTH}文字以内にしてください。")
    cursor = db.execute(
        "INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)",
        (user_id, normalized_name, clock.now_iso()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("案件の作成番号を取得できませんでした")
    return cursor.lastrowid


def rename_project(db: sqlite3.Connection, user_id: int, project_id: int, name: str) -> None:
    """本人の案件名だけを変更し、関連タスクはそのまま保つ。"""
    project = get_owned_project(db, user_id, project_id)
    normalized_name = name.strip()
    if project is None:
        raise ProjectError("変更できる案件が見つかりません。")
    if not normalized_name:
        raise ProjectError("案件名を入力してください。")
    if len(normalized_name) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectError(f"案件名は{MAX_PROJECT_NAME_LENGTH}文字以内にしてください。")
    db.execute("UPDATE projects SET name = ? WHERE user_id = ? AND id = ?", (normalized_name, user_id, project_id))


def set_project_closed(db: sqlite3.Connection, user_id: int, project_id: int, closed: bool) -> None:
    """案件を終了・再開し、タスクや過去の記録は削除せずに残す。"""
    if get_owned_project(db, user_id, project_id) is None:
        raise ProjectError("変更できる案件が見つかりません。")
    status = "closed" if closed else "active"
    db.execute("UPDATE projects SET status = ? WHERE user_id = ? AND id = ?", (status, user_id, project_id))