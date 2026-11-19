"""プロジェクトの作成・名前変更・終了状態を管理する。"""
import sqlite3

from app import auth, clock

MAX_PROJECT_NAME_LENGTH = 100
MAX_SQLITE_INTEGER = 2 ** 63 - 1


class ProjectError(ValueError):
    """プロジェクトの操作を理由付きで断るためのエラー。"""


def get_owned_project(db: sqlite3.Connection, user_id: int, project_id: int) -> sqlite3.Row | None:
    """本人のプロジェクトだけを返す。"""
    if not isinstance(project_id, int) or not 0 < project_id <= MAX_SQLITE_INTEGER:
        return None
    return db.execute(
        "SELECT * FROM projects WHERE user_id = ? AND id = ?", (user_id, project_id),
    ).fetchone()


def get_project(db: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    """既存タスクに保存されたプロジェクトの実在を確認する。"""
    if not isinstance(project_id, int) or not 0 < project_id <= MAX_SQLITE_INTEGER:
        return None
    return db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def list_projects(
    db: sqlite3.Connection, user_id: int, *, include_closed: bool = True,
) -> list[sqlite3.Row]:
    """プロジェクトを一覧し、終了したプロジェクトを含めるか選べるようにする。"""
    condition = "" if include_closed else "AND status = 'active'"
    return db.execute(
        f"SELECT * FROM projects WHERE user_id = ? {condition} "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, name COLLATE NOCASE, id",
        (user_id,),
    ).fetchall()


def list_assignable_projects(
    db: sqlite3.Connection, user_id: int, *, include_closed: bool = False,
) -> list[sqlite3.Row]:
    """本人用と、参加を承認した共有プロジェクトだけを返す。"""
    status_condition = "" if include_closed else "AND (p.status = 'active')"
    return db.execute(
        f"""SELECT p.* FROM projects p
            WHERE (p.user_id = ? OR (p.is_shared = 1 AND EXISTS (
                    SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
                    WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
               ))) {status_condition}
            ORDER BY CASE p.is_shared WHEN 1 THEN 0 ELSE 1 END,
                     CASE p.status WHEN 'active' THEN 0 ELSE 1 END,
                     p.name COLLATE NOCASE, p.id""",
        (user_id, user_id),
    ).fetchall()


def list_manageable_projects(
    db: sqlite3.Connection, user_id: int, *, is_manager: bool = False,
) -> list[sqlite3.Row]:
    """本人の案件と、閲覧できる共有案件を返し、管理操作の可否も添える。"""
    return db.execute(
        """SELECT p.*,
                  CASE WHEN p.user_id = ? OR ? = 1 THEN 1 ELSE 0 END AS can_manage
            FROM projects p
            WHERE p.user_id = ? OR (p.is_shared = 1 AND (
                    ? = 1 OR EXISTS (
                        SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
                        WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
                    )
               ))
            ORDER BY CASE p.is_shared WHEN 1 THEN 0 ELSE 1 END,
                     CASE p.status WHEN 'active' THEN 0 ELSE 1 END,
                     p.name COLLATE NOCASE, p.id""",
        (user_id, int(is_manager), user_id, int(is_manager), user_id),
    ).fetchall()


def list_wbs_projects(
    db: sqlite3.Connection, user_id: int, *, is_manager: bool = False,
) -> list[sqlite3.Row]:
    """本人用と、参加承認済みの利用者または上司が見られる共有案件を返す。"""
    return db.execute(
        f"""SELECT p.* FROM projects p
            WHERE p.user_id = ?
               OR (p.is_shared = 1 AND (
                    ? = 1 OR EXISTS (
                        SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
                        WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
                    )
               ))
            ORDER BY CASE p.status WHEN 'active' THEN 0 ELSE 1 END,
                     CASE p.is_shared WHEN 1 THEN 0 ELSE 1 END,
                     p.name COLLATE NOCASE, p.id""",
        (user_id, int(is_manager), user_id),
    ).fetchall()


def get_assignable_project(
    db: sqlite3.Connection, user_id: int, project_id: int | None,
) -> sqlite3.Row | None:
    """タスクへ付けられる本人用または共有プロジェクトだけを返す。"""
    if not isinstance(project_id, int) or not 0 < project_id <= MAX_SQLITE_INTEGER:
        return None
    return db.execute(
        """SELECT p.* FROM projects p
           WHERE p.id = ? AND (p.user_id = ? OR (p.is_shared = 1 AND EXISTS (
               SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
               WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
           )))""",
        (project_id, user_id, user_id),
    ).fetchone()


def list_task_project_options(db: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """割当可能案件と、既存タスクに付いたままの共有案件を編集欄へ返す。"""
    return db.execute(
        """SELECT p.*,
                  CASE WHEN p.user_id = ? OR EXISTS (
                      SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
                      WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
                  ) THEN 1 ELSE 0 END AS can_assign
           FROM projects p
           WHERE p.user_id = ? OR (p.is_shared = 1 AND (
               EXISTS (
                   SELECT 1 FROM project_members pm JOIN users u ON lower(trim(u.email)) = pm.email
                   WHERE pm.project_id = p.id AND u.id = ? AND pm.status = 'accepted'
               ) OR EXISTS (
                   SELECT 1 FROM tasks t WHERE t.project_id = p.id AND t.user_id = ?
               )
           ))
           ORDER BY CASE p.is_shared WHEN 1 THEN 0 ELSE 1 END,
                    CASE p.status WHEN 'active' THEN 0 ELSE 1 END,
                    p.name COLLATE NOCASE, p.id""",
        (user_id, user_id, user_id, user_id, user_id),
    ).fetchall()


def get_wbs_project(
    db: sqlite3.Connection, user_id: int, project_id: int, *, is_manager: bool = False,
) -> sqlite3.Row | None:
    """WBSで見られる本人用・参加中・上司向け共有プロジェクトを返す。"""
    project_options = list_wbs_projects(db, user_id, is_manager=is_manager)
    return next((project for project in project_options if project["id"] == project_id), None)


def create_project(
    db: sqlite3.Connection, user_id: int, name: str, *, is_shared: bool = False,
    invite_emails: list[str] | None = None, allowed_emails: set[str] | frozenset[str] = frozenset(),
) -> int:
    """入力を検証し、共有案件なら許可済み利用者への招待もまとめて記録する。"""
    normalized_name = name.strip()
    if not normalized_name:
        raise ProjectError("プロジェクト名を入力してください。")
    if len(normalized_name) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectError(f"プロジェクト名は{MAX_PROJECT_NAME_LENGTH}文字以内にしてください。")
    normalized_invites = _validate_invite_emails(invite_emails or [], allowed_emails)
    if normalized_invites and not is_shared:
        raise ProjectError("共有相手を選ぶ場合は「チームで共有する」を選んでください。")
    owner_email = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"]
    normalized_invites = [
        email for email in normalized_invites if email != auth.normalize_email(owner_email)
    ]
    cursor = db.execute(
        "INSERT INTO projects (user_id, name, is_shared, created_at) VALUES (?, ?, ?, ?)",
        (user_id, normalized_name, int(is_shared), clock.now_iso()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("プロジェクトの作成番号を取得できませんでした")
    if normalized_invites:
        _insert_pending_members(db, cursor.lastrowid, normalized_invites)
    return cursor.lastrowid


def _validate_invite_emails(
    emails: list[str], allowed_emails: set[str] | frozenset[str],
) -> list[str]:
    normalized_allowed = {auth.normalize_email(email) for email in allowed_emails}
    normalized_emails = list(dict.fromkeys(auth.normalize_email(email) for email in emails))
    if any(not email or email not in normalized_allowed for email in normalized_emails):
        raise ProjectError("招待相手はログイン許可リストから選んでください。")
    return normalized_emails


def _insert_pending_members(db: sqlite3.Connection, project_id: int, emails: list[str]) -> None:
    invited_at = clock.now_iso()
    for email in emails:
        db.execute(
            """INSERT INTO project_members (project_id, email, status, invited_at, responded_at)
               VALUES (?, ?, 'pending', ?, NULL)
               ON CONFLICT(project_id, email) DO UPDATE SET
                   status = CASE WHEN project_members.status = 'accepted'
                                 THEN 'accepted' ELSE 'pending' END,
                   invited_at = excluded.invited_at,
                   responded_at = CASE WHEN project_members.status = 'accepted'
                                       THEN project_members.responded_at ELSE NULL END""",
            (project_id, email, invited_at),
        )


def _manageable_shared_project(
    db: sqlite3.Connection, user_id: int, project_id: int, *, is_manager: bool,
) -> sqlite3.Row | None:
    project = db.execute("SELECT * FROM projects WHERE id = ? AND is_shared = 1", (project_id,)).fetchone()
    if project is None or (project["user_id"] != user_id and not is_manager):
        return None
    return project


def add_project_members(
    db: sqlite3.Connection, user_id: int, project_id: int, emails: list[str],
    *, allowed_emails: set[str] | frozenset[str], is_manager: bool = False,
) -> None:
    """作成者または上司だけが、許可済みユーザーへ招待を追加できるようにする。"""
    project = _manageable_shared_project(db, user_id, project_id, is_manager=is_manager)
    if project is None:
        raise ProjectError("メンバーを管理できる共有プロジェクトが見つかりません。")
    normalized_emails = _validate_invite_emails(emails, allowed_emails)
    if not normalized_emails:
        raise ProjectError("招待する利用者を選んでください。")
    owner_email = db.execute("SELECT email FROM users WHERE id = ?", (project["user_id"],)).fetchone()["email"]
    normalized_emails = [
        email for email in normalized_emails if email != auth.normalize_email(owner_email)
    ]
    _insert_pending_members(db, project_id, normalized_emails)


def list_project_members(db: sqlite3.Connection, project_id: int) -> list[dict]:
    """案件の作成者と、招待中・参加中の利用者を管理欄向けに返す。"""
    owner = db.execute(
        """SELECT u.id, u.name, u.email FROM projects p
           JOIN users u ON u.id = p.user_id WHERE p.id = ? AND p.is_shared = 1""",
        (project_id,),
    ).fetchone()
    if owner is None:
        return []
    members = [{
        "user_id": owner["id"], "name": owner["name"], "email": owner["email"],
        "status": "accepted", "is_owner": True,
    }]
    rows = db.execute(
        """SELECT pm.email, pm.status, u.id AS user_id, u.name
           FROM project_members pm LEFT JOIN users u ON lower(trim(u.email)) = pm.email
           WHERE pm.project_id = ? AND pm.status IN ('accepted', 'pending')
           ORDER BY CASE pm.status WHEN 'accepted' THEN 0 ELSE 1 END, pm.email""",
        (project_id,),
    ).fetchall()
    owner_email = auth.normalize_email(owner["email"])
    for row in rows:
        if row["email"] == owner_email:
            continue
        members.append({
            "user_id": row["user_id"], "name": row["name"] or row["email"].partition("@")[0],
            "email": row["email"], "status": row["status"], "is_owner": False,
        })
    return members


def list_pending_invitations(db: sqlite3.Connection, email: str | None) -> list[sqlite3.Row]:
    """ログイン中の利用者宛てに届いた共有案件の招待だけを返す。"""
    normalized_email = auth.normalize_email(email)
    if not normalized_email:
        return []
    return db.execute(
        """SELECT p.id AS project_id, p.name AS project_name, u.name AS inviter_name, pm.invited_at
           FROM project_members pm
           JOIN projects p ON p.id = pm.project_id AND p.is_shared = 1
           JOIN users u ON u.id = p.user_id
           WHERE pm.email = ? AND pm.status = 'pending'
           ORDER BY pm.invited_at, p.name COLLATE NOCASE""",
        (normalized_email,),
    ).fetchall()


def respond_to_invitation(
    db: sqlite3.Connection, email: str | None, project_id: int, *, accept: bool,
) -> None:
    """本人宛ての保留中招待だけを承認・辞退する。"""
    normalized_email = auth.normalize_email(email)
    if not normalized_email:
        raise ProjectError("ログイン中のメールアドレスを確認できません。")
    status = "accepted" if accept else "declined"
    cursor = db.execute(
        """UPDATE project_members SET status = ?, responded_at = ?
           WHERE project_id = ? AND email = ? AND status = 'pending'
             AND EXISTS (SELECT 1 FROM projects p WHERE p.id = project_id AND p.is_shared = 1)""",
        (status, clock.now_iso(), project_id, normalized_email),
    )
    if cursor.rowcount != 1:
        raise ProjectError("受け取れるプロジェクト招待が見つかりません。")


def revoke_project_invitation(
    db: sqlite3.Connection, user_id: int, project_id: int, email: str, *, is_manager: bool = False,
) -> None:
    """作成者または上司が、保留中の招待を取り消す。"""
    if _manageable_shared_project(db, user_id, project_id, is_manager=is_manager) is None:
        raise ProjectError("招待を管理できる共有プロジェクトが見つかりません。")
    cursor = db.execute(
        """UPDATE project_members SET status = 'revoked', responded_at = ?
           WHERE project_id = ? AND email = ? AND status = 'pending'""",
        (clock.now_iso(), project_id, auth.normalize_email(email)),
    )
    if cursor.rowcount != 1:
        raise ProjectError("取り消せる招待が見つかりません。")


def remove_project_member(
    db: sqlite3.Connection, user_id: int, project_id: int, email: str, *, is_manager: bool = False,
) -> None:
    """参加者を解除する。タスクは残し、共有WBSの表示対象だけから外す。"""
    project = _manageable_shared_project(db, user_id, project_id, is_manager=is_manager)
    if project is None:
        raise ProjectError("メンバーを管理できる共有プロジェクトが見つかりません。")
    normalized_email = auth.normalize_email(email)
    owner_email = db.execute("SELECT email FROM users WHERE id = ?", (project["user_id"],)).fetchone()["email"]
    if normalized_email == auth.normalize_email(owner_email):
        raise ProjectError("プロジェクトの作成者は参加解除できません。")
    cursor = db.execute(
        """UPDATE project_members SET status = 'removed', responded_at = ?
           WHERE project_id = ? AND email = ? AND status = 'accepted'""",
        (clock.now_iso(), project_id, normalized_email),
    )
    if cursor.rowcount != 1:
        raise ProjectError("参加中の利用者が見つかりません。")


def rename_project(
    db: sqlite3.Connection, user_id: int, project_id: int, name: str, *, is_manager: bool = False,
) -> None:
    """本人のプロジェクト名だけを変更し、関連タスクはそのまま保つ。"""
    project = next(
        (item for item in list_manageable_projects(db, user_id, is_manager=is_manager)
         if item["id"] == project_id and item["can_manage"]),
        None,
    )
    normalized_name = name.strip()
    if project is None:
        raise ProjectError("変更できるプロジェクトが見つかりません。")
    if not normalized_name:
        raise ProjectError("プロジェクト名を入力してください。")
    if len(normalized_name) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectError(f"プロジェクト名は{MAX_PROJECT_NAME_LENGTH}文字以内にしてください。")
    db.execute("UPDATE projects SET name = ? WHERE id = ?", (normalized_name, project_id))


def set_project_closed(
    db: sqlite3.Connection, user_id: int, project_id: int, closed: bool, *, is_manager: bool = False,
) -> None:
    """プロジェクトを終了・再開し、タスクや過去の記録は削除せずに残す。"""
    if not any(
        item["id"] == project_id and item["can_manage"]
        for item in list_manageable_projects(db, user_id, is_manager=is_manager)
    ):
        raise ProjectError("変更できるプロジェクトが見つかりません。")
    status = "closed" if closed else "active"
    db.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))