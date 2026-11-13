"""案件ごとのWBS表示に使う期間・タスク・信号を組み立てる。"""
import sqlite3
from datetime import date, timedelta

from app import clock, projects, tasks

WEEKDAY_NAMES = "月火水木金土日"


def _week_start(value: str | None, today: date) -> date:
    """指定日を含む週の月曜日を返し、読めない値は今週に戻す。"""
    try:
        selected_date = date.fromisoformat(value) if value else today
    except ValueError:
        selected_date = today
    return selected_date - timedelta(days=selected_date.weekday())


def _schedule_signal(task: dict, today: date) -> tuple[str, str]:
    """タスク自身の日付と状態だけで、予定の遅れを文字と色名にする。"""
    if task["status"] == "done":
        return "完了", "done"
    start_date = date.fromisoformat(task["start_date"]) if task["start_date"] else None
    due_date = date.fromisoformat(task["due_date"]) if task["due_date"] else None
    if start_date is None or due_date is None:
        return "判定なし", "none"
    if due_date < today:
        return "期限超過", "late"
    if start_date < today and task["status"] == "todo":
        return "開始予定超過", "start-late"
    return "遅れなし", "on-track"


def _periods(mode: str, week_start: date, today: date) -> list[dict]:
    """週表示は4週、日表示は選択週の7日を時間軸にする。"""
    if mode == "day":
        periods = []
        for offset in range(7):
            period_date = week_start + timedelta(days=offset)
            periods.append({
                "start": period_date.isoformat(),
                "end": period_date.isoformat(),
                "label": f"{period_date.month}/{period_date.day}({WEEKDAY_NAMES[period_date.weekday()]})",
                "today_position": 50.0 if period_date == today else None,
            })
        return periods
    return [
        {
            "start": (week_start + timedelta(days=7 * offset)).isoformat(),
            "end": (week_start + timedelta(days=7 * offset + 6)).isoformat(),
            "label": f"{(week_start + timedelta(days=7 * offset)).month}/{(week_start + timedelta(days=7 * offset)).day} 週",
            "today_position": (
                ((today - (week_start + timedelta(days=7 * offset))).days + 0.5) / 7 * 100
                if week_start + timedelta(days=7 * offset) <= today
                <= week_start + timedelta(days=7 * offset + 6)
                else None
            ),
        }
        for offset in range(4)
    ]


def _bar_cells(task: dict, periods: list[dict[str, str]]) -> list[str]:
    """開始日から期限までの予定を、時間軸の各区間へ描画するクラスにする。"""
    if not task["start_date"] or not task["due_date"]:
        return ["" for _ in periods]
    start_date = date.fromisoformat(task["start_date"])
    due_date = date.fromisoformat(task["due_date"])
    overlaps = [
        start_date <= date.fromisoformat(period["end"])
        and due_date >= date.fromisoformat(period["start"])
        for period in periods
    ]
    visible_indexes = [index for index, overlaps_period in enumerate(overlaps) if overlaps_period]
    if not visible_indexes:
        return ["" for _ in periods]
    bar_cells = []
    for index, overlaps_period in enumerate(overlaps):
        if not overlaps_period:
            bar_cells.append("")
            continue
        period_start = date.fromisoformat(periods[index]["start"])
        period_end = date.fromisoformat(periods[index]["end"])
        cell_classes = []
        if index == visible_indexes[0] and start_date >= period_start:
            cell_classes.append("bar-start")
        if index == visible_indexes[-1] and due_date <= period_end:
            cell_classes.append("bar-end")
        bar_cells.append(" ".join(cell_classes) or "bar-middle")
    return bar_cells


def build_view(
    db: sqlite3.Connection,
    user_id: int,
    *,
    project_id: int | None = None,
    mode: str = "week",
    week: str | None = None,
    status_filter: str = "all",
    signal_filter: str = "all",
    search_query: str = "",
) -> dict:
    """案件と期間を確認し、WBS画面が必要とする一覧データを返す。"""
    today = clock.today()
    week_start = _week_start(week, today)
    selected_mode = mode if mode in ("week", "day") else "week"
    selected_status_filter = status_filter if status_filter in ("all", "open", "done") else "all"
    selected_signal_filter = signal_filter if signal_filter in (
        "all", "on-track", "start-late", "late", "none",
    ) else "all"
    normalized_search = search_query.strip()[:100]
    search_key = normalized_search.casefold()
    project_options = projects.list_projects(db, user_id)
    selected_project = projects.get_owned_project(db, user_id, project_id) if project_id else None
    if selected_project is None:
        selected_project = next(
            (project for project in project_options if project["status"] == "active"),
            project_options[0] if project_options else None,
        )

    periods = _periods(selected_mode, week_start, today)
    wbs_tasks = []
    total_task_count = 0
    if selected_project is not None:
        rows = db.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND project_id = ?",
            (user_id, selected_project["id"]),
        ).fetchall()
        enriched_tasks = tasks.enrich(db, rows)
        total_task_count = len(enriched_tasks)
        for task in enriched_tasks:
            task["schedule_label"], task["schedule_class"] = _schedule_signal(task, today)
            task["bar_cells"] = _bar_cells(task, periods)
        enriched_tasks = [
            task for task in enriched_tasks
            if (selected_status_filter == "all"
                or (selected_status_filter == "open" and task["status"] != "done")
                or (selected_status_filter == "done" and task["status"] == "done"))
            and (selected_signal_filter == "all" or task["schedule_class"] == selected_signal_filter)
            and (not search_key or search_key in task["title"].casefold())
        ]
        sort_task = lambda task: (
            task["start_date"] or task["due_date"] or "9999-12-31",
            task["due_date"] or "9999-12-31",
            task["id"],
        )
        wbs_tasks = tasks.arrange_as_tree(enriched_tasks, sort_task, sort_task)

    return {
        "projects": project_options,
        "project": selected_project,
        "tasks": wbs_tasks,
        "total_task_count": total_task_count,
        "status_filter": selected_status_filter,
        "signal_filter": selected_signal_filter,
        "search_query": normalized_search,
        "periods": periods,
        "mode": selected_mode,
        "week_start": week_start.isoformat(),
        "previous_week": (week_start - timedelta(days=7)).isoformat(),
        "next_week": (week_start + timedelta(days=7)).isoformat(),
        "today": today.isoformat(),
    }