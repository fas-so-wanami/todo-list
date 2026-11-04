"""VS Code の Copilot Chat から ToDo を操作するための MCP サーバー(stdio で起動)。

Copilot が報告文を解釈し、ここで公開するツールを呼んでタスクを更新する。
変更はアプリの「Copilot からの更新履歴」に記録され、画面から取り消せる。
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Literal

from mcp.server.mcpserver import MCPServer

from app import clock, daily, db as database, tasks
from app.ai import actions as ai_actions

USER_ID = database.DEFAULT_USER_ID
MAX_MINUTES_PER_LOG = 24 * 60
ProgressPercent = Literal[0, 25, 50, 75, 100]

INSTRUCTIONS = """個人用ToDoアプリを操作するツール群です。ユーザーの報告を受けたら次の流れで使ってください。
1. まず list_tasks で現在のタスクとIDを確認する(推測でIDを使わない)。
2. 報告内容に合うツールを呼ぶ。
   - 「今から○○やる」「始めた」→ set_progress(25)。既に途中なら変更不要
   - 「半分くらい」→ 50、「ほぼ終わった」→ 75、「終わった/完了」→ 100
   - 一覧に無い作業 → create_task(進捗も指定可)
   - 「2時間かかった」「30分やった」→ log_time(分に換算して今日の作業時間に加算)
   - 「困っている」「詰まった」「進まない」「分からない」など → flag_trouble(理由を note に)
   - 残しておきたい補足 → add_memo
3. report にはユーザーの元の発言をそのまま入れる(履歴に表示される)。
4. 実行結果を短く日本語で伝える。間違えた場合は undo_last_update で取り消せる。
日報や振り返りのまとめを頼まれたら get_today_summary を使ってください。"""

server = MCPServer(name="todo", title="ToDoリスト", instructions=INSTRUCTIONS)
_initialized_db_paths: set[str] = set()


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    db_path = database.resolve_db_path()
    if str(db_path) not in _initialized_db_paths:
        database.init_db(db_path)
        _initialized_db_paths.add(str(db_path))
    connection = database.connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def describe_task(db: sqlite3.Connection, task: dict) -> dict:
    return {
        "task_id": task["id"],
        "title": task["title"],
        "parent_title": task.get("parent_title"),
        "progress_percent": task["progress"],
        "status": tasks.STATUS_LABELS[task["status"]],
        "importance": tasks.LEVEL_LABELS[task["importance"]],
        "urgency": tasks.LEVEL_LABELS[task["urgency"]],
        "due_date": task["due_date"],
        "tags": task.get("tags", []),
        "has_trouble_flag": task.get("has_open_blocker", False),
        "minutes_today": daily.get_minutes(db, task["id"], clock.today()) or 0,
        "minutes_total": task.get("total_minutes", 0),
    }


def apply_single_action(action: dict, report: str) -> dict:
    with open_db() as db:
        outcome = ai_actions.apply_actions(db, USER_ID, report.strip()[:1000], [action])
    result = outcome["results"][0]
    return {"ok": result["ok"], "message": result["message"], "update_id": outcome["report_id"]}


@server.tool(title="タスク一覧")
def list_tasks(scope: Literal["today", "open"] = "today") -> dict:
    """タスクの一覧を返す。scope="today" は今日のタスク(今日の予定・前日の未完了・期限が今日以前・進行中・今日完了)、
    scope="open" は未完了のすべてのタスク。他のツールに渡す task_id はここで確認する。"""
    with open_db() as db:
        if scope == "open":
            task_list = daily.list_unfinished_tasks(db, USER_ID)
        else:
            task_list = daily.merge_today_tasks(
                daily.list_candidates(db, USER_ID), daily.list_done_today(db, USER_ID))
        return {"date": clock.today().isoformat(), "tasks": [describe_task(db, task) for task in task_list]}


@server.tool(title="今日のまとめ")
def get_today_summary() -> dict:
    """今日の進み具合(完了数・完了率)、完了したもの、進行中のもの、困りごと、長時間化しているタスク、
    タスクごとの今日の作業時間を返す。日報・振り返りの文章を作るときに使う。"""
    with open_db() as db:
        candidates = daily.list_candidates(db, USER_ID)
        done_today = daily.list_done_today(db, USER_ID)
        progress = daily.calculate_progress(len(done_today), len(candidates))
        reflection_tasks = [describe_task(db, task) for task in daily.list_reflection_tasks(db, USER_ID)]
        unfinished = daily.list_unfinished_tasks(db, USER_ID)
        return {
            "date": clock.today().isoformat(),
            "completed_count": progress["done"],
            "planned_count": progress["total"],
            "completion_percent": progress["percent"],
            "completed_today": [describe_task(db, task) for task in done_today],
            "in_progress": [describe_task(db, task) for task in candidates if task["status"] == "doing"],
            "trouble_flagged": [describe_task(db, task) for task in unfinished if task["has_open_blocker"]],
            "long_running_over_5h": [describe_task(db, task) for task in unfinished if task["is_long_running"]],
            "minutes_today_total": sum(task["minutes_today"] for task in reflection_tasks),
            "time_by_task_today": [
                {"title": task["title"], "minutes": task["minutes_today"]}
                for task in reflection_tasks if task["minutes_today"]
            ],
        }


@server.tool(title="タスクを作成")
def create_task(title: str, progress_percent: ProgressPercent = 0, report: str = "") -> dict:
    """新しいタスクを作り、今日のタスクに入れる。title に「#案件A」と書くとタグになる。
    すでに着手済み・完了済みの作業なら progress_percent を指定する。report はユーザーの元の発言。"""
    clean_title = title.strip()[:200]
    if not clean_title:
        return {"ok": False, "message": "タイトルが空です", "update_id": None}
    return apply_single_action({"type": "create", "title": clean_title, "progress": progress_percent}, report)


@server.tool(title="進捗を変更")
def set_progress(task_id: int, progress_percent: ProgressPercent, report: str = "") -> dict:
    """タスクの進捗率を 0/25/50/75/100 のいずれかに変更する。0=未着手、100=完了、それ以外=進行中。
    「始めた」は25、「終わった」は100。report はユーザーの元の発言。"""
    return apply_single_action({"type": "progress", "task_id": task_id, "progress": progress_percent}, report)


@server.tool(title="作業時間を記録")
def log_time(task_id: int, minutes: int, report: str = "") -> dict:
    """タスクの「今日の作業時間」に minutes 分を加算する(1時間半なら90)。report はユーザーの元の発言。"""
    if not 1 <= minutes <= MAX_MINUTES_PER_LOG:
        return {"ok": False, "message": f"minutes は 1〜{MAX_MINUTES_PER_LOG} の範囲で指定してください", "update_id": None}
    return apply_single_action({"type": "add_time", "task_id": task_id, "minutes": minutes}, report)


@server.tool(title="困りごとフラグ")
def flag_trouble(task_id: int, note: str = "", report: str = "") -> dict:
    """タスクに困りごとフラグ(🚩)を立てる。詰まっている・進まない・分からない・相談したい等のときに使う。
    note には困っている理由を短く書く。report はユーザーの元の発言。"""
    return apply_single_action({"type": "blocker", "task_id": task_id, "note": note.strip()[:200]}, report)


@server.tool(title="メモを追記")
def add_memo(task_id: int, text: str, report: str = "") -> dict:
    """タスクのメモに日時付きで追記する。report はユーザーの元の発言。"""
    clean_text = text.strip()[:500]
    if not clean_text:
        return {"ok": False, "message": "メモが空です", "update_id": None}
    return apply_single_action({"type": "memo", "task_id": task_id, "text": clean_text}, report)


@server.tool(title="直前の更新を取り消す")
def undo_last_update() -> dict:
    """Copilot 経由で行った直近の更新(まだ取り消していないもの)を1件取り消す。"""
    with open_db() as db:
        latest = db.execute(
            "SELECT id, raw_text FROM ai_reports WHERE user_id = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (USER_ID,),
        ).fetchone()
        if latest is None:
            return {"ok": False, "message": "取り消せる更新はありません"}
        ai_actions.undo_report(db, USER_ID, latest["id"])
        return {"ok": True, "message": f"更新を取り消しました(報告: {latest['raw_text'] or 'なし'})"}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
