"""困りごとフラグ(手動・AI・キーワード検知)の管理。"""
import sqlite3

from app import clock

# AIが使えないときの簡易検知用。誤検知してもユーザーがワンクリックで外せる。
TROUBLE_KEYWORDS = (
    "困って", "困った", "詰まっ", "つまづ", "つまず", "わからない", "分からない",
    "進まない", "終わらない", "手が止ま", "行き詰", "相談したい", "無理", "お手上げ",
)


def find_open_blocker(db: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM blockers WHERE task_id = ? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def raise_blocker(
    db: sqlite3.Connection, task_id: int, source: str = "manual", note: str = ""
) -> int | None:
    """未解決の困りごとが無いときだけ追加し、そのIDを返す。既にあれば None。"""
    if find_open_blocker(db, task_id):
        return None
    cursor = db.execute(
        "INSERT INTO blockers (task_id, raised_at, source, note) VALUES (?, ?, ?, ?)",
        (task_id, clock.now_iso(), source, note),
    )
    return cursor.lastrowid


def update_open_blocker_note(db: sqlite3.Connection, task_id: int, note: str) -> bool:
    """未解決の困りごと(相談)の理由を書き換える。未解決のものが無ければ False。"""
    blocker = find_open_blocker(db, task_id)
    if blocker is None:
        return False
    db.execute("UPDATE blockers SET note = ? WHERE id = ?", (note.strip()[:200], blocker["id"]))
    return True


def resolve_open_blockers(db: sqlite3.Connection, task_id: int) -> None:
    db.execute(
        "UPDATE blockers SET resolved_at = ? WHERE task_id = ? AND resolved_at IS NULL",
        (clock.now_iso(), task_id),
    )


def toggle_blocker(db: sqlite3.Connection, task_id: int) -> bool:
    """フラグを切り替え、切り替え後に困りごと状態なら True を返す。"""
    if find_open_blocker(db, task_id):
        resolve_open_blockers(db, task_id)
        return False
    raise_blocker(db, task_id, source="manual")
    return True


def contains_trouble_keyword(text: str) -> bool:
    return any(keyword in text for keyword in TROUBLE_KEYWORDS)
