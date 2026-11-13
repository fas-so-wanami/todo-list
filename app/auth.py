"""ローカル開発ユーザーと、Azure認証ユーザーの識別を扱う。"""
from dataclasses import dataclass
import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from app import db as database

DEVELOPMENT_USERS = (
    ("member-b@example.test", "開発メンバーB"),
    ("manager@example.test", "開発上司"),
)
DEVELOPMENT_MANAGER_EMAILS = frozenset({"manager@example.test"})
DEFAULT_DEVELOPMENT_OWNER_EMAIL = "member-a@example.test"
DEVELOPMENT_COOKIE_NAME = "todo_dev_user"


class AuthConfigurationError(RuntimeError):
    """認証設定の不足や危険な起動方法を知らせる。"""


@dataclass(frozen=True)
class AuthSettings:
    """1プロセスで使う認証モードと許可範囲。"""

    mode: str
    allowed_emails: frozenset[str]
    manager_emails: frozenset[str]
    data_owner_email: str | None
    cookie_secret: bytes


def normalize_email(email: str | None) -> str:
    """メールアドレスを照合しやすい小文字形式にする。"""
    return (email or "").strip().casefold()


def _email_set(value: str) -> frozenset[str]:
    return frozenset(normalize_email(email) for email in value.split(",") if normalize_email(email))


def development_user_specs(settings: AuthSettings) -> tuple[tuple[str, str], ...]:
    """開発用ユーザーを作り、指定があればAに既存データ所有者を使う。"""
    owner_email = settings.data_owner_email or DEFAULT_DEVELOPMENT_OWNER_EMAIL
    owner_name = "ユーザーA" if settings.data_owner_email else "開発メンバーA"
    return ((owner_email, owner_name), *DEVELOPMENT_USERS)


def load_settings() -> AuthSettings:
    """起動モードに応じて設定を検証し、危険な認証抜けを防ぐ。"""
    mode = os.getenv("TODO_AUTH_MODE", "local").strip().lower()
    if mode not in {"local", "dev", "entra"}:
        raise AuthConfigurationError("TODO_AUTH_MODEはlocal、dev、entraのいずれかにしてください。")
    if os.getenv("WEBSITE_INSTANCE_ID") and mode != "entra":
        raise AuthConfigurationError("Azure上ではTODO_AUTH_MODE=entraを必須にしています。")

    allowed_emails = _email_set(os.getenv("TODO_ALLOWED_USER_EMAILS", ""))
    manager_emails = _email_set(os.getenv("TODO_MANAGER_EMAILS", ""))
    data_owner_email = normalize_email(os.getenv("TODO_DATA_OWNER_EMAIL")) or None
    if mode == "entra":
        if not allowed_emails:
            raise AuthConfigurationError("TODO_ALLOWED_USER_EMAILSに利用を許可するメールを設定してください。")
        if not data_owner_email:
            raise AuthConfigurationError("既存データを守るためTODO_DATA_OWNER_EMAILを設定してください。")
        if data_owner_email not in allowed_emails:
            raise AuthConfigurationError("TODO_DATA_OWNER_EMAILは利用許可リストにも含めてください。")
        if not manager_emails.issubset(allowed_emails):
            raise AuthConfigurationError("上司メールはTODO_ALLOWED_USER_EMAILSにも含めてください。")
    if mode == "dev":
        manager_emails = manager_emails | {email for email, _ in DEVELOPMENT_USERS if email in DEVELOPMENT_MANAGER_EMAILS}
    return AuthSettings(
        mode=mode,
        allowed_emails=allowed_emails,
        manager_emails=manager_emails,
        data_owner_email=data_owner_email,
        cookie_secret=secrets.token_bytes(32),
    )


def initialize_users(
    connection: sqlite3.Connection, db_path: Path | str, settings: AuthSettings,
) -> None:
    """必要なアカウントを準備し、既存データの持ち主を明示設定に結び付ける。"""
    if settings.mode == "entra":
        database.bind_default_user_email(connection, settings.data_owner_email or "")
    elif settings.mode == "dev":
        is_default_db = Path(db_path).resolve() == database.DEFAULT_DB_PATH.resolve()
        if is_default_db and not settings.data_owner_email:
            raise AuthConfigurationError("実データでユーザー切替するにはTODO_DATA_OWNER_EMAILを設定してください。")
        user_specs = development_user_specs(settings)
        first_email, first_name = user_specs[0]
        current_owner = connection.execute(
            "SELECT email FROM users WHERE id = ?", (database.DEFAULT_USER_ID,),
        ).fetchone()
        if current_owner is not None and current_owner["email"] not in (None, first_email):
            raise AuthConfigurationError("既存データの所有者メールが設定と違います。割り当てを中止しました。")
        database.bind_default_user_email(connection, first_email)
        connection.execute(
            "UPDATE users SET name = ? WHERE id = ?", (first_name, database.DEFAULT_USER_ID),
        )
        for email, name in user_specs[1:]:
            connection.execute(
                "INSERT OR IGNORE INTO users (name, email) VALUES (?, ?)", (name, email),
            )
            connection.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))


def get_development_users(connection: sqlite3.Connection, settings: AuthSettings) -> list[sqlite3.Row]:
    """開発切替画面に出す、固定の仮ユーザーだけを返す。"""
    emails = [email for email, _ in development_user_specs(settings)]
    placeholders = ",".join("?" for _ in emails)
    return connection.execute(
        f"SELECT id, name, email FROM users WHERE email IN ({placeholders}) "
        "ORDER BY id", emails,
    ).fetchall()


def resolve_enterprise_user(
    connection: sqlite3.Connection, email: str, settings: AuthSettings,
) -> sqlite3.Row | None:
    """許可メールだけを既存ユーザーへ結び、初回利用者は新規登録する。"""
    normalized_email = normalize_email(email)
    if not normalized_email or normalized_email not in settings.allowed_emails:
        return None
    user = connection.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
    if user is not None:
        return user
    display_name = normalized_email.partition("@")[0]
    cursor = connection.execute(
        "INSERT INTO users (name, email) VALUES (?, ?)", (display_name, normalized_email),
    )
    return connection.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()


def sign_development_user(user_id: int, secret: bytes) -> str:
    """開発用ユーザー選択を改ざんできないHttpOnly Cookie値へ署名する。"""
    value = str(user_id)
    signature = hmac.new(secret, value.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def verify_development_user(value: str | None, secret: bytes) -> int | None:
    """Cookie署名を検証し、改ざんされていないユーザー番号だけ返す。"""
    if not value or "." not in value:
        return None
    user_id_text, signature = value.split(".", 1)
    if not user_id_text.isascii() or not user_id_text.isdecimal():
        return None
    expected = hmac.new(secret, user_id_text.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return int(user_id_text)